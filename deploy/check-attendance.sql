-- Checking the dashboard's figures against the database directly.
--
--   sqlcmd -S .\SQLEXPRESS -d FaceAttendance -E -i deploy\check-attendance.sql
--
-- Times are stored UTC. The dashboard shows them in this machine's timezone, so every query
-- below converts with AT TIME ZONE - compare UTC against the screen and they will look five
-- and a half hours apart when nothing is wrong.
--
-- Decision 1 = Accepted. Everything else is a failed attempt, kept on purpose.
-- Direction 1 = In, 2 = Out.

DECLARE @zone   sysname = 'India Standard Time';
DECLARE @tenant nvarchar(64) = 'default';

-- Change these to the period you want. Defaults to today.
DECLARE @from date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE @zone AS date);
DECLARE @to   date = CAST(SYSDATETIMEOFFSET() AT TIME ZONE @zone AS date);


-- 1. Every punch, the way the "Every attempt" tab shows it ---------------------------------
PRINT '';
PRINT '=== every attempt ===';

SELECT
    CONVERT(varchar(16), e.CreatedAtUtc AT TIME ZONE @zone, 120) AS [When],
    p.FullName                                                   AS [Name],
    p.ExternalId                                                 AS [ID],
    CASE e.Direction WHEN 1 THEN 'In' WHEN 2 THEN 'Out' ELSE '?' END AS [In/Out],
    CASE e.Decision
        WHEN 1 THEN 'Recorded'
        WHEN 2 THEN 'Face did not match'
        WHEN 3 THEN 'Ignored, punched twice'
        WHEN 4 THEN 'Not registered'
        WHEN 5 THEN 'No face in photo'
        WHEN 6 THEN 'Photo too dark or blurry'
        WHEN 7 THEN 'Suspected photo of a photo'
        WHEN 8 THEN 'Face service error'
        WHEN 9 THEN 'Too soon to check out'
        ELSE CAST(e.Decision AS varchar(4))
    END                                                          AS [Result],
    e.DeviceId                                                   AS [Device]
FROM dbo.AttendanceEvents e
LEFT JOIN dbo.People p ON p.Id = e.PersonId
WHERE e.TenantId = @tenant
  AND CAST(e.CreatedAtUtc AT TIME ZONE @zone AS date) BETWEEN @from AND @to
ORDER BY e.CreatedAtUtc DESC;


-- 2. One line per person per day, the way the "In and out per day" tab shows it ------------
-- Each Out is paired with the In immediately before it, so a lunch break that was punched
-- out and back in is not counted as time at work. An unpaired punch contributes no hours
-- rather than an invented end time, which is why Hours can be 0 while InAt has a value.
PRINT '';
PRINT '=== in and out per day ===';

