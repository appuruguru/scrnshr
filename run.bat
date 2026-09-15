@echo off
cd /d "%~dp0"

if not exist venv (
    python -m venv venv
    if errorlevel 1 (
        echo Failed to create a virtual environment. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

start "" pythonw scrnshr.py
