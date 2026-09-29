@echo off
rem Builds dist\MomentumScanner\MomentumScanner.exe and copies what it needs next to it.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing - run run.bat once first.
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -q pyinstaller || exit /b 1
".venv\Scripts\python.exe" -m PyInstaller MomentumScanner.spec --noconfirm --clean || exit /b 1

set OUT=dist\MomentumScanner
copy /Y config.yaml "%OUT%\config.yaml" >nul
copy /Y .env.example "%OUT%\.env.example" >nul
copy /Y README.md "%OUT%\README.md" >nul
if exist .env (
  if not exist "%OUT%\.env" copy /Y .env "%OUT%\.env" >nul
)
if not exist "%OUT%\tradingview" mkdir "%OUT%\tradingview"
copy /Y tradingview\*.pine "%OUT%\tradingview\" >nul 2>nul

echo.
echo Built %OUT%\MomentumScanner.exe
echo It keeps its own config.yaml, .env and var\ (database, logs) in %OUT%.
echo To reuse your existing history, copy the var folder into %OUT% (with the scanner stopped).
endlocal