WITH punches AS (
    SELECT
        e.PersonId,
        p.ExternalId,
        p.FullName,
        e.Direction,
        CAST(e.CreatedAtUtc AT TIME ZONE @zone AS datetime2(0)) AS LocalAt
    FROM dbo.AttendanceEvents e
    JOIN dbo.People p ON p.Id = e.PersonId
    WHERE e.TenantId = @tenant
      AND e.Decision = 1
),
sequenced AS (
    SELECT
        PersonId, ExternalId, FullName, Direction, LocalAt,
        LAG(LocalAt)   OVER (PARTITION BY PersonId ORDER BY LocalAt) AS PrevAt,
        LAG(Direction) OVER (PARTITION BY PersonId ORDER BY LocalAt) AS PrevDirection
    FROM punches
),
worked AS (
    SELECT
        PersonId,
        CAST(LocalAt AS date) AS WorkDay,
        SUM(DATEDIFF(SECOND, PrevAt, LocalAt)) AS Seconds
    FROM sequenced
    WHERE Direction = 2 AND PrevDirection = 1
    GROUP BY PersonId, CAST(LocalAt AS date)
),
edges AS (
    SELECT
        PersonId, ExternalId, FullName,
        CAST(LocalAt AS date) AS WorkDay,
        MIN(CASE WHEN Direction = 1 THEN LocalAt END) AS FirstIn,
        MAX(CASE WHEN Direction = 2 THEN LocalAt END) AS LastOut,
        COUNT(*)                                      AS Punches
    FROM punches
    GROUP BY PersonId, ExternalId, FullName, CAST(LocalAt AS date)
)
SELECT
    edges.WorkDay                                  AS [Day],
    edges.FullName                                 AS [Name],
    edges.ExternalId                               AS [ID],
    CONVERT(varchar(5), edges.FirstIn, 108)        AS [In],
    ISNULL(CONVERT(varchar(5), edges.LastOut, 108), '--') AS [Out],
    CAST(ISNULL(worked.Seconds, 0) / 3600.0 AS decimal(6, 2)) AS [Hours],
    edges.Punches                                  AS [Punches],
    -- A rougher rule than the dashboard's: an odd number of punches means somebody forgot
    -- one half, which is the usual case. The dashboard also flags the unusual sequences
    -- (two Ins in a row, an Out with no In), so on a strange day the two can disagree -
    -- trust the dashboard, and look at the punches above to see what happened.
    CASE
        WHEN edges.Punches % 2 = 1 THEN 'punch missing'
        ELSE ''
    END                                            AS [Note]
FROM edges
LEFT JOIN worked
       ON worked.PersonId = edges.PersonId
      AND worked.WorkDay  = edges.WorkDay
WHERE edges.WorkDay BETWEEN @from AND @to
ORDER BY edges.WorkDay DESC, edges.FullName;


-- 3. Who is on the system -----------------------------------------------------------------
-- Status 1 = Active, 3 = Archived (what "Remove" on the People page does).
-- EnrollmentStatus 2 = Enrolled, and only an Active+Enrolled person can clock in.
PRINT '';
PRINT '=== people ===';

SELECT
    p.ExternalId AS [ID],
    p.FullName   AS [Name],
    p.Department AS [Department],
    CASE p.Status WHEN 1 THEN 'Active' WHEN 2 THEN 'Suspended' WHEN 3 THEN 'Removed'
         ELSE CAST(p.Status AS varchar(4)) END AS [Status],
    CASE p.EnrollmentStatus WHEN 0 THEN 'No photos yet' WHEN 1 THEN 'Part registered'
         WHEN 2 THEN 'Registered' WHEN 3 THEN 'Needs registering again'
         ELSE CAST(p.EnrollmentStatus AS varchar(4)) END AS [Registration],
    (SELECT COUNT(*) FROM dbo.FaceTemplates f WHERE f.PersonId = p.Id) AS [Photos],
    CONVERT(varchar(16), p.EnrolledAtUtc AT TIME ZONE @zone, 120)      AS [Registered]
FROM dbo.People p
WHERE p.TenantId = @tenant
ORDER BY p.FullName;


-- 4. Is the kiosk clock drifting from the server clock? ------------------------------------
-- CapturedAtUtc is the time the phone says it took the photo; CreatedAtUtc is when this
-- server recorded it, and that is the one every screen shows. They should agree within a
-- second or two. A large gap means a kiosk clock is wrong, or punches are being queued
-- offline and sent later - in which case the screens are showing the wrong time and the
-- real one is in CapturedAtUtc.
PRINT '';
PRINT '=== phone clock vs server clock ===';

SELECT
    COUNT(*)                                                   AS [Punches],
    MIN(DATEDIFF(SECOND, CapturedAtUtc, CreatedAtUtc))         AS [Smallest gap, s],
    MAX(DATEDIFF(SECOND, CapturedAtUtc, CreatedAtUtc))         AS [Largest gap, s],
    SUM(CASE WHEN ABS(DATEDIFF(SECOND, CapturedAtUtc, CreatedAtUtc)) > 60
             THEN 1 ELSE 0 END)                                AS [Over a minute out]
FROM dbo.AttendanceEvents
WHERE TenantId = @tenant;
