"""Lists what a page the service serves would make a browser load, for no-third-party-check.sh's
scan of the service's own /-/stats page (LL-025).

Usage: loads.py html|headers  < what was served

Prints one finding per line and exits 0 whether or not it found anything: a non-zero status is
this script failing to run, which the check calls blind. Exits 64 for a mode it does not know.

The site's pages get a stricter scan, in the check itself: any absolute URL, anywhere, fails
(that is deliberate there, "until someone adds a reviewed exception"). The stats page cannot
take that scan, because it prints every link's target as text and a target is a URL. So this
reads the page as HTML and flags what a browser would go and fetch, or run:

  * any <script>, and any inline event handler on any tag (which can fetch() with no <script>);
  * an absolute or protocol-relative URL in a URL-bearing attribute (src, srcset, href, data,
    poster, ...) of any tag except <a>, <area> and <form>, which a reader has to act on before
    anything is fetched. `https:/host` and `\\\\host` count: a browser reads them as another
    origin (the WHATWG URL standard treats \\ as / in http(s) URLs);
  * `<meta http-equiv=refresh>` naming such a URL, which fires by itself;
  * a url() or @import to another origin in a <style> or a style="" attribute.

Text is never flagged, in an element or an attribute that fetches nothing (alt, title). An
<a href> to another origin is not flagged either: nothing is fetched until someone follows it.
That is looser than the site's scan on purpose, and it is the whole of what this one lets
through.

For `headers`, the raw response headers as curl's -D wrote them: any absolute URL fails, as it
does for the site (a `Link: <https://...>; rel=preload` header is a load, and `Location:` is
another origin's URL in a response nobody asked to be sent away).
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

# The start of an absolute or protocol-relative URL inside a longer value: at its start or after
# something that separates one URL from the next (space, comma in a srcset, `=` in a refresh's
# `url=`, a quote or bracket around it). A scheme takes any run of slashes and backslashes after
# it, and two on their own are protocol-relative. `[a-z0-9]` after them keeps `https:` alone, and
# `a//b` in the middle of a word, from counting.
_EXTERNAL = re.compile(r"""(?:^|[\s,;=('"<])(?:https?:[/\\]*|[/\\]{2})[a-z0-9]""", re.IGNORECASE)

# `url(` or `@import` followed by one, as CSS spells a load.
_CSS_LOAD = re.compile(
    r"""(?:url\(\s*|@import\s*(?:url\(\s*)?)["']?\s*(?:https?:[/\\]*|[/\\]{2})[a-z0-9]""",
    re.IGNORECASE,
)

# Attributes a browser fetches from. `content` is only one on <meta http-equiv=refresh>, and
# `style` is CSS, both handled apart.
_URL_ATTRS = frozenset(
    {
        "src", "srcset", "imagesrcset", "href", "xlink:href", "data", "poster", "background",
        "manifest", "codebase", "archive", "lowsrc", "dynsrc", "icon",
    }
)  # fmt: skip

# Tags whose URLs are followed by the reader, not by the page.
_NAVIGATION = frozenset({"a", "area", "form"})

_SHOWN = 120


def _shown(value: str) -> str:
    """One line, and short, with the start of the value (the host) kept."""
    value = " ".join(value.split())
    return value if len(value) <= _SHOWN else value[:_SHOWN] + "..."


class _Loads(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []
        self._style_open = False

    def handle_starttag(self, tag, attrs):
        self._style_open = tag == "style"
        attrs = [(name.lower(), value or "") for name, value in attrs]
        if tag == "script":
            self.found.append("a <script>")
        refresh = tag == "meta" and any(
            n == "http-equiv" and v.strip().lower() == "refresh" for n, v in attrs
        )
        for name, value in attrs:
            if name.startswith("on") and name[2:].isalpha():
                self.found.append(f'<{tag} {name}="{_shown(value)}"> is an inline event handler')
            elif name == "style" and _CSS_LOAD.search(value):
                self.found.append(f'<{tag} style="{_shown(value)}"> loads from another origin')
            elif tag in _NAVIGATION:
                continue
            elif (name in _URL_ATTRS or (refresh and name == "content")) and _EXTERNAL.search(value):
                self.found.append(f'<{tag} {name}="{_shown(value)}"> loads from another origin')

    def handle_endtag(self, tag):
        if tag == "style":
            self._style_open = False

    def handle_data(self, data):
        if self._style_open and _CSS_LOAD.search(data):
            self.found.append(f"<style> loads from another origin: {_shown(data)}")


def scan_html(text: str) -> list[str]:
    parser = _Loads()
    parser.feed(text)
    parser.close()
    return parser.found


def scan_headers(text: str) -> list[str]:
    return [
        f"header {_shown(line)} names another origin"
        for line in text.splitlines()
        if _EXTERNAL.search(line)
    ]


def main(argv: list[str]) -> int:
    scans = {"html": scan_html, "headers": scan_headers}
    if len(argv) != 2 or argv[1] not in scans:
        print(f"usage: {argv[0]} html|headers  < what was served", file=sys.stderr)
        return 64
    for finding in scans[argv[1]](sys.stdin.read()):
        print(finding)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
