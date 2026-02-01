@echo off
setlocal

REM === Base folder ===
set BASE=C:\_TsuyamaSignage

REM === Embedded Python ===
set PY=%BASE%\runtime\python\python.exe

REM === Script ===
set SCRIPT=%BASE%\app\pc_agent.py

REM === Optional: LHM path (for CPU temp via WMI) ===
set LHM=%BASE%\bin\LibreHardwareMonitor\LibreHardwareMonitor.exe

REM === Run ===
"%PY%" "%SCRIPT%" --base "%BASE%" --interval 5 --lhm "%LHM%"

endlocal
