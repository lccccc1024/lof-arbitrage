#!/usr/bin/env python3
"""
LOF 基金套利检测工具

每个交易日运行，输出存在套利机会的 LOF 基金到 CSV。

逻辑：
  溢价套利 = 场内价 > 净值 x (1 + 申购费率 + 安全边际)
            --> 场外申购，场内卖出
  折价套利 = 场内价 < 净值 x (1 - 赎回费率 - 安全边际)
            --> 场内买入，场外赎回

风险：
  - T+2 确认期间净值波动可能吞噬套利空间
  - 交易成本（佣金）未计入，请自行叠加
  - 大额申赎可能触发比例配售或失败

数据源：
  - 行情：东方财富 push2 接口
  - 净值：天天基金 daily NAV API
  - 费率：东方财富基金费率详情页
  - 分类：东方财富基金代码 JS（含 QDII-LOF）

注意：
  - QDII-LOF 基金在东方财富中属于 QDII 分类（不是 MK0025 LOF 板块），
    本脚本额外从 fundcode_search.js 识别 QDII/海外类型基金代码，
    然后批量查询行情，只保留有场内价格的（即 LOF 型 QDII）。
"""

import csv
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from threading import Lock
from typing import Optional

# Windows 终端 UTF-8 支持
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import requests

# ──────────────── 配 置 ────────────────

QUOTE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
QUOTE_BATCH_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FUND_JS_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
FEE_URL_TPL = "https://fundf10.eastmoney.com/jjfl_{}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://fund.eastmoney.com/",
}

SAFETY_MARGIN = 0.003   # 0.3%
MIN_AMOUNT = 50_000     # 5 万元
MAX_WORKERS = 6

DEFAULT_SUBSCRIBE_FEE = 0.015   # 1.5%
DEFAULT_REDEEM_FEE = 0.005      # 0.5%

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")


