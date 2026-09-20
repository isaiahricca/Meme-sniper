@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Meme Sniper V0.7.3

echo ============================================================
echo                 MEME SNIPER V0.7.3
echo ============================================================
echo.
echo Working folder:
echo %CD%
echo.

rem Keep the Python environment OUTSIDE Desktop/OneDrive.
rem This prevents OneDrive / protected-folder issues and works no matter
rem where this folder is moved.
set "VENV=%LOCALAPPDATA%\MemeSniper\v073_venv"
set "MARKER=%VENV%\.meme_sniper_v073_ready"

rem --- Find Python 3.12 ---
set "USE_PY=0"
py -3.12 --version >nul 2>&1
if not errorlevel 1 set "USE_PY=1"

if "%USE_PY%"=="0" (
    python --version >nul 2>&1
    if errorlevel 1 goto :NO_PYTHON
)

rem --- Require the user's real config ---
if not exist ".env" goto :NO_ENV

rem --- Require the existing research database for this migration ---
if not exist "memesniper.db" goto :NO_DATABASE

rem --- Create a local venv if needed ---
if not exist "%VENV%\Scripts\python.exe" (
    echo [1/3] Creating private Python environment...
    if not exist "%LOCALAPPDATA%\MemeSniper" mkdir "%LOCALAPPDATA%\MemeSniper" >nul 2>&1
    if "%USE_PY%"=="1" (
        py -3.12 -m venv "%VENV%"
    ) else (
        python -m venv "%VENV%"
    )
    if errorlevel 1 goto :VENV_FAILED
) else (
    echo [1/3] Python environment found.
)

rem --- Install dependencies once for this release ---
if not exist "%MARKER%" (
    echo [2/3] Installing Meme Sniper dependencies. First run can take a few minutes...
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :PIP_FAILED
    "%VENV%\Scripts\python.exe" -m pip install -r "%CD%\requirements.txt"
    if errorlevel 1 goto :PIP_FAILED
    "%VENV%\Scripts\python.exe" -c "import tzdata, fastapi, sqlalchemy, httpx" >nul 2>&1
    if errorlevel 1 goto :PIP_FAILED
    echo ready>"%MARKER%"
) else (
    echo [2/3] Dependencies already installed.
)

echo [3/3] Starting Meme Sniper V0.7.3...
echo.
echo Dashboard: http://127.0.0.1:8000
echo.
echo IMPORTANT: Keep this window open while running locally.
echo Press Ctrl+C to stop Meme Sniper cleanly.
echo ============================================================
echo.
"%VENV%\Scripts\python.exe" -m app.main
set "APP_ERROR=%ERRORLEVEL%"
echo.
if not "%APP_ERROR%"=="0" (
    echo Meme Sniper stopped with error code %APP_ERROR%.
    echo Take a screenshot of the error above if you need help.
) else (
    echo Meme Sniper stopped.
)
pause
exit /b %APP_ERROR%

:NO_ENV
echo ERROR: Your real .env file is missing.
echo.
echo Copy the .env file from your OLD working Meme Sniper folder
echo into this folder, next to START_MEME_SNIPER.bat.
echo Do NOT rename .env.example.
echo.
pause
exit /b 2

:NO_DATABASE
echo ERROR: memesniper.db is missing.
echo.
echo Copy your existing memesniper.db from the OLD working folder
echo into this folder. Windows may display it simply as "memesniper".
echo Stop the old bot with Ctrl+C before copying the database.
echo.
pause
exit /b 3

:NO_PYTHON
echo ERROR: Python was not found.
echo Meme Sniper expects Python 3.12. Your previous installation used 3.12.10.
echo.
pause
exit /b 4

:VENV_FAILED
echo.
echo ERROR: Could not create the private Python environment at:
echo %VENV%
echo.
echo This launcher deliberately avoids Desktop/OneDrive for the environment,
echo so you should NOT get the old C:\Windows\System32 access-denied problem.
echo.
pause
exit /b 5

:PIP_FAILED
echo.
echo ERROR: Dependency installation failed.
echo Check the messages above. The window will stay open so the error is visible.
echo.
pause
exit /b 6
