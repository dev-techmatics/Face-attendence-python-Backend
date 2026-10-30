"""Stops a browser keeping its own copy of the attendance pages.

Two reasons, either enough on its own. These pages are a live view: somebody who refreshes, or
who comes back with the back button, is asking what is true *now*, and a cached copy answers
what was true when they last looked — without any hint that it is doing so. And a page listing
who was in the building and when has no business sitting in a disk cache on a shared machine.

Only the pages. The kiosk API sends JSON that no browser caches, and the two routes that set
their own value (a CSV download, a stored photograph) are left as they are.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

#: The HR dashboard and the staff page.
PREFIXES = ("/admin", "/me")

#: ``no-store`` is the one that matters; the rest are for proxies too old to honour it.
HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


class NoStoreMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)

        if request.url.path.startswith(PREFIXES):
            for name, value in HEADERS.items():
                response.headers.setdefault(name, value)

        return response
