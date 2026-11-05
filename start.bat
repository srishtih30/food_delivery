@echo off
setlocal
cd /d "%~dp0"
echo ===================================================
echo  Route Studio - setup and launch
echo ===================================================
echo Keep this window open while using the app.
echo.

set "PYTHON_CMD=python"
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo Enable Add Python to PATH, then run this file again.
  goto :failed
)
where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo Install Node.js 22.12 or newer from https://nodejs.org/
  echo Then run this file again.
  goto :failed
)
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit((a===20 && b>=19)||(a===22 && b>=12)||a>22?0:1)" >nul 2>nul
if errorlevel 1 (
  echo Please install Node.js 22.12 or newer from https://nodejs.org/
  goto :failed
)

if not exist "backend\.venv\Scripts\python.exe" (
  echo Creating a private Python environment...
  %PYTHON_CMD% -m venv "backend\.venv"
  if errorlevel 1 goto :failed
)
echo Installing Python requirements...
"backend\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "backend\requirements.txt"
if errorlevel 1 goto :failed

pushd "frontend"
echo Installing frontend requirements...
call npm ci --no-audit --no-fund
if errorlevel 1 goto :frontend_failed
echo Building the latest app...
call npm run build
if errorlevel 1 goto :frontend_failed
popd

echo Starting the app and opening Fleet Lab...
"backend\.venv\Scripts\python.exe" "launch.py"
if errorlevel 1 goto :failed
exit /b 0

:frontend_failed
popd
:failed
echo.
echo Setup or launch failed. See the message above for details.
pause
exit /b 1
