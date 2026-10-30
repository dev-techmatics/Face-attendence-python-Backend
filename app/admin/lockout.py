"""Slows down guessing at the dashboard password.

The password is typed by a person every day, so it is short enough to be typeable — which
means it is also short enough to guess, given unlimited attempts. Everybody in the office is
on the network that can reach this page, so "only the office can see it" is not on its own a
defence against somebody working through a word list.

A fixed number of tries, then a pause. Counted per source address, so one person fumbling
their password does not lock out the rest of the building.

In-process and therefore per-instance, like the attendance limiter: this service runs as one
process, and if that ever changes the counters belong in a shared store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Tries before the pause starts. Generous enough for a genuine typo, small enough that a
#: word list is hopeless.
MAX_ATTEMPTS = 5

#: How long the pause lasts, and how long a quiet period has to be before the count resets.
LOCKOUT_SECONDS = 15 * 60


@dataclass
class _Attempts:
    count: int = 0
    first_at: float = field(default_factory=time.monotonic)
    locked_until: float = 0.0


_by_client: dict[str, _Attempts] = {}


def seconds_remaining(client: str) -> int:
    """How long this address must wait, or 0 if it may try now."""
    record = _by_client.get(client)

    if record is None or record.locked_until == 0.0:
        return 0

    remaining = record.locked_until - time.monotonic()

    if remaining <= 0:
        # The pause is over. Start them clean rather than leaving them one try from another
        # lockout, which would be indistinguishable from the password being wrong.
        _by_client.pop(client, None)
        return 0

    return int(remaining) + 1


def record_failure(client: str) -> int:
    """Count a wrong password. Returns the seconds to wait, or 0 if they may try again."""
    record = _by_client.get(client)
    now = time.monotonic()

    if record is None or now - record.first_at > LOCKOUT_SECONDS:
        record = _Attempts()
        _by_client[client] = record

    record.count += 1

    if record.count >= MAX_ATTEMPTS:
        record.locked_until = now + LOCKOUT_SECONDS
        return LOCKOUT_SECONDS

    return 0


def record_success(client: str) -> None:
    """A correct password clears the count: the person is who they said they were."""
    _by_client.pop(client, None)


def reset() -> None:
    """Drop every counter. Used by the tests."""
    _by_client.clear()
