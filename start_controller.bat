@echo off
setlocal enabledelayedexpansion

rem ==========================================================
rem  TsuyamaST Super AI Signage Controller 起動バッチ（ログ確実版）
rem  - Pythonのstdout/stderrを必ずログへ出す
rem  - 例外終了しても原因が残る
rem ==========================================================

set "BAT_DIR=%~dp0"
set "ROOT_DIR=%BAT_DIR%"
set "SCRIPT=%ROOT_DIR%app\analysisPC\playerTsuyamaST_SuperAI_Signage_Controller.py"

set "LOG_DIR=%ROOT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem --- 起動ログ
set "TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=%TS: =0%"
set "OUT_LOG=%LOG_DIR%\controller_start_%TS%.log"

rem --- Python探索（pythonw優先：黒窓を出しにくい）
set "PYEXE="
if exist "%ROOT_DIR%runtime\python\pythonw.exe" set "PYEXE=%ROOT_DIR%runtime\python\pythonw.exe"
if not defined PYEXE if exist "%ROOT_DIR%venv\Scripts\pythonw.exe" set "PYEXE=%ROOT_DIR%venv\Scripts\pythonw.exe"
if not defined PYEXE if exist "%ROOT_DIR%runtime\python\python.exe" set "PYEXE=%ROOT_DIR%runtime\python\python.exe"
if not defined PYEXE if exist "%ROOT_DIR%venv\Scripts\python.exe" set "PYEXE=%ROOT_DIR%venv\Scripts\python.exe"

if not defined PYEXE (
  where python >nul 2>&1
  if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python') do (
      set "PYEXE=%%P"
      goto :PY_FOUND
    )
  )
)
:PY_FOUND

rem --- 事前チェック
if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: "%SCRIPT%"
  echo %date% %time% [ERROR] Script not found: "%SCRIPT%" >> "%LOG_DIR%\controller_start_error.log"
  exit /b 1
)
if not defined PYEXE (
  echo [ERROR] Python not found.
  echo %date% %time% [ERROR] Python not found. >> "%LOG_DIR%\controller_start_error.log"
  exit /b 2
)

cd /d "%ROOT_DIR%"

echo ========================================================== >> "%OUT_LOG%"
echo %date% %time% [INFO] START Controller >> "%OUT_LOG%"
echo %date% %time% [INFO] ROOT_DIR = "%ROOT_DIR%" >> "%OUT_LOG%"
echo %date% %time% [INFO] PYEXE    = "%PYEXE%" >> "%OUT_LOG%"
echo %date% %time% [INFO] SCRIPT   = "%SCRIPT%" >> "%OUT_LOG%"

rem --- ★重要：子プロセスのstdout/stderrを確実にログへ入れる
rem startを使うとリダイレクトが子に効かないため、cmd /c 経由で /wait する
start "" /wait cmd /c ""%PYEXE%" "%SCRIPT%" >> "%OUT_LOG%" 2>&1"

set "EC=%errorlevel%"
echo %date% %time% [INFO] EXITCODE=%EC% >> "%OUT_LOG%"

exit /b %EC%
