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
REM
REM After this, run deploy\install-watchdog.cmd: an at-boot task covers a reboot, not a
REM process that dies or wedges while the machine stays up.

setlocal
set "TASK=FaceAttendanceBackend"
set "CMD=E:\Fa-py\deploy\start-server.cmd"

REM Same instance and database as FA_DATABASE__URL in .env.production. Hardcoded the way
REM deploy\check-attendance.sql is: one box, one database, and a wrong guess here would be
REM worse than an obvious constant.
set "INSTANCE=.\SQLEXPRESS"
set "DB=FaceAttendance"

net session >nul 2>&1
if %errorlevel%==0 goto elevated

echo Not elevated: registering a per-user at-logon task.
echo The backend will NOT be up after an unattended reboot. Re-run this as
echo Administrator to fix that.
schtasks /Create /TN "%TASK%" /TR "\"%CMD%\"" /SC ONLOGON /F
goto report

:elevated
REM An at-boot task runs as SYSTEM, and the connection string uses Windows integrated auth
REM with no password stored - so SYSTEM is also the database login. On this instance
REM FaceAttendance is owned by QA-PLAYWRIGHT\ADMIN and SYSTEM is not a sysadmin, so without
REM a grant the service starts, answers /v1/health, and then fails every request that touches
REM a person or a punch. Checked before registering, because that failure reads like a broken
REM database rather than a missing permission and would cost somebody an afternoon.
where sqlcmd >nul 2>&1
if not %errorlevel%==0 (
    echo WARNING: sqlcmd was not found, so SYSTEM's database access could not be checked.
    echo          If attendance fails after the next reboot, run:
    echo            sqlcmd -S %INSTANCE% -d %DB% -E -i deploy\grant-system-db-access.sql
    goto register
)

sqlcmd -S "%INSTANCE%" -d "%DB%" -E -h -1 -W -Q "SET NOCOUNT ON; DECLARE @ok int = 0; IF IS_SRVROLEMEMBER('sysadmin', N'NT AUTHORITY\SYSTEM') = 1 SET @ok = 1; IF IS_ROLEMEMBER('db_datareader', N'NT AUTHORITY\SYSTEM') = 1 AND IS_ROLEMEMBER('db_datawriter', N'NT AUTHORITY\SYSTEM') = 1 SET @ok = 1; SELECT CASE WHEN @ok = 1 THEN 'SYSTEM-DB-OK' ELSE 'SYSTEM-DB-MISSING' END;" 2>nul | findstr /C:"SYSTEM-DB-OK" >nul

if %errorlevel%==0 goto register

echo.
echo NT AUTHORITY\SYSTEM cannot reach the %DB% database, so an at-boot task would start a
echo backend that cannot read or write attendance. Nothing has been registered.
echo.
echo Fix it with one command, then run this script again:
echo.
echo     sqlcmd -S %INSTANCE% -d %DB% -E -i E:\Fa-py\deploy\grant-system-db-access.sql
echo.
echo Or, if you would rather the task ran as you instead of SYSTEM, register it by hand with
echo /RU and /RP - that stores your Windows password in the task, which is why it is not the
echo default here.
endlocal
exit /b 1

:register
echo Elevated: registering an at-boot task that runs without anyone logged in.
schtasks /Create /TN "%TASK%" /TR "\"%CMD%\"" /SC ONSTART /RU "SYSTEM" /RL HIGHEST /F

:report
echo.
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /I "TaskName Status Logon Schedule"
endlocal
