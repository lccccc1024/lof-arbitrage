@echo off
title LOF Arbitrage Scanner

set AUTO_MODE=0
if /i "%~1"=="/AUTO" set AUTO_MODE=1

echo ====================================
echo   LOF Arbitrage Scanner
echo ====================================
echo.

cd /d "%~dp0"

if not exist "lof_arbitrage.py" (
    echo [ERROR] lof_arbitrage.py not found
    echo Please place this script next to lof_arbitrage.py
    if "%AUTO_MODE%"=="0" pause
    exit /b 1
)

echo [1/3] Checking Python ...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python and add to PATH.
    if "%AUTO_MODE%"=="0" pause
    exit /b 1
)
python --version

echo.
echo [2/3] Checking dependencies ...
python -c "import pandas, requests" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Missing dependencies. Run: pip install pandas requests
    if "%AUTO_MODE%"=="0" pause
    exit /b 1
)

echo.
echo [3/3] Running ...
echo.

python lof_arbitrage.py

if %errorlevel% equ 0 (
    echo.
    echo Done! Check output/ for log and CSV files.
    if "%AUTO_MODE%"=="0" (
        echo.
        echo Press any key to exit...
        pause >nul
    )
) else (
    echo.
    echo [ERROR] Script failed. Check messages above.
    if "%AUTO_MODE%"=="0" (
        echo.
        echo Press any key to exit...
        pause >nul
    )
)
