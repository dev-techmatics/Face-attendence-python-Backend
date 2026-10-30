"""The LAN-only guard.

Biometric data must not leave the office. On this deployment Windows Firewall is disabled on
the office adapter, so this middleware is the control that actually enforces it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.trusted_network import TrustedNetworkMiddleware

pytestmark = pytest.mark.asyncio


def build(networks: list[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/api/v1/persons")
    async def persons() -> dict[str, str]:
        return {"items": "secret"}

    app.add_middleware(TrustedNetworkMiddleware, networks=networks)
    return app


async def request(app: FastAPI, path: str, client_host: str):
    transport = ASGITransport(app=app, client=(client_host, 51234))

    async with AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get(path)


async def test_an_address_inside_the_range_is_allowed():
    app = build(["172.24.61.0/24"])

    response = await request(app, "/api/v1/persons", "172.24.61.50")

    assert response.status_code == 200


async def test_an_address_outside_the_range_is_refused():
    app = build(["172.24.61.0/24"])

    response = await request(app, "/api/v1/persons", "203.0.113.7")

    assert response.status_code == 403
    assert response.json()["code"] == "untrusted_network"
    # The refusal must not say what the allowlist contains.
    assert "172.24" not in response.text


async def test_health_answers_from_anywhere():
    # A monitoring probe must not need to be in the allowlist, and /health leaks nothing.
    app = build(["172.24.61.0/24"])

    response = await request(app, "/health", "203.0.113.7")

    assert response.status_code == 200


async def test_an_ipv4_mapped_v6_client_is_matched_as_v4():
    # A v4 client on a dual-stack socket arrives as ::ffff:172.24.61.50; comparing it verbatim
    # would refuse every LAN device.
    app = build(["172.24.61.0/24"])

    response = await request(app, "/api/v1/persons", "::ffff:172.24.61.50")

    assert response.status_code == 200


async def test_loopback_must_be_listed_explicitly():
    app = build(["172.24.61.0/24"])

    assert (await request(app, "/api/v1/persons", "127.0.0.1")).status_code == 403

    allowed = build(["172.24.61.0/24", "127.0.0.1/32"])

    assert (await request(allowed, "/api/v1/persons", "127.0.0.1")).status_code == 200


async def test_an_empty_list_disables_the_check():
    app = build([])

    assert (await request(app, "/api/v1/persons", "203.0.113.7")).status_code == 200


async def test_a_host_address_with_a_prefix_is_read_as_its_network():
    # An operator reading the machine's own address off ipconfig writes 172.24.61.79/24.
    app = build(["172.24.61.79/24"])

    assert (await request(app, "/api/v1/persons", "172.24.61.12")).status_code == 200


async def test_an_unparseable_entry_is_ignored_not_fatal():
    # A typo in the environment file must not take the service down, but it also must not
    # silently widen the allowlist.
    app = build(["not-a-network", "172.24.61.0/24"])

    assert (await request(app, "/api/v1/persons", "172.24.61.12")).status_code == 200
    assert (await request(app, "/api/v1/persons", "203.0.113.7")).status_code == 403
