@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem =========================================================
rem TsuyamaSignage pc_agent watchdog (完成版)
rem 目的:
rem  - pc_agent.py が落ちたら自動で再起動
rem  - 指定時間窓(WINDOW_SEC)内に MAX_CRASH 回落ちたら OS再起動
rem  - STATE肥大化防止（古い記録は自動で捨てる）
rem =========================================================

set "BASE=C:\_TsuyamaSignage"
set "PY=%BASE%\runtime\python\python.exe"
set "AGENT=%BASE%\app\pc_agent.py"
set "LOGDIR=%BASE%\logs"
set "LOG=%LOGDIR%\pc_agent_watchdog.log"
set "STATE=%LOGDIR%\pc_agent_crash_state.csv"

rem ---- 監視パラメータ ----
set "WINDOW_SEC=600"    rem 10分
set "MAX_CRASH=3"       rem 10分で3回落ちたら再起動
set "SLEEP_SEC=5"       rem 再起動までの待ち
set "LOG_MAX_BYTES=10485760"  rem 10MBでログをローテ（簡易）

rem ---- logsフォルダが無ければ作る ----
if not exist "%LOGDIR%" (
  mkdir "%LOGDIR%" >nul 2>&1
)

rem ---- 重要ファイル存在チェック（ここで落とすと原因が分かりやすい）----
if not exist "%PY%" (
  echo [%date% %time%] ERROR: python not found: "%PY%" >> "%LOG%"
  exit /b 1
)
if not exist "%AGENT%" (
  echo [%date% %time%] ERROR: agent not found: "%AGENT%" >> "%LOG%"
  exit /b 1
)

rem ---- STATEが無ければ作成（ヘッダ付き）----
if not exist "%STATE%" (
  echo epoch_sec,code > "%STATE%"
)

rem =========================================================
rem ループ本体
rem =========================================================
:loop

call :maybe_rotate_log

call :now_epoch NOW
echo [%date% %time%] start pc_agent (epoch=!NOW!)>> "%LOG%"

rem ---- pc_agent 実行（標準出力/エラーをLOGへ）----
"%PY%" "%AGENT%" >> "%LOG%" 2>&1
set "CODE=%errorlevel%"

call :now_epoch NOW2
echo [%date% %time%] pc_agent exited code=!CODE! (epoch=!NOW2!)>> "%LOG%"

rem ---- クラッシュ記録（epoch, code）----
>> "%STATE%" echo !NOW2!,!CODE!

rem ---- WINDOW_SEC内のクラッシュ回数を計数し、古い行を捨てる（STATE肥大化防止）----
call :count_recent_crashes CRASHES

echo [%date% %time%] recent_crashes=!CRASHES! (window=%WINDOW_SEC%s max=%MAX_CRASH%)>> "%LOG%"

if !CRASHES! GEQ %MAX_CRASH% (
  echo [%date% %time%] too many crashes -> reboot>> "%LOG%"
  shutdown /r /t 10 /c "pc_agent crashed repeatedly. Auto reboot." /f
  exit /b 0
)

timeout /t %SLEEP_SEC% /nobreak >nul
goto loop


rem =========================================================
rem サブルーチン：現在時刻を epoch秒で返す
rem 使い方: call :now_epoch VAR
rem =========================================================
:now_epoch
for /f %%A in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "%~1=%%A"
exit /b 0


rem =========================================================
rem サブルーチン：WINDOW_SEC内のクラッシュ回数を数える
rem  - STATEの古い行は破棄して再書き込み（肥大化防止）
rem  - 戻り値: CRASHES (件数)
rem =========================================================
:count_recent_crashes
call :now_epoch NOWE
set /a CUTOFF=!NOWE!-%WINDOW_SEC%

rem PowerShellで:
rem  - ヘッダ以外を読み込む
rem  - cutoff以上のみ残す
rem  - 件数を数える
rem  - 残した行でSTATEを再生成（ヘッダ維持）
for /f %%A in ('
  powershell -NoProfile -Command ^
    "$p = ''%STATE%''; " ^
    "$lines = Get-Content $p -ErrorAction SilentlyContinue; " ^
    "if(-not $lines -or $lines.Count -lt 2){ ''epoch_sec,code'' | Set-Content $p; Write-Output 0; exit } " ^
    "$data = $lines | Select-Object -Skip 1 | ForEach-Object { $_.Trim() } | Where-Object { $_ -match ''^\d+,\-?\d+'' }; " ^
    "$keep = @(); " ^
    "foreach($l in $data){ $e = [int]($l.Split('','')[0]); if($e -ge %CUTOFF%){ $keep += $l } } " ^
    "(''epoch_sec,code'') + $keep | Set-Content $p; " ^
    "Write-Output $keep.Count"
') do set "%~1=%%A"

exit /b 0


rem =========================================================
rem サブルーチン：ログの簡易ローテーション（10MB超で .1へ退避）
rem =========================================================
:maybe_rotate_log
if exist "%LOG%" (
  for %%F in ("%LOG%") do set "SZ=%%~zF"
  if !SZ! GEQ %LOG_MAX_BYTES% (
    del "%LOG%.1" >nul 2>&1
    ren "%LOG%" "pc_agent_watchdog.log.1" >nul 2>&1
  )
)
exit /b 0
