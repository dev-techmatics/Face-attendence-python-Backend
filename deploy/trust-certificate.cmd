@echo off
REM Makes Chrome (and Edge) stop warning about this server on THIS PC.
REM
REM What it does: adds E:\Fa-py\deploy\certs\server.crt to the trusted certificates for the
REM Windows account that runs it. No administrator rights needed, and it affects nobody else
REM on the machine.
REM
REM Why that is safe here: the certificate is self-signed and is NOT a certificate authority,
REM so it can only ever vouch for this one server. It cannot be used to impersonate a bank, a
REM webmail provider or anything else. A real CA certificate would be a different matter.
REM
REM Check you are trusting the right file before you run it. The thumbprint should be:
REM    86C8A13ECCEBDBE19416DB9B873D86A9735B6FD3
REM
REM To undo: run deploy\untrust-certificate.cmd, or open certmgr.msc ->
REM Trusted Root Certification Authorities -> Certificates and delete "face-attendance.local".

echo Installing the Face Attendance certificate for %USERNAME% only...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='E:\Fa-py\deploy\certs\server.crt';" ^
  "$c=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($p);" ^
  "Write-Host ('  subject    : ' + $c.Subject);" ^
  "Write-Host ('  thumbprint : ' + $c.Thumbprint);" ^
  "$s=New-Object System.Security.Cryptography.X509Certificates.X509Store('Root','CurrentUser');" ^
  "$s.Open('ReadWrite'); $s.Add($c); $s.Close();" ^
  "if (Get-ChildItem Cert:\CurrentUser\Root | Where-Object Thumbprint -eq $c.Thumbprint) { Write-Host ''; Write-Host '  Done. Close Chrome COMPLETELY and reopen it.' } else { Write-Host '  Failed.' }"

echo.
pause