def _setup_logging() -> None:
    """配置文件日志：output/lof_arbitrage_YYYYMMDD.log（仅文件，不含终端）"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_path = os.path.join(OUTPUT_DIR, f"lof_arbitrage_{date.today():%Y%m%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )


def _emit(level: int, msg: str, *args) -> None:
    """同时输出到终端（print）和日志文件（logging）。终端编码由 reconfigure 保障。"""
    if args:
        formatted = msg % args
    else:
        formatted = msg
    # 根据级别自动添加中文前缀
    prefix = {logging.ERROR: "[错误] ", logging.WARNING: "[警告] "}.get(level, "")
    print(prefix + formatted)
    logging.log(level, msg, *args)


def _request_with_retry(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = 15,
    max_retries: int = 2,
    encoding: Optional[str] = "utf-8",
) -> requests.Response:
    """带重试的 GET 请求。encoding=None 则由 requests 自动检测（用于 HTML 页面）。"""
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if encoding is not None:
                resp.encoding = encoding
            return resp
        except requests.RequestException as e:
            if attempt == max_retries:
                raise
            _emit(logging.INFO, "  [重试 %d/%d] %s", attempt + 1, max_retries, e)
            time.sleep(2 ** attempt)


# ──────────────── 数 据 获 取 ────────────────


def fetch_lof_list() -> pd.DataFrame:
    """从东方财富获取 LOF 基金板块行情列表 (MK0025)，自动翻页获取全部。"""
    all_items = []
    page = 1
    page_size = 200

    while True:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "b:MK0025",
            "fields": "f12,f14,f2,f6",
        }
        try:
            resp = _request_with_retry(QUOTE_URL, params=params, headers=HEADERS, timeout=15)
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError) as e:
            _emit(logging.ERROR, "获取 LOF 基金行情列表失败 (第%d页): %s", page, e)
            if page == 1:
                sys.exit(1)
            break  # 后续页失败则用已获取的数据

        if not isinstance(data, dict):
            if page == 1:
                _emit(logging.ERROR, "LOF 行情接口返回格式异常")
                sys.exit(1)
            break
        items = (data.get("data") or {}).get("diff") or (data.get("data") or {}).get("list") or []
        if not items:
            break

        all_items.extend(items)

        total = data.get("data", {}).get("total", 0)
        if len(all_items) >= total or len(items) < page_size:
            break

        page += 1
        time.sleep(0.3)

    if not all_items:
        _emit(logging.WARNING, "未获取到 LOF 基金数据，接口可能变更")
        return pd.DataFrame()

    records = []
    for item in all_items:
        code = str(item.get("f12", ""))
        name = item.get("f14", "")
        if not code or not name:
            continue
        price = item.get("f2")
        records.append({
            "code": code,
            "name": name,
            "price": price if price not in (None, "-") else None,
            "amount": float(item.get("f6", 0) if item.get("f6") not in (None, "-") else 0),
        })

    df = pd.DataFrame(records)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    df = df[df["price"] > 0]  # 排除停牌/无成交（价格为 0）
    return df


def get_qdii_lof_codes() -> list[str]:
    """从 fundcode_search.js 获取 QDII/海外类型基金代码列表。

    QDII-LOF 基金在东方财富分类中属于 QDII 类型（QDII-商品、
    指数型-海外股票 等），不在 MK0025 板块下。此函数从官方
    基金分类 JS 文件提取所有 QDII/海外类型代码，后续再通过
    行情接口筛选出真正有场内交易价格的 LOF 型 QDII。
    """
    try:
        resp = _request_with_retry(FUND_JS_URL, headers=HEADERS, timeout=15)
        text = resp.text
        start = text.index("[")
        end = text.rindex("]") + 1
        arr = json.loads(text[start:end])

        # 基金代码前缀（排除 A 股股票代码 00/002/003/30/60/68）
        FUND_PREFIXES = ("15", "16", "18", "50", "51", "56", "58")

        codes = []
        for item in arr:
            code, _, _, ftype, _ = item
            if ("QDII" in ftype or "海外" in ftype) and code.startswith(FUND_PREFIXES):
                codes.append(code)
        return codes
    except Exception as e:
        _emit(logging.WARNING, "获取 QDII 基金分类数据失败: %s", e)
        return []


def fetch_qdii_lof_quotes() -> pd.DataFrame:
    """批量获取 QDII-LOF 基金行情（东方财富 ulist.np 接口）。

    流程：
      1. 从 fundcode_search.js 提取 QDII/海外类型基金代码
      2. 用 ulist.np 批量查询行情
      3. 只保留有场内价格数据的（即有交易所交易的 LOF 型 QDII）
    """
    codes = get_qdii_lof_codes()
    if not codes:
        return pd.DataFrame()

    _emit(logging.INFO, "  -> QDII/海外类型基金: %d 只，正在批量查询行情 ...", len(codes))

    # 上交所基金代码 500000–599999
    SH_PREFIX_START = "5"
    secids = []
    for code in codes:
        if code.startswith(SH_PREFIX_START):
            secids.append(f"1.{code}")
        else:
            secids.append(f"0.{code}")

    records = []
    batch_size = 100

    for i in range(0, len(secids), batch_size):
        batch = ",".join(secids[i : i + batch_size])
        try:
            resp = _request_with_retry(
                QUOTE_BATCH_URL,
                params={
                    "fltt": 2,
                    "fields": "f2,f12,f14,f6",
                    "secids": batch,
                },
                headers=HEADERS,
                timeout=15,
            )
            data = resp.json()
            if not isinstance(data, dict):
                continue
            items = (data.get("data") or {}).get("diff") or (data.get("data") or {}).get("list") or []
            for item in items:
                code = str(item.get("f12", ""))
                name = item.get("f14", "")
                price = item.get("f2")
                if code and name and price and price != "-":
                    amt_raw = item.get("f6", 0)
                    if not amt_raw or amt_raw == "-":
                        amt_raw = 0
                    records.append({
                        "code": code,
                        "name": name,
                        "price": float(price),
                        "amount": float(amt_raw),
                    })
        except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError) as e:
            _emit(logging.WARNING, "    第%d批查询失败: %s", i // batch_size + 1, e)
        time.sleep(0.3)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["price"])
    df = df[df["price"] > 0]  # 排除停牌/无成交（价格为 0）
    return df


def fetch_nav(code: str) -> Optional[float]:
    """获取基金最新公布的单位净值。"""
    params = {"fundCode": code, "pageIndex": 1, "pageSize": 1}
    try:
        resp = _request_with_retry(NAV_URL, params=params, headers=HEADERS, timeout=10)
        body = resp.json()
        lsjz = body.get("Data", {}).get("LSJZList", [])
        if lsjz:
            nav_date_str = lsjz[0].get("FSRQ", "")
            if nav_date_str:
                try:
                    nav_date = datetime.strptime(nav_date_str, "%Y-%m-%d").date()
                    days_old = (date.today() - nav_date).days
                    if days_old > 2:
                        _emit(logging.WARNING, "  %s 净值日期 %s（%d 天前），可能已过期", code, nav_date_str, days_old)
                except ValueError:
                    pass
            return float(lsjz[0]["DWJZ"])
    except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def fetch_fees(code: str) -> tuple[Optional[float], Optional[float], str]:
    """从东方财富费率页面解析申购/赎回费率及申购状态。

    申购费率：取 <strike> 原费率第一档（最低申购金额，通常 1.50%）。
    赎回费率：取费率表格第一行（最短持有期，通常 1.50%）。
    申购状态：取 "申购状态" 后的文字（开放申购/暂停申购/封闭期）。
    返回 (subscribe_fee, redeem_fee, sub_status)。
    """
    url = FEE_URL_TPL.format(code)
    try:
        resp = _request_with_retry(url, headers=HEADERS, timeout=10, encoding=None)
        html = resp.text
    except requests.RequestException:
        return None, None, "未知"

    sub_fee, red_fee = None, None
    sub_status = "未知"

    # 申购费率：<strike> 标签内为原始费率
    idx = html.find("申购费率")
    if idx >= 0:
        section = html[idx : idx + 1500]
        strikes = re.findall(r"<strike[^>]*>([\d.]+)%", section)
        if strikes:
            try:
                sub_fee = float(strikes[0]) / 100.0
            except ValueError:
                pass

    # 赎回费率：表格第一行对应最短持有期
    idx = html.find("赎回费率")
    if idx >= 0:
        section = html[idx : idx + 1000]
        rates = re.findall(r"<td[^>]*>([\d.]+)%</td>", section)
        if rates:
            try:
                red_fee = float(rates[0]) / 100.0
            except ValueError:
                pass

    # 申购状态
    idx = html.find("申购状态")
    if idx >= 0:
        section = html[idx : idx + 500]
        m = re.search(
            r"<td[^>]*>\s*(?:<span[^>]*>)?\s*([^<>]{2,15})\s*(?:</span>)?\s*</td>",
            section,
        )
        if m:
            sub_status = m.group(1).strip()

    return sub_fee, red_fee, sub_status


# ──────────────── 套 利 分 析 ────────────────


def calc_arbitrage(row: dict) -> dict:
    """对单只基金计算套利指标。"""
    price = row["price"]
    nav = row["nav"]
    sub_fee = row.get("subscribe_fee")
    red_fee = row.get("redeem_fee")

    premium_rate = (price - nav) / nav if nav and nav > 0 else None

    arbitrage_type = "无"
    premium_threshold = None
    discount_threshold = None

    if premium_rate is not None and sub_fee is not None:
        premium_threshold = sub_fee + SAFETY_MARGIN
        if premium_rate > premium_threshold:
            arbitrage_type = "溢价套利"

    if premium_rate is not None and red_fee is not None:
        discount_threshold = red_fee + SAFETY_MARGIN
        if premium_rate < -discount_threshold:
            arbitrage_type = "折价套利"

    return {
        "code": row["code"],
        "name": row["name"],
        "price": round(price, 4),
        "nav": round(nav, 4) if nav else None,
        "premium_rate": round(premium_rate * 100, 2) if premium_rate is not None else None,
        "subscribe_fee": round(sub_fee * 100, 2) if sub_fee is not None else None,
        "redeem_fee": round(red_fee * 100, 2) if red_fee is not None else None,
        "premium_threshold": round(premium_threshold * 100, 2) if premium_threshold is not None else None,
        "discount_threshold": round(discount_threshold * 100, 2) if discount_threshold is not None else None,
        "arbitrage_type": arbitrage_type,
        "amount": round(row["amount"], 2),
        "liquid": "充足" if row["amount"] >= MIN_AMOUNT else "偏低",
        "sub_status": row.get("sub_status", "未知"),
    }


# ──────────────── 输 出 ────────────────


def save_csv(results: list[dict], today: str) -> str:
    """保存结果到 CSV。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"lof_arbitrage_{today}.csv")

    header_map = {
        "code": "基金代码",
        "name": "基金名称",
        "price": "场内价格",
        "nav": "基金净值",
        "premium_rate": "折溢价率(%)",
        "subscribe_fee": "申购费率(%)",
        "redeem_fee": "赎回费率(%)",
        "premium_threshold": "溢价阈值(%)",
        "discount_threshold": "折价阈值(%)",
        "arbitrage_type": "套利类型",
        "amount": "成交额(元)",
        "liquid": "流动性",
        "sub_status": "申购状态",
    }
    fieldnames = list(header_map.keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow(header_map)
        writer.writerows(results)

    return path


def print_summary(results: list[dict]):
    """控制台打印汇总（Windows GBK 安全版）。"""
    df = pd.DataFrame(results)

    sep = "=" * 70
    dash = "-" * 60

    print(f"\n{sep}")
    print(f"  LOF {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{sep}")

    if df.empty:
        print("\n  [空] 未获取到数据。\n")
        return

    # --- 全部基金 ---
    print(f"\n>> 全部 {len(df)} 只 LOF 基金 (溢价 Top 10 / 折价 Top 10)")
    print(f"    {'代码':>6}  {'名称':<22}  {'价格':>8}  {'净值':>8}  {'溢价率%':>7}  {'类型':<8}")
    print(f"    {dash}")

    sorted_df = df.sort_values("premium_rate", ascending=False, na_position="last")
    for _, r in sorted_df.head(10).iterrows():
        prem = f"{r['premium_rate']:+.2f}" if pd.notna(r["premium_rate"]) else "   N/A"
        nav_s = f"{r['nav']:.4f}" if pd.notna(r["nav"]) else "   N/A"
        at = (r["arbitrage_type"] if pd.notna(r["arbitrage_type"])
              and r["arbitrage_type"] != "无" else "")
        print(f"    {r['code']:>6}  {r['name']:<22}  {r['price']:>8.4f}  {nav_s:>8}  {prem:>7}  {at:<8}")

    # 折价 Top 10
    bottom = sorted_df.tail(10)
    if len(sorted_df) > 20:
        print(f"    {'...':>6}  {'(中间省略)':<22}")
    for _, r in bottom.iterrows():
        prem = f"{r['premium_rate']:+.2f}" if pd.notna(r["premium_rate"]) else "   N/A"
        nav_s = f"{r['nav']:.4f}" if pd.notna(r["nav"]) else "   N/A"
        at = (r["arbitrage_type"] if pd.notna(r["arbitrage_type"])
              and r["arbitrage_type"] != "无" else "")
        print(f"    {r['code']:>6}  {r['name']:<22}  {r['price']:>8.4f}  {nav_s:>8}  {prem:>7}  {at:<8}")

    # --- 套利机会 ---
    arb_df = df[df["arbitrage_type"] != "无"]
    if not arb_df.empty:
        print(f"\n>> 存在套利机会的基金 ({len(arb_df)} 只)")
        print(f"    {'代码':>6}  {'名称':<22}  {'溢价率%':>7}  {'类型':<8}  {'流动性':<4}  {'申购状态':<8}")
        print(f"    {'-'*60}")
        for _, r in arb_df.iterrows():
            prem = f"{r['premium_rate']:+.2f}" if pd.notna(r["premium_rate"]) else "   N/A"
            print(f"    {r['code']:>6}  {r['name']:<22}  {prem:>7}  {r['arbitrage_type']:<8}  {r['liquid']:<4}  {r.get('sub_status', '未知'):<8}")
    else:
        print("\n>> 当前无显著套利机会。")

    # --- 低流动性预警 ---
    low_liq = df[df["liquid"] == "偏低"]
    if not low_liq.empty:
        print(f"\n!! 流动性偏低 ({len(low_liq)} 只, 日成交额 < {MIN_AMOUNT/10000:.0f}万)")
        for _, r in low_liq.iterrows():
            print(f"    {r['code']:>6}  {r['name']:<22}  成交额: {r['amount']:>10.0f}")

    print()


# ──────────────── 主 流 程 ────────────────


def main() -> None:
    _setup_logging()
    today = date.today().strftime("%Y%m%d")
    start = time.time()

    _emit(logging.INFO, "=" * 50)
    _emit(logging.INFO, "  LOF 基金套利检测  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    _emit(logging.INFO, "=" * 50)

    # -- 1. 获取 LOF 基金行情（含 QDII-LOF）--
    _emit(logging.INFO, "[1/5] 获取 LOF 基金行情列表 (东方财富 MK0025) ...")
    lof_df = fetch_lof_list()
    _emit(logging.INFO, "  -> 获取到 %d 只 LOF 基金", len(lof_df))

    if lof_df.empty:
        _emit(logging.ERROR, "[退出] 未获取到 LOF 基金数据。")
        sys.exit(1)

    # -- 1b. 补充 QDII-LOF 基金行情 --
    _emit(logging.INFO, "[2/5] 获取 QDII-LOF 基金行情 (分类 JS + ulist.np) ...")
    qdii_df = fetch_qdii_lof_quotes()

    if qdii_df.empty:
        _emit(logging.INFO, "  -> 未获取到 QDII-LOF 基金")
    else:
        _emit(logging.INFO, "  -> 获取到 %d 只 QDII-LOF 基金（有场内交易的）", len(qdii_df))

        combined = pd.concat([lof_df, qdii_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="code", keep="first")
        _emit(logging.INFO, "  -> 合并后共 %d 只基金（去重）", len(combined))
        lof_df = combined

    funds = lof_df.to_dict("records")

    # -- 2. 获取净值 --
    _emit(logging.INFO, "[3/5] 获取基金净值 (天天基金) ...")
    nav_ok = 0
    nav_lock = Lock()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(fetch_nav, f["code"]): f for f in funds}
        for fut in as_completed(fut_map):
            f = fut_map[fut]
            nav = fut.result()
            f["nav"] = nav
            if nav:
                with nav_lock:
                    nav_ok += 1
    _emit(logging.INFO, "  -> 净值获取成功 %d/%d", nav_ok, len(funds))

    valid = [f for f in funds if f.get("nav") is not None]
    _emit(logging.INFO, "  -> 有净值数据的 LOF: %d 只", len(valid))

    if not valid:
        _emit(logging.ERROR, "[退出] 无有效净值数据。")
        sys.exit(1)

    # -- 3. 获取费率 --
    _emit(logging.INFO, "[4/5] 获取基金费率 (东方财富) ...")
    fee_ok = 0
    sub_defaults = 0
    red_defaults = 0
    fee_lock = Lock()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(fetch_fees, f["code"]): f for f in valid}
        for fut in as_completed(fut_map):
            f = fut_map[fut]
            sub_fee, red_fee, sub_status = fut.result()
            if sub_fee is not None:
                f["subscribe_fee"] = sub_fee
            else:
                f["subscribe_fee"] = DEFAULT_SUBSCRIBE_FEE
                with fee_lock:
                    sub_defaults += 1
            if red_fee is not None:
                f["redeem_fee"] = red_fee
            else:
                f["redeem_fee"] = DEFAULT_REDEEM_FEE
                with fee_lock:
                    red_defaults += 1
            f["sub_status"] = sub_status
            if sub_fee is not None and red_fee is not None:
                with fee_lock:
                    fee_ok += 1

    parts = [f"全部解析 {fee_ok}/{len(valid)}"]
    if sub_defaults:
        parts.append(f"申购费率默认 {sub_defaults}")
    if red_defaults:
        parts.append(f"赎回费率默认 {red_defaults}")
    _emit(logging.INFO, "  -> 费率获取完成 (%s)", "; ".join(parts))

    # 费率解析大面积失败告警
    if sub_defaults > len(valid) * 0.5 or red_defaults > len(valid) * 0.5:
        _emit(logging.WARNING,
            "  ⚠ 超过一半基金使用默认费率！东方财富费率页面可能已改版，请检查正则表达式"
        )
    elif sub_defaults or red_defaults:
        _emit(logging.INFO,
            "  ⚠ 部分基金使用默认费率（申购 %.1f%% / 赎回 %.1f%%），可能造成套利假阴性",
            DEFAULT_SUBSCRIBE_FEE * 100,
            DEFAULT_REDEEM_FEE * 100,
        )

    # -- 4. 计算 + 输出 --
    _emit(logging.INFO, "[5/5] 计算套利机会 ...")
    results = [calc_arbitrage(f) for f in valid]

    csv_path = save_csv(results, today)
    _emit(logging.INFO, "  -> CSV: %s", csv_path)

    print_summary(results)

    elapsed = time.time() - start
    _emit(logging.INFO, "  耗时 %.1fs\n", elapsed)


if __name__ == "__main__":
    main()
