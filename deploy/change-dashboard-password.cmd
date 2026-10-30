@echo off
REM Change the password used to sign in to the HR dashboard at /admin.
REM Staff do not need a password - they sign in at /me with a photo.
"E:\Fa-py\.venv\Scripts\python.exe" "E:\Fa-py\deploy\set_password.py" %*
echo.
pause
