@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Python environment not found. Run setup_windows.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe tools\backup_sqlite.py memesniper.db cloud_seed\memesniper.db
if errorlevel 1 (
  echo Backup failed.
  pause
  exit /b 1
)
echo.
echo SAFE CLOUD COPY CREATED: cloud_seed\memesniper.db
echo This contains trading research history. It does NOT copy your .env/API keys.
pause
