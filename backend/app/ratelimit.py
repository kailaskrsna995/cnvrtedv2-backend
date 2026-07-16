"""
RATE LIMITING (slowapi)
=======================
Shared limiter, imported by both main.py (to attach + install the 429 handler) and the
auth routes (to decorate /login and /register). Lives in its own module so main.py and
auth_route.py can both import it without a circular import.

Keyed by the real CLIENT IP. Railway sits behind a proxy, so request.client.host is the
proxy's address — every user would share one bucket. We read the first hop of
X-Forwarded-For instead (the originating client), falling back to the socket address.

Storage is in-memory, which is correct for our SINGLE Railway replica. If we ever run
multiple replicas, this needs a Redis backend (each replica would otherwise count
independently). Not a concern at current scale.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # "client, proxy1, proxy2" — the first entry is the originating client.
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
