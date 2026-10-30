@echo off
REM Face Attendance backend - production launcher.
REM
REM Started by the scheduled task (deploy\install-autostart.cmd) and usable by hand for a
REM foreground run. Logs go to data\logs\, which is gitignored.

setlocal

set "ROOT=E:\Fa-py"
cd /d "%ROOT%" || exit /b 1

REM Points pydantic-settings and alembic at the same file, so the service and the migrations
REM can never disagree about which database they mean.
set "FA_ENV_FILE=.env.production"

if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

REM Bind to every interface: the office LAN reaches it on 172.24.61.79 and the health check
REM uses loopback. Who may actually talk to it is decided by FA_TRUSTED_NETWORKS, because
REM Windows Firewall is disabled on the office adapter.
"%ROOT%\.venv\Scripts\python.exe" -m uvicorn app.main:app ^
  --host 0.0.0.0 ^
  --port 8443 ^
  --ssl-keyfile "%ROOT%\deploy\certs\server.key" ^
  --ssl-certfile "%ROOT%\deploy\certs\server.crt" ^
  --workers 1 ^
  --log-level info ^
  --no-access-log ^
  >> "%ROOT%\data\logs\server.log" 2>&1

endlocal
