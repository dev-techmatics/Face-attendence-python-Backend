"""Header-based API key authentication, and tenant resolution.

Adequate for server-to-server and kiosk traffic over TLS; put a real identity provider in
front of it before exposing an admin UI to end users.
"""

from __future__ import annotations

import hmac
import logging

from app.core.config import ApiKeySettings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-Api-Key"
TENANT_HEADER = "X-Tenant-Id"
DEVICE_HEADER = "X-Device-Id"

ADMIN_ROLE = "admin"
DEVICE_ROLE = "device"


def match_api_key(keys: ApiKeySettings, presented: str) -> str | None:
    """Return the role the presented key grants, or None.

    Compares in fixed time. A plain equality check leaks key material through timing, which is
    cheap to avoid and awkward to explain after the fact.
    """
    for key in keys.admin:
        if _fixed_time_equals(presented, key):
            return ADMIN_ROLE

    for key in keys.device:
        if _fixed_time_equals(presented, key):
            return DEVICE_ROLE

    return None


def _fixed_time_equals(a: str, b: str) -> bool:
    if not b:
        return False

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def normalize_tenant_id(value: str) -> str:
    """Constrain a tenant id to a safe character set and length.

    It ends up in object storage keys, so an unsanitised tenant id would be a path-traversal
    vector.
    """
    trimmed = (value or "").strip()[:64]

    return "".join(
        c if (c.isascii() and c.isalnum()) or c in ("-", "_") else "_" for c in trimmed
    )
