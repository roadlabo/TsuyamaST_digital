@echo off
cd /d "C:\_TsuyamaSignage\app\04_ai_monitor"
call "C:\_PythonEnvs\venv310\Scripts\activate.bat"
python "C:\_TsuyamaSignage\app\04_ai_monitor\ai_congestion_monitor.py"
