@echo off
setlocal
cd /d "%~dp0"
title Workshop Porter

echo.
echo  ==========================================
echo       Workshop Porter - Windows Test
echo  ==========================================
echo.

if not exist "config.toml" (
    echo Creating config.toml from config.example.toml...
    copy /Y "config.example.toml" "config.toml" >nul
    echo.
    echo Edit config.toml and add your admin token and Steam account first.
    echo Then run this file again.
    pause
    exit /b 1
)

set "PYTHON_BOOTSTRAP="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_BOOTSTRAP=py -3"

if not defined PYTHON_BOOTSTRAP (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_BOOTSTRAP=python"
)

if not defined PYTHON_BOOTSTRAP (
    echo ERROR: Python 3.11 or newer was not found.
    echo Install Python from https://www.python.org/downloads/ and enable "Add Python to PATH".
    pause
    exit /b 1
)

echo Installing or updating Python dependencies...
%PYTHON_BOOTSTRAP% -m pip install --user --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto :failed

echo.
echo Starting Workshop Porter at http://127.0.0.1:8765
echo Keep this window open. Press Ctrl+C to stop the server.
echo.
%PYTHON_BOOTSTRAP% "main.py"
set "APP_EXIT=%ERRORLEVEL%"

echo.
echo Workshop Porter stopped with exit code %APP_EXIT%.
pause
exit /b %APP_EXIT%

:failed
echo.
echo ERROR: Setup failed. Review the messages above.
pause
exit /b 1
