@echo off
REM Registers the health watchdog to run every five minutes.
REM
REM Run this AS ADMINISTRATOR, after deploy\install-autostart.cmd. Both tasks must run as the
REM same account: the watchdog restarts the backend, so whatever identity it has is the
REM identity the backend inherits - and with Windows integrated auth that identity is also
REM the database login. A watchdog running as SYSTEM that restarts a backend you set up to
REM run as yourself would quietly swap the database account underneath you.
REM
REM Five minutes is a deliberate floor, not a maximum. The backend takes about five seconds
REM to load the face model, so a check every thirty seconds would spend its life racing a
REM start it triggered itself. Five minutes is also the most attendance anybody loses.

setlocal
set "TASK=FaceAttendanceWatchdog"
set "MAIN=FaceAttendanceBackend"
set "SCRIPT=E:\Fa-py\deploy\watchdog.ps1"

if not exist "%SCRIPT%" (
    echo %SCRIPT% is missing. Nothing registered.
    exit /b 1
)

net session >nul 2>&1
if not %errorlevel%==0 (
    echo Not elevated. Re-run this as Administrator: an at-logon watchdog cannot restart a
    echo backend that is supposed to be up with nobody logged in.
    exit /b 1
)

schtasks /Query /TN "%MAIN%" >nul 2>&1
if not %errorlevel%==0 (
    echo The %MAIN% task does not exist yet. Run deploy\install-autostart.cmd first, so the
    echo watchdog and the thing it restarts agree about which account they run as.
    exit /b 1
)

schtasks /Create /TN "%TASK%" ^
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%SCRIPT%\"" ^
    /SC MINUTE /MO 5 /RU "SYSTEM" /RL HIGHEST /F

if not %errorlevel%==0 (
    echo Could not register the task.
    exit /b 1
)

echo.
echo Registered. It writes only when something is wrong, so an empty log is the good case:
echo   E:\Fa-py\data\logs\watchdog.log
echo.
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /I "TaskName Status Schedule Next"
endlocal
