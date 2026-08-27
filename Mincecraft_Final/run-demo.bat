@echo off
REM DOKI weighing station — demo with no hardware.
REM Double-click this, or run it from a command prompt in this folder.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found on the PATH.
    echo Install it from python.org and tick "Add Python to PATH".
    pause
    exit /b 1
)

python -c "import PyQt5" >nul 2>&1
if errorlevel 1 (
    echo Installing PyQt5, one time only...
    python -m pip install PyQt5
    if errorlevel 1 (
        echo PyQt5 could not be installed.
        pause
        exit /b 1
    )
)

echo Starting the station in demo mode - simulated scale, scratch log files.
python station.py --demo
pause
