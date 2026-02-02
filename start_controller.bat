@echo off
setlocal enabledelayedexpansion

rem ==========================================================
rem  TsuyamaST Super AI Signage Controller 起動バッチ
rem  対象:
rem    C:\_TsuyamaSignage\app\analysisPC\playerTsuyamaST_SuperAI_Signage_Controller.py
rem  タスクスケジューラ登録用（GUIアプリ想定）
rem ==========================================================

rem --- このbat自身のフォルダ（末尾に \ が付く）
set "BAT_DIR=%~dp0"

rem --- ルートフォルダ（通常は C:\_TsuyamaSignage\）
rem     batを C:\_TsuyamaSignage\ に置く想定
set "ROOT_DIR=%BAT_DIR%"

rem --- 起動する .py
set "SCRIPT=%ROOT_DIR%app\analysisPC\playerTsuyamaST_SuperAI_Signage_Controller.py"

rem --- ログ出力先（なければ作る）
set "LOG_DIR=%ROOT_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem --- Pythonの候補（優先順）
set "PYEXE="
if exist "%ROOT_DIR%runtime\python\python.exe" set "PYEXE=%ROOT_DIR%runtime\python\python.exe"
if not defined PYEXE if exist "%ROOT_DIR%venv\Scripts\python.exe" set "PYEXE=%ROOT_DIR%venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT_DIR%runtime\python\pythonw.exe" set "PYEXE=%ROOT_DIR%runtime\python\pythonw.exe"
if not defined PYEXE if exist "%ROOT_DIR%venv\Scripts\pythonw.exe" set "PYEXE=%ROOT_DIR%venv\Scripts\pythonw.exe"

rem --- 見つからなければシステムのpythonへフォールバック
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

rem --- カレントをルートに固定（相対パス事故防止）
cd /d "%ROOT_DIR%"

rem --- 起動ログ
set "TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=%TS: =0%"
set "OUT_LOG=%LOG_DIR%\controller_start_%TS%.log"

echo ========================================================== >> "%OUT_LOG%"
echo %date% %time% [INFO] START Controller >> "%OUT_LOG%"
echo %date% %time% [INFO] ROOT_DIR = "%ROOT_DIR%" >> "%OUT_LOG%"
echo %date% %time% [INFO] PYEXE    = "%PYEXE%" >> "%OUT_LOG%"
echo %date% %time% [INFO] SCRIPT   = "%SCRIPT%" >> "%OUT_LOG%"

rem --- 多重起動が困る場合：簡易ガード（pythonが同じスクリプトで動いていたら起動しない）
rem     ※必要なら有効化（行頭の rem を外す）
rem tasklist /v | find /i "playerTsuyamaST_SuperAI_Signage_Controller.py" >nul
rem if %errorlevel%==0 (
rem   echo %date% %time% [WARN] Already running. Exit. >> "%OUT_LOG%"
rem   exit /b 0
rem )

rem --- 起動（GUIアプリでもタスクで動かしやすいよう start "" を使用）
start "" "%PYEXE%" "%SCRIPT%" >> "%OUT_LOG%" 2>&1

echo %date% %time% [INFO] LAUNCHED >> "%OUT_LOG%"
exit /b 0
