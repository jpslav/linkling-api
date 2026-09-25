"""Lists the stylesheets one served page or stylesheet pulls in, for no-third-party-check.sh's crawl.

Usage: refs.py <html|css> <the URL it was served from>  < body

Prints one same-origin path per line, resolved against that URL the way a browser resolves it.
A reference to another origin is not printed: the crawl does not fetch it, and the scan of the
body it appears in reports it. An HTML parser, not a line grep, because a <link> split over
lines, `rel="preload stylesheet"`, and an @import inside a <style> are all ordinary markup.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

# `@import "a.css";`, `@import url(a.css)`, `@import"a.css"` -- the whitespace is optional.
_IMPORT = re.compile(r"""@import\s*(?:url\(\s*)?["']?([^"')\s;]+)""", re.IGNORECASE)


def css_refs(text: str) -> list[str]:
    return _IMPORT.findall(text)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "stylesheet" in (attrs.get("rel") or "").lower().split():
            if attrs.get("href"):
                self.refs.append(attrs["href"])
        self._in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.refs.extend(css_refs(data))


def same_origin_paths(kind: str, base: str, body: str) -> list[str]:
    if kind == "html":
        page = _Page()
        page.feed(body)
        page.close()
        refs = page.refs
    else:
        refs = css_refs(body)
    origin = urlsplit(base)[:2]
    paths = []
    for ref in refs:
        resolved = urlsplit(urljoin(base, ref.strip()))
        if resolved[:2] == origin:
            paths.append(resolved.path + (f"?{resolved.query}" if resolved.query else ""))
    return paths


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("html", "css"):
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 64
    for path in same_origin_paths(argv[1], argv[2], sys.stdin.read()):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
