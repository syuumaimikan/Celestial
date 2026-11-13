@echo off
rem Celestial launcher: double-click to open the app window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First-time setup...
    uv venv || goto :fail
    uv pip install -e . || goto :fail
)
start "" ".venv\Scripts\pythonw.exe" -m celestial
exit /b 0

:fail
echo Setup failed. Install uv (https://docs.astral.sh/uv/) and Python 3.11+, then try again.
pause
exit /b 1
