-- Lets the at-boot scheduled task reach the database.
--
--   sqlcmd -S .\SQLEXPRESS -d FaceAttendance -E -i deploy\grant-system-db-access.sql
--
-- Why this is needed at all. deploy\install-autostart.cmd, run elevated, registers a task
-- that runs as NT AUTHORITY\SYSTEM so the backend is up after an unattended reboot with
-- nobody logged in. The connection string uses Windows integrated auth and stores no
-- password, so the database identity is whatever account the task runs as - and SYSTEM is
-- not the account this database was created under. Measured 2026-11-10 on this instance:
--
--   * FaceAttendance is owned by QA-PLAYWRIGHT\ADMIN, and dbo is the only user in it.
--   * NT AUTHORITY\SYSTEM has a server login but is NOT in the sysadmin role, so it does
--     not map to dbo and cannot open the database at all.
--
-- Without this script the service starts, answers /v1/health, and then fails every request
-- that touches a person or a punch - which reads like a broken database rather than a
-- missing permission.
--
-- Reader + writer, not db_owner. With FA_DATABASE__MIGRATE_ON_STARTUP=false the running
-- service never issues DDL; schema changes go through alembic, run by a human as themselves.
-- A service account that cannot drop a table is worth the thirty seconds this takes.

SET NOCOUNT ON;

DECLARE @login sysname = N'NT AUTHORITY\SYSTEM';

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = @login)
BEGIN
    RAISERROR(N'There is no server login for NT AUTHORITY\SYSTEM on this instance. Create it first, or run the task as a named account instead.', 16, 1);
    RETURN;
END

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = @login)
BEGIN
    CREATE USER [NT AUTHORITY\SYSTEM] FOR LOGIN [NT AUTHORITY\SYSTEM];
    PRINT 'Created the database user.';
END
ELSE
    PRINT 'The database user already exists; leaving it as it is.';

ALTER ROLE db_datareader ADD MEMBER [NT AUTHORITY\SYSTEM];
ALTER ROLE db_datawriter ADD MEMBER [NT AUTHORITY\SYSTEM];
PRINT 'Granted read and write.';

-- Proof rather than assurance: this is what the service will be able to do.
PRINT '';
PRINT '--- what NT AUTHORITY\SYSTEM can now do in this database ---';

EXECUTE AS USER = N'NT AUTHORITY\SYSTEM';

SELECT
    USER_NAME()                                              AS [Acting as],
    IS_ROLEMEMBER('db_datareader')                           AS [Can read],
    IS_ROLEMEMBER('db_datawriter')                           AS [Can write],
    IS_ROLEMEMBER('db_owner')                                AS [Is owner],
    HAS_PERMS_BY_NAME('dbo.People', 'OBJECT', 'SELECT')      AS [Select on People],
    HAS_PERMS_BY_NAME('dbo.AttendanceEvents', 'OBJECT', 'INSERT') AS [Insert on AttendanceEvents];

REVERT;
