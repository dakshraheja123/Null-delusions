@echo off
setlocal
cd /d "%~dp0"

python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=1)" >nul 2>&1
if not errorlevel 1 (
    echo Gemini Desk is already running. Opening the existing server...
    start "" http://127.0.0.1:8000
    exit /b 0
)

echo Installing or checking dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check that Python and pip are installed.
    pause
    exit /b 1
)

echo.
echo Starting Gemini Desk at http://127.0.0.1:8000
python main.py
pause
