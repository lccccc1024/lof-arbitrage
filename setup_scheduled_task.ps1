# LOF 基金套利检测 —— 定时任务安装脚本
# 以管理员身份运行：右键 -> 使用 PowerShell 运行
#
# 每个交易日 16:00 自动运行（通常在 15:00 收盘后）
# 注意：非交易日（周末/节假日）也会运行，输出数据可能过期，请留意日志文件。

$TaskName = "LOF Arbitrage Daily Scan"
$ScriptDir = $PSScriptRoot
$ScriptPath = Join-Path $ScriptDir "run_lof_arbitrage.bat"

# 兼容 python / python3 两种命名
$PythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonPath) {
    $PythonPath = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}

# 检查 Python
if (-not $PythonPath) {
    Write-Host "[错误] 未找到 Python (python / python3)，请确认已安装并加入 PATH。" -ForegroundColor Red
    exit 1
}

Write-Host "Python: $PythonPath" -ForegroundColor Cyan

# 检查脚本
if (-not (Test-Path $ScriptPath)) {
    Write-Host "[错误] 未找到 run_lof_arbitrage.bat，请确认脚本目录。" -ForegroundColor Red
    exit 1
}

# 获取当前登录用户名（非 SYSTEM）
$CurrentUser = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $CurrentUser) {
    # 回退：取运行此脚本的用户
    $CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

Write-Host "计划任务运行身份: $CurrentUser" -ForegroundColor Cyan

# 创建计划任务
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$ScriptPath`" /AUTO" `
    -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -Daily -At "16:00" -RandomDelay "00:05:00"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName `
                          -Action $Action `
                          -Trigger $Trigger `
                          -Settings $Settings `
                          -Principal $Principal `
                          -Force
    Write-Host "[OK] 计划任务已创建: $TaskName" -ForegroundColor Green
    Write-Host "    触发器: 每日 16:00 (±5分钟随机延迟)" -ForegroundColor Green
    Write-Host "    运行身份: $CurrentUser" -ForegroundColor Green
    Write-Host "    工作目录: $ScriptDir" -ForegroundColor Green
    Write-Host "    日志文件: $ScriptDir\output\lof_arbitrage_YYYYMMDD.log" -ForegroundColor Green
} catch {
    Write-Host "[错误] 创建计划任务失败: $_" -ForegroundColor Red
    Write-Host "请以管理员身份运行此脚本。" -ForegroundColor Yellow
    exit 1
}

# 查看结果
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
    Select-Object TaskName, State, @{n="NextRun";e={$_.NextRunTime}}
