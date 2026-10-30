@echo off
REM Stops whatever is listening on 8443. Narrow on purpose: it will not touch the CompreFace
REM stack on 443 or anything else on this machine.
setlocal enabledelayedexpansion
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"LISTENING" ^| findstr ":8443"') do (
    echo Stopping pid %%p
    taskkill /PID %%p /F
)
endlocal
