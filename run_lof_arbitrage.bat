@echo off
chcp 65001 >nul
title LOF基金套利检测工具

:: /AUTO 模式：用于计划任务自动运行，跳过所有 pause
set AUTO_MODE=0
if /i "%~1"=="/AUTO" set AUTO_MODE=1

echo ====================================
echo  LOF 基金套利检测工具
echo ====================================
echo.

cd /d "%~dp0"

if not exist "lof_arbitrage.py" (
    echo [错误] 未找到 lof_arbitrage.py
    echo 请确保本脚本与 lof_arbitrage.py 在同一目录下。
    if "%AUTO_MODE%"=="0" pause
    exit /b 1
)

echo [1/2] 检查 Python 环境...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请确认已安装并加入 PATH。
    if "%AUTO_MODE%"=="0" pause
    exit /b 1
)

python --version

echo.
echo [2/2] 开始运行...
echo.

python lof_arbitrage.py

if %errorlevel% equ 0 (
    echo.
    echo 运行完成！CSV 文件已保存到 output 目录。
    if "%AUTO_MODE%"=="0" (
        echo.
        echo 按任意键退出...
        pause >nul
    )
) else (
    echo.
    echo [错误] 脚本执行失败，请检查上方错误信息。
    if "%AUTO_MODE%"=="0" (
        echo.
        echo 按任意键退出...
        pause >nul
    )
)
