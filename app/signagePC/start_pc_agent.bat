@echo off
set HERE=%~dp0
cd /d "%HERE%"
set ROOT=%HERE%..\..
set PY=%ROOT%\runtime\python\python.exe

"%PY%" "%ROOT%\app\signagePC\pc_agent.py"
