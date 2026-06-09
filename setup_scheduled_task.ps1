# LOF 基金套利检测 —— 定时任务安装脚本
# 以管理员身份运行：右键 -> 使用 PowerShell 运行
#
# 每个交易日 16:00 自动运行（通常在 15:00 收盘后）
# 如果当天是非交易日或周六日，运行会正常失败（无套利数据）

$TaskName = "LOF Arbitrage Daily Scan"
$ScriptPath = Join-Path $PSScriptRoot "run_lof_arbitrage.bat"
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

# 创建计划任务
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$ScriptPath`" /AUTO"
$Trigger = New-ScheduledTaskTrigger -Daily -At "16:00"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

try {
    Register-ScheduledTask -TaskName $TaskName `
                          -Action $Action `
                          -Trigger $Trigger `
                          -Settings $Settings `
                          -Principal $Principal `
                          -Force
    Write-Host "[OK] 计划任务已创建: $TaskName" -ForegroundColor Green
    Write-Host "    触发器: 每日 16:00" -ForegroundColor Green
    Write-Host "    运行方式: SYSTEM（可在任务计划程序中修改）" -ForegroundColor Green
} catch {
    Write-Host "[错误] 创建计划任务失败: $_" -ForegroundColor Red
    Write-Host "请以管理员身份运行此脚本。" -ForegroundColor Yellow
}

# 查看结果
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
    Select-Object TaskName, State, @{n="NextRun";e={$_.NextRunTime}}
