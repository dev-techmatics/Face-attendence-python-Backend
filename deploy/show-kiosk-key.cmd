@echo off
REM Shows the kiosk (device) key and copies it to the clipboard, so it can be pasted into the
REM app's configuration without being typed out or pasted into a chat window.
REM
REM The device key can only mark attendance. It cannot enrol a face or read a stored photo,
REM which is exactly why it is the one that belongs on a phone.

setlocal
"E:\Fa-py\.venv\Scripts\python.exe" -c "import json,re,pathlib,subprocess; env=pathlib.Path(r'E:\Fa-py\.env.production').read_text(encoding='utf-8'); k=json.loads(re.search(r'FA_API_KEYS__DEVICE=(\[.*\])',env).group(1))[0]; print(); print('  KIOSK (device) KEY'); print('  ' + '-'*len(k)); print('  ' + k); print(); print('  Copied to the clipboard.'); subprocess.run('clip', input=k.encode(), shell=True)"
echo.
pause
