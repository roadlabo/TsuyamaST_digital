@echo off
setlocal

REM 将来的にタスクスケジューラ登録予定
REM Windows起動時に自動起動可能

if not exist "C:\_TsuyamaSignage\logs\" (
    mkdir "C:\_TsuyamaSignage\logs\"
)

cd /d "C:\_TsuyamaSignage\app\04_ai_monitor"

echo [INFO] Starting AI Congestion Monitor...

"C:\_PythonEnvs\venv310\Scripts\python.exe" ^
"C:\_TsuyamaSignage\app\04_ai_monitor\ai_congestion_monitor.py" ^
>> "C:\_TsuyamaSignage\logs\ai_monitor.log" 2>&1

if %ERRORLEVEL% neq 0 (
    echo [ERROR] AI Monitor failed with code %ERRORLEVEL%
)

echo.
echo [INFO] Process finished.
pause
