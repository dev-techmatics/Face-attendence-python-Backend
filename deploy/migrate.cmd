@echo off
REM Applies pending schema changes. Run this BEFORE restarting the service after a code
REM update - the service never migrates at startup, because two instances starting at once
REM would race each other through the same change.

setlocal
set "ROOT=E:\Fa-py"
cd /d "%ROOT%" || exit /b 1
set "FA_ENV_FILE=.env.production"
"%ROOT%\.venv\Scripts\python.exe" -m alembic upgrade head
endlocal
