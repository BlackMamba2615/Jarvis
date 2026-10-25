@echo off
setlocal
cd /d "%~dp0"
where pyw.exe >nul 2>nul
if errorlevel 1 goto missing
py.exe -3.12 -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" >nul 2>nul
if errorlevel 1 goto missing
start "" pyw.exe -3.12 "%~dp0jarvis_setup.py"
exit /b 0
:missing
echo Please install 64-bit Python 3.12 with Tcl/Tk and the Python launcher.
echo Official source: https://www.python.org/downloads/windows/
echo Then double-click SetupJarvis.cmd again.
pause
exit /b 1
