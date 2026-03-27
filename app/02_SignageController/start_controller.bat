@echo off
cd /d "C:\_TsuyamaSignage\app\02_SignageController"
call "C:\_PythonEnvs\venv_signage_controller\Scripts\activate.bat"
python "C:\_TsuyamaSignage\app\02_SignageController\analysisPCTsuyamaST_SuperAI_Signage_Controller.py"
