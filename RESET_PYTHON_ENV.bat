@echo off
setlocal
set "VENV=%LOCALAPPDATA%\MemeSniper\v073_venv"
echo This deletes ONLY the generated V0.7.3 Python environment.
echo It does NOT touch your .env, database, or Meme Sniper files.
echo.
echo Target: %VENV%
echo.
choice /M "Reset the Python environment"
if errorlevel 2 exit /b 0
if exist "%VENV%" rmdir /s /q "%VENV%"
echo Reset complete. Double-click START_MEME_SNIPER.bat to rebuild it.
pause
