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

import html
import re
import sys
from html.parser import HTMLParser

# The start of an absolute or protocol-relative URL inside a longer value: at its start or after
# something that separates one URL from the next (a control character or space, a comma in a
# srcset, `=` in a refresh's `url=`, a quote or bracket around it). A scheme takes any run of
# slashes and backslashes after it, and two on their own are protocol-relative. What follows may
# be anything a host or its user info can start with (`[` of an IPv6 literal, `@`, a percent
# escape, a non-ASCII letter): only a control character, a space, a slash, a quote or a bracket
# does not count, which keeps `https:` alone, and `a//b` in the middle of a word, out.
_URL_START = r"""(?:https?:[/\\]*|[/\\]{2})[^\x00-\x20/\\'")<>]"""
_EXTERNAL = re.compile(r"""(?:^|[\x00-\x20,;=('"<])""" + _URL_START, re.IGNORECASE)

# What a URL parser drops from anywhere in its input, before it looks at the scheme.
_DROPPED = re.compile(r"[\t\n\r]")

# A `javascript:` URL, and a `data:` URL that is not an image: a document given by value, which
# can hold anything, so nothing about the origin of what it loads can be read off the markup.
_JAVASCRIPT_URL = re.compile(r"^[\x00-\x20]*javascript:", re.IGNORECASE)
_DATA_DOCUMENT = re.compile(r"^[\x00-\x20]*data:(?!\s*image/)", re.IGNORECASE)

# CSS naming an image or stylesheet from another origin: `url(` followed by one, and a quoted
# string that starts with one (`@import "..."`, and every candidate of `image-set()` after the
# first, which are strings and not `url()`). `_CSS_URL_FN` is the first alone, for the many
# attributes (`fill`, `filter`, `mask`, ...) that take a `url()` and nothing else.
_CSS_URL_FN = re.compile(r"""url\(\s*["']?\s*""" + _URL_START, re.IGNORECASE)
_CSS_LOAD = re.compile(r"""(?:url\(\s*["']?|["']|@import)\s*""" + _URL_START, re.IGNORECASE)
_CSS_IMPORT_DATA = re.compile(r"""@import\s*(?:url\(\s*)?["']?\s*data:""", re.IGNORECASE)

# A CSS escape: a backslash and one to six hex digits (and one space, which ends them), or a
# backslash and any other character. The tokenizer decodes these before it reads a name or a
# string, so `\75rl(` is `url(` and `"\68ttps://..."` is a string starting `https://`.
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.DOTALL)

# Attributes a browser fetches from. `content` is only one on <meta http-equiv=refresh>, and
# `style` is CSS, both handled apart.
_URL_ATTRS = frozenset(
    {
        "src", "srcset", "imagesrcset", "href", "xlink:href", "data", "poster", "background",
        "manifest", "codebase", "archive", "lowsrc", "dynsrc", "icon",
    }
)  # fmt: skip

# Attributes that hold a URL a script may be run from, though nothing is fetched from it.
_ACTIONS = frozenset({"action", "formaction"})

# Tags whose URLs are followed by the reader, not by the page.
_NAVIGATION = frozenset({"a", "area", "form"})

# Tags that embed a document, where a `data:` URL is a whole page or stylesheet given by value.
_DOCUMENTS = frozenset({"iframe", "frame", "object", "embed", "link"})

_SHOWN = 120


def _shown(value: str) -> str:
    """One line, and short, with the start of the value (the host) kept."""
    value = " ".join(value.split())
    return value if len(value) <= _SHOWN else value[:_SHOWN] + "..."


def _css_escape(match: re.Match) -> str:
    if match.group(1) is None:
        return "" if match.group(2) == "\n" else match.group(2)
    code = int(match.group(1), 16)
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�"


def _css_text(text: str) -> str:
    """The CSS as its tokenizer reads it: an escape is the character it names."""
    return _CSS_ESCAPE.sub(_css_escape, text)


def _attribute_load(tag: str, name: str, value: str, refresh: bool) -> str | None:
    """Why this attribute makes a browser run something, or fetch something from another origin."""
    if name.startswith("on") and name[2:].isalpha():
        return "is an inline event handler"
    url = _DROPPED.sub("", value)
    if (name in _URL_ATTRS or name in _ACTIONS) and _JAVASCRIPT_URL.match(url):
        return "runs script"
    css = _css_text(value)
    if name == "style":
        if _CSS_LOAD.search(css) or _CSS_IMPORT_DATA.search(css):
            return "loads from another origin"
    elif _CSS_URL_FN.search(css):
        return "loads from another origin"
    if tag in _NAVIGATION:
        return None
    if name in _URL_ATTRS and tag in _DOCUMENTS and _DATA_DOCUMENT.match(url):
        return "embeds a document by value"
    if (name in _URL_ATTRS or (refresh and name == "content")) and _EXTERNAL.search(url):
        return "loads from another origin"
    return None


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
            if name == "srcdoc":
                # A page given by value: judged as a page, whatever tag carries it.
                for finding in scan_html(value):
                    self.found.append(f'<{tag} srcdoc="{_shown(value)}"> holds a page with {finding}')
                continue
            reason = _attribute_load(tag, name, value, refresh)
            if reason:
                self.found.append(f'<{tag} {name}="{_shown(value)}"> {reason}')

    def handle_startendtag(self, tag, attrs):
        # html.parser reads `<style/>` as an element that is over at once, so the CSS after it
        # would be judged as text. A browser ignores the slash on `style` and reads raw text up to
        # `</style>`, so this is a start tag, as it is there.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "style":
            self._style_open = False

    def handle_data(self, data):
        if not self._style_open:
            return
        # html.parser hands a <style>'s text over raw. A browser decodes character references in
        # it when the <style> is inside <svg> or <math>, so this reads both ways: more matches,
        # never fewer.
        css = _css_text(html.unescape(data))
        if _CSS_LOAD.search(css) or _CSS_IMPORT_DATA.search(css):
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
        if _EXTERNAL.search(_DROPPED.sub("", line))
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
