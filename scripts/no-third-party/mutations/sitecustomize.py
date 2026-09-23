"""A deliberate leak, for scripts/no-third-party-check.sh --mutate api only. Never shipped.

Python imports sitecustomize at startup, so mounting this into the service's site-packages
makes every POST it handles -- a create -- also send an HTTP request to 198.51.100.7, the way a
shortener that fetches its target's title would. 198.51.100.0/24 is TEST-NET-2 (RFC 5737):
the request reaches nobody. The check must fail and name that address.
"""

import threading
import urllib.request

try:
    import fastapi
except ImportError:  # the healthcheck runs this Python too, which is harmless; nothing to patch
    fastapi = None


def _fetch():
    try:
        urllib.request.urlopen("http://198.51.100.7/linkling-mutation", timeout=2)
    except Exception:
        pass


if fastapi is not None:
    _original = fastapi.FastAPI.__call__

    async def _call(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") == "POST":
            threading.Thread(target=_fetch, daemon=True).start()
        await _original(self, scope, receive, send)

    fastapi.FastAPI.__call__ = _call
