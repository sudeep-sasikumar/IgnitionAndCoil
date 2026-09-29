@echo off
rem Ignition & Coil - start the scanner + dashboard (Windows).
rem First run: creates the Python environment and installs everything (a few minutes).
rem Written with labels instead of ( ) blocks: brackets inside messages or the folder name
rem would otherwise break the batch parser.
setlocal
cd /d "%~dp0"
title Ignition ^& Coil scanner

if exist ".venv\Scripts\python.exe" goto env_ready
echo Creating the Python environment...
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3.12 -m venv .venv
if exist ".venv\Scripts\python.exe" goto install
:use_python
python -m venv .venv
if exist ".venv\Scripts\python.exe" goto install
echo.
echo Could not create the Python environment.
echo Install Python 3.12 from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1

:install
echo Installing packages - this takes a few minutes the first time...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto install_failed

:env_ready
if exist ".env" goto run
copy /Y .env.example .env >nul
echo Created .env - add your Telegram token and chat ID to it, see README section 2.
echo Until then alerts are printed in this window.

:run
".venv\Scripts\python.exe" main.py %*
if not errorlevel 1 goto done
echo.
echo The scanner stopped with an error - see var\logs\scanner.log
echo Restarting in 30 seconds. Press Ctrl+C to cancel.
timeout /t 30
goto run

:install_failed
echo.
echo Installing the packages failed - check the internet connection and run this again.
pause
exit /b 1

:done
endlocal
