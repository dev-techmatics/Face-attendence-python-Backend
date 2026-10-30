@echo off
REM Removes the autostart task. Does not stop a running server; use stop-server.cmd for that.
schtasks /Delete /TN "FaceAttendanceBackend" /F
