"""A deliberate leak, for scripts/no-third-party-check.sh --mutate api only. Never shipped.

Python imports sitecustomize at startup, so mounting this into the service's site-packages
makes every create also fetch the link's target, the way a shortener that shows a page title
would. It is the careful version: it resolves the target's host and fetches only a public
address, the usual guard against being pointed at internal ones. That guard is why the check's
target is a host name and not an address. Pointed at a reserved address, a guarded fetcher
sends nothing and the check would pass. Given a name, it must look the name up first, and that
lookup is what the check has to catch and name. The check's target host ends in `.invalid`, so
the lookup finds nothing and nothing is ever fetched.
"""

import ipaddress
import json
import socket
import threading
import urllib.request
from urllib.parse import urlsplit

try:
    import fastapi
except ImportError:  # the healthcheck runs this Python too, which is harmless; nothing to patch
    fastapi = None


def _fetch_title(body: bytes) -> None:
    try:
        url = json.loads(body)["url"]
        host = urlsplit(url).hostname
        for *_, sockaddr in socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP):
            if ipaddress.ip_address(sockaddr[0]).is_global:
                urllib.request.urlopen(url, timeout=2)
                return
    except Exception:
        pass


if fastapi is not None:
    _original = fastapi.FastAPI.__call__

    async def _call(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await _original(self, scope, receive, send)
        chunks = []

        async def receive_and_keep():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    threading.Thread(target=_fetch_title, args=(b"".join(chunks),), daemon=True).start()
            return message

        await _original(self, scope, receive_and_keep, send)

    fastapi.FastAPI.__call__ = _call
