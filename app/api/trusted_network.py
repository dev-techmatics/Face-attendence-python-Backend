"""Refuses requests from outside the configured networks.

Biometric data must not leave the office, so this service is meant to answer only to the
company LAN. The obvious place for that rule is the firewall — but on this deployment Windows
Firewall is disabled on the office adapter, and the service runs without the privileges needed
to add a rule. So the check also lives here, in the application, where it cannot be forgotten.

Belt and braces on purpose: keep the firewall rule *and* this list. A middleware cannot protect
you from a port that should never have been exposed, and a firewall cannot help once a request
is already inside the process.

``trusted_networks`` empty disables the check, which is the right default for a laptop running
the test suite and the wrong one for a deployment — so `FA_TRUSTED_NETWORKS` is set explicitly
in the production environment file.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

#: Paths answered regardless of origin. A monitoring probe on the loopback interface must not
#: need to be in the allowlist, and /health leaks nothing an attacker could use.
ALWAYS_ALLOWED = ("/health", "/v1/health")


class TrustedNetworkMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, networks: Sequence[str]) -> None:
        super().__init__(app)
        self._networks = _parse(networks)

        if self._networks:
            logger.info(
                "Restricting access to %s",
                ", ".join(str(n) for n in self._networks),
            )
        else:
            logger.warning(
                "No trusted networks configured, so every source address is accepted. Set "
                "FA_TRUSTED_NETWORKS before exposing this service to a network."
            )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self._networks or request.url.path in ALWAYS_ALLOWED:
            return await call_next(request)

        client = request.client.host if request.client else None
        address = _coerce(client)

        if address is None or not any(address in network for network in self._networks):
            # Logged at warning: on a LAN-only service, an outside request is worth noticing.
            logger.warning("Refused %s %s from %s", request.method, request.url.path, client)

            # No detail about what the allowlist contains, and a 403 rather than a 404 so an
            # operator reading the logs can tell this apart from a wrong URL.
            return JSONResponse(
                status_code=403,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "Not reachable from this network",
                    "status": 403,
                    "detail": "This service only answers requests from the company network.",
                    "code": "untrusted_network",
                    "hints": [],
                },
            )

        return await call_next(request)


def _parse(networks: Sequence[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

    for entry in networks:
        text = (entry or "").strip()

        if not text:
            continue

        try:
            # strict=False so "172.24.61.79/24" is accepted as the /24 containing it, which is
            # how an operator reading the machine's own address off ipconfig will write it.
            parsed.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            logger.error("Ignoring unparseable trusted network %r", entry)

    return parsed


def _coerce(client: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not client:
        return None

    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return None

    # A v4 client arriving on a dual-stack socket shows up as ::ffff:172.24.61.79; compare it
    # as the v4 address it actually is, or every LAN client would be refused.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped

    return address
