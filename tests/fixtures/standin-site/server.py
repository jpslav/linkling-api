"""A stand-in for `linkling-web`, so that the site half of scripts/no-third-party-check.sh runs in CI.

CI cannot read the private `linkling-web` (ADR-0015), so the check's crawl of the public site had
nothing to run against there. This serves what the crawl needs and nothing else: `/` and
`/privacy.html` as HTML, two same-origin stylesheets, and a 404. It is not the real site: it is
not nginx, so the shipped `deploy/nginx-privacy.conf` (mounted by compose.yaml) is inert here, and
it says nothing about what the real pages contain.

What it serves is fixed at build time by the one word in the `mode` file beside it, which the
Dockerfile copies to /mode. `none` is the clean site. Each other mode is a deliberate defect that
the check must catch, and scripts/no-third-party-standin.sh (run by CI) requires the check to end
red on it, in the way named here:

  external-stylesheet  `/` links a stylesheet on another origin. The check must `fail`, naming it.
  cut-short-reply      /second.css promises 500 bytes more than it sends and closes the
                       connection, as LL-025's stylesheet did, which used to make the crawl `pass`.
                       The check must be `blind`: it did not see the whole reply.
  stalled-reply        the same, but the connection is held open. Only the check's own time bound
                       (curl's --max-time) ends it. The check must be `blind`, after that bound.

The server resolves no name and opens no connection of its own: the check captures every packet
this container sends, and any DNS query fails it. `http.server` would look up its own host name
when it binds, so the bind below is done by hand.
"""

from __future__ import annotations

import http.server
import pathlib
import socketserver
import sys

MODES = ("none", "external-stylesheet", "cut-short-reply", "stalled-reply")

# A host that cannot resolve (RFC 2606), so that the link never reaches anybody even if a browser
# followed it. The check reads the markup and fetches nothing from it.
EXTERNAL_STYLESHEET = "https://styles.standin.invalid/site.css"

_HEAD = b'<!doctype html><html><head><meta charset="utf-8"><title>stand-in</title>'
PAGES = {
    "/": (
        "text/html; charset=utf-8",
        _HEAD + b'<link rel="stylesheet" href="/style.css">'
        b'<link rel="stylesheet" href="/second.css">{external}</head>'
        b'<body><a href="/privacy.html">Privacy</a></body></html>',
    ),
    "/privacy.html": (
        "text/html; charset=utf-8",
        _HEAD + b'<link rel="stylesheet" href="/style.css"></head><body><p>Privacy</p></body></html>',
    ),
    "/style.css": ("text/css; charset=utf-8", b"body{margin:0}"),
    "/second.css": ("text/css; charset=utf-8", b"main{margin:0}"),
}
# How many bytes more than it sends a cut-short reply promises.
SHORT_BY = 500
# What a stalled reply waits on: the client hanging up. A backstop only, so that a client that
# never does cannot keep a handler thread for ever.
STALL_SECONDS = 120


class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def handler_for(mode: str):
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; the modes are {', '.join(MODES)}")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            page = PAGES.get(self.path)
            if page is None:
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            ctype, body = page
            if b"{external}" in body:
                link = f'<link rel="stylesheet" href="{EXTERNAL_STYLESHEET}">' if mode == "external-stylesheet" else ""
                body = body.replace(b"{external}", link.encode())
            short = mode in ("stalled-reply",) and self.path == "/second.css"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body) + (SHORT_BY if short else 0)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            if short:
                if mode == "stalled-reply":
                    self.connection.settimeout(STALL_SECONDS)
                    try:
                        self.connection.recv(1)
                    except OSError:
                        pass
                self.close_connection = True

    return Handler


def make_server(mode: str, address: tuple[str, int]) -> Server:
    return Server(address, handler_for(mode))


if __name__ == "__main__":
    mode = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/mode").read_text().strip()
    make_server(mode, ("0.0.0.0", 80)).serve_forever()
