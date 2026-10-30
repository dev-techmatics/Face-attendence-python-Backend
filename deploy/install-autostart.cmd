@echo off
REM Registers the backend to start automatically.
REM
REM Two variants, because they are not equivalent:
REM
REM   Run WITHOUT admin  -> a per-user task that fires at logon. Works, but the backend is
REM                         only up once someone signs in to this PC. Auto sign-in is off on
REM                         this machine and should stay off, so after an unattended reboot
REM                         the kiosks stay down until a person logs in.
REM
REM   Run AS ADMIN       -> a task that fires at boot and runs whether anyone is logged in or
REM                         not. This is what a production kiosk fleet needs.
REM
REM The task is called FaceAttendanceBackend either way, so re-running this as admin replaces
REM the per-user one.

setlocal
set "TASK=FaceAttendanceBackend"
set "CMD=E:\Fa-py\deploy\start-server.cmd"

net session >nul 2>&1
if %errorlevel%==0 (
    echo Elevated: registering an at-boot task that runs without anyone logged in.
    schtasks /Create /TN "%TASK%" /TR "\"%CMD%\"" /SC ONSTART /RU "SYSTEM" /RL HIGHEST /F
) else (
    echo Not elevated: registering a per-user at-logon task.
    echo The backend will NOT be up after an unattended reboot. Re-run this as
    echo Administrator to fix that.
    schtasks /Create /TN "%TASK%" /TR "\"%CMD%\"" /SC ONLOGON /F
)

echo.
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /I "TaskName Status Logon Schedule"
endlocal
