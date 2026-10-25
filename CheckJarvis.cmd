@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.voice-venv\Scripts\pythonw.exe" goto setup
start "" "%~dp0.voice-venv\Scripts\pythonw.exe" "%~dp0jarvis_setup.py" --check-gui
exit /b 0
:setup
call "%~dp0SetupJarvis.cmd"
