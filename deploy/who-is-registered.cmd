@echo off
REM Prints who is registered and today's attendance. Read-only - it cannot change anything.
REM Double-click it, or run it from a command prompt with extra options:
REM    who-is-registered.cmd --days 7
REM    who-is-registered.cmd --person TS4012
"E:\Fa-py\.venv\Scripts\python.exe" "E:\Fa-py\deploy\report.py" --today %*
echo.
pause
