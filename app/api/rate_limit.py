"""Fixed-window throttle for the attendance endpoint.

A kiosk submitting frames in a loop is the normal failure mode, and each call costs an
inference. Partitioned by device where one identifies itself, otherwise by remote address, so
one misbehaving kiosk cannot consume the budget for every other door.

In-process and therefore per-instance: behind a load balancer each instance enforces its own
window. Move the counters to Redis if you need a cluster-wide limit.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from app.api.deps import SettingsDep
from app.core.security import DEVICE_HEADER

WINDOW_SECONDS = 60

#: partition key -> (window start, count in that window)
_counters: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))


def _partition_key(request: Request) -> str:
    device = request.headers.get(DEVICE_HEADER)

    if device and device.strip():
        return f"device:{device.strip()}"

    if request.client and request.client.host:
        return f"ip:{request.client.host}"

    return "unknown"


async def attendance_rate_limit(request: Request, settings: SettingsDep) -> None:
    limit = settings.attendance_rate_limit_per_minute

    if limit <= 0:
        return

    key = _partition_key(request)
    now = time.monotonic()
    window_start, count = _counters[key]

    if now - window_start >= WINDOW_SECONDS:
        _counters[key] = (now, 1)
        return

    if count >= limit:
        retry_after = max(1, int(WINDOW_SECONDS - (now - window_start)))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"More than {limit} attendance requests in a minute from this device.",
            headers={"Retry-After": str(retry_after)},
        )

    _counters[key] = (window_start, count + 1)


def reset_rate_limits() -> None:
    """Drop every counter. Used by the tests."""
    _counters.clear()
