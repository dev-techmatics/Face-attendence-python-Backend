@echo off
REM Reverses trust-certificate.cmd: removes the Face Attendance certificate from this
REM Windows account's trusted certificates. The browser warning comes back afterwards.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t='86C8A13ECCEBDBE19416DB9B873D86A9735B6FD3';" ^
  "$f=Get-ChildItem Cert:\CurrentUser\Root | Where-Object Thumbprint -eq $t;" ^
  "if ($f) { $s=New-Object System.Security.Cryptography.X509Certificates.X509Store('Root','CurrentUser'); $s.Open('ReadWrite'); $s.Remove($f); $s.Close(); Write-Host '  Removed.' } else { Write-Host '  It was not installed.' }"

echo.
pause
