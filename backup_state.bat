@echo off
setlocal
cd /d "%~dp0"
if not exist backups mkdir backups
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set stamp=%%i
if exist .env copy /Y .env "backups\.env_%stamp%.backup" >nul
if exist memesniper.db copy /Y memesniper.db "backups\memesniper_%stamp%.db" >nul
echo.
echo Meme Sniper state backup complete: backups\%stamp%
echo Your .env/API keys and SQLite database were copied locally only.
pause
