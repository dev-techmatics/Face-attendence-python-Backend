"""Domain enumerations.

The numeric values are persisted, so never renumber them once data exists.
"""

from __future__ import annotations

from enum import IntEnum


class NamedIntEnum(IntEnum):
    """An IntEnum that also accepts its member name, case-insensitively.

    The numbers are what the database stores, but configuration files, query strings and form
    fields all say ``"Front"`` rather than ``1`` - as they did against the original service,
    whose model binder resolved enum names. Keeping that here means no caller has to change.
    """

    @classmethod
    def _missing_(cls, value: object) -> "NamedIntEnum | None":
        if isinstance(value, str):
            text = value.strip()

            for member in cls:
                if member.name.lower() == text.lower():
                    return member

            if text.lstrip("+-").isdigit():
                return cls(int(text))

        return None


class FacePose(NamedIntEnum):
    """The three capture angles collected during registration."""

    Front = 1
    Left = 2
    Right = 3


class PersonStatus(NamedIntEnum):
    Active = 1
    Suspended = 2
    Archived = 3


class EnrollmentStatus(NamedIntEnum):
    #: Person record exists, no usable face captured yet.
    NotEnrolled = 0

    #: Some but not all required poses captured.
    InProgress = 1

    #: All required poses captured and cross-validated. Ready for attendance.
    Enrolled = 2

    #: Templates exist but were produced by a provider/model that is no longer active.
    Stale = 3


class AttendanceDirection(NamedIntEnum):
    In = 1
    Out = 2


class AttendanceDecision(NamedIntEnum):
    """Outcome of an attendance attempt.

    Every attempt is persisted, including failures, because the failures are what you
    need when someone disputes a missing punch.
    """

    Accepted = 1
    Rejected = 2
    Duplicate = 3
    NotEnrolled = 4
    NoFaceDetected = 5
    LowQuality = 6
    SpoofSuspected = 7
    ProviderError = 8

    #: The face matched, but checking out this soon after checking in is not allowed. Kept
    #: distinct from Rejected, which means the face did not match: telling somebody their
    #: face was not recognised when the real answer is "not yet" sends them to re-register
    #: for no reason, and buries a policy refusal among the recognition failures on the
    #: Problems screen.
    TooSoon = 9

    #: They had already checked out, and their face appeared again soon after - almost
    #: always somebody walking back past the kiosk rather than starting a second shift.
    #: Nothing is recorded as attendance.
    #:
    #: Its own outcome rather than Duplicate, for two reasons. The screen needs to say
    #: something different ("you are done" rather than "that was already recorded"), and
    #: counting it separately on the Problems page answers a question Duplicate cannot: if
    #: this fires twenty times a week, the kiosk is somewhere people walk back through.
    AlreadyCheckedOut = 10


class FaceTemplateKind(NamedIntEnum):
    #: A numeric vector we can compare ourselves, in-process.
    Embedding = 1

    #: An opaque handle; only the provider can compare it.
    ProviderManaged = 2
