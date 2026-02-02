@echo off
setlocal enabledelayedexpansion

rem ==========================================================
rem  TsuyamaST Super AI Signage Controller 起動バッチ（venv確定版）
rem  - venvを必ず使用（PySide6有効）
rem  - stdout/stderrを必ず logs に残す
rem ==========================================================

set "BAT_DIR=%~dp0"
set "ROOT_DIR=%BAT_DIR%"
set "SCRIPT=%ROOT_DIR%app\analysisPC\playerTsuyamaST_SuperAI_Signage_Controller.py"

set "LOG_DIR=%ROOT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=%TS: =0%"
set "OUT_LOG=%LOG_DIR%\controller_start_%TS%.log"

rem --- venv python を固定（activate不要）
set "PYW=%ROOT_DIR%runtime\venv\Scripts\pythonw.exe"
set "PYC=%ROOT_DIR%runtime\venv\Scripts\python.exe"
set "PYEXE="

if exist "%PYW%" (
  set "PYEXE=%PYW%"
) else if exist "%PYC%" (
  set "PYEXE=%PYC%"
)

rem --- 事前チェック
if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: "%SCRIPT%"
  echo %date% %time% [ERROR] Script not found: "%SCRIPT%" >> "%LOG_DIR%\controller_start_error.log"
  exit /b 1
)
if not defined PYEXE (
  echo [ERROR] venv python not found. expected:
  echo   "%PYW%"
  echo   "%PYC%"
  echo %date% %time% [ERROR] venv python not found >> "%LOG_DIR%\controller_start_error.log"
  exit /b 2
)

cd /d "%ROOT_DIR%"

echo ========================================================== >> "%OUT_LOG%"
echo %date% %time% [INFO] START Controller >> "%OUT_LOG%"
echo %date% %time% [INFO] ROOT_DIR = "%ROOT_DIR%" >> "%OUT_LOG%"
echo %date% %time% [INFO] PYEXE    = "%PYEXE%" >> "%OUT_LOG%"
echo %date% %time% [INFO] SCRIPT   = "%SCRIPT%" >> "%OUT_LOG%"

rem --- 子プロセスのstdout/stderrを確実にログへ（start単独は不可）
start "" /wait cmd /c ""%PYEXE%" "%SCRIPT%" >> "%OUT_LOG%" 2>&1"

set "EC=%errorlevel%"
echo %date% %time% [INFO] EXITCODE=%EC% >> "%OUT_LOG%"

exit /b %EC%
