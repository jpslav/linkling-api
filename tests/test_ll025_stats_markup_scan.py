"""LL-025 -- the markup scan scripts/no-third-party-check.sh applies to the `/-/stats` page.

The check itself needs Docker. This pins, without it, the scanner it runs in the observer
(scripts/no-third-party/loads.py): what it lets a page say and what it refuses. The stats page
prints every link's target as text, and a target is a URL by nature, so the scan the site gets
(a grep for any absolute URL) would fail the page as shipped. This one reads the HTML and flags
only what a browser would go and fetch, or run.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from linkling.server import stats

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "no-third-party" / "loads.py"
_spec = importlib.util.spec_from_file_location("no3p_loads", _PATH)
loads = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = loads
_spec.loader.exec_module(loads)


def _shipped_page() -> str:
    """The real renderer's output: targets with the characters HTML escapes, as text."""
    rows = [
        stats.LinkStats(
            name="q3-plan",
            target='https://example.com/a?x=1&y=2#frag "quoted" <b>',
            created_by="jp",
            expires_at="2099-01-01T00:00:00Z",
            expired=False,
            days=(("2026-09-24", 3), ("2026-09-23", 1)),
        ),
        stats.LinkStats(
            name="proto-relative",
            target="//cdn.example.com/x.png",
            created_by=None,
            expires_at=None,
            expired=False,
            days=(),
        ),
        stats.LinkStats(
            name="scheme-less-slashes",
            target="https:/host.example/only-one-slash",
            created_by='<script src="https://evil.example/x.js">',
            expires_at=None,
            expired=False,
            days=(),
        ),
    ]
    return stats.render(rows)


def test_the_page_as_shipped_is_clean_with_link_targets_printed_as_text():
    page = _shipped_page()
    assert "https://example.com/a?x=1&amp;y=2" in page, "the targets must appear in the page"
    assert loads.scan_html(page) == []


def test_an_empty_page_is_clean():
    assert loads.scan_html(stats.render([])) == []


LOADS = {
    "stylesheet": '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
    "protocol-relative image": '<img src="//cdn.example.com/x.png">',
    "backslashes": '<img src="\\\\cdn.example.com\\x.png">',
    "one slash": '<img src="https:/cdn.example.com/x.png">',
    "srcset": '<img src="/a.png" srcset="/a.png 1x, https://cdn.example.com/b.png 2x">',
    "iframe": '<iframe src="https://example.org/"></iframe>',
    "object": '<object data="https://example.org/x.swf"></object>',
    "video poster": '<video poster="https://example.org/p.png"></video>',
    "base": '<base href="https://example.org/">',
    "svg use": '<svg><use xlink:href="https://example.org/s.svg#a"></use></svg>',
    "meta refresh": '<meta http-equiv="refresh" content="0; url=https://example.org/">',
    "upper case": '<LINK REL=stylesheet HREF="HTTPS://EXAMPLE.ORG/A.CSS">',
    "tag over lines": '<link\n  rel="stylesheet"\n  href="https://example.org/a.css"\n>',
    "slash as entity": '<img src="https:&#47;&#x2f;example.org/x.png">',
    "style import": "<style>@import url(https://example.org/a.css);</style>",
    "style import bare": '<style>@import "https://example.org/a.css";</style>',
    "style url": "<style>body { background: url('//example.org/i.png') }</style>",
    "style attribute": '<p style="background:url(https://example.org/i.png)">x</p>',
    "external script": '<script src="https://example.org/x.js"></script>',
    "inline script": "<script>fetch('/x')</script>",
    "inline handler": '<body onload="fetch(1)">',
    "handler on a link": '<a href="/x" onclick="fetch(1)">x</a>',
    "handler, upper case": '<p ONCLICK="fetch(1)">x</p>',
    # Round 1, F1: spellings a browser's URL parser reads as another origin. It removes every tab
    # and newline from its input first, strips leading control characters, and takes any first
    # character of a host (an IPv6 literal, empty user info, a percent escape, a full-width letter).
    "tab inside the slashes": '<img src="https:/\t/evil.example/x.png">',
    "newline after the slashes": '<img src="https://\nevil.example/x.png">',
    "newline entity after the slashes": '<link rel="stylesheet" href="https://&#10;fonts.googleapis.com/css2?family=Inter">',
    "tab entity inside the scheme": '<img src="ht&#9;tps://evil.example/x.png">',
    "tab between protocol-relative slashes": '<img src="/\t/evil.example/x.png">',
    "leading control character": '<img src="\x01https://evil.example/x.png">',
    "leading space": '<img src=" https://evil.example/x.png">',
    "ipv6 host": '<link rel="stylesheet" href="https://[2606:4700:4700::1111]/a.css">',
    "ipv6 host, protocol-relative": '<img src="//[2606:4700:4700::1111]/a.png">',
    "empty user info": '<img src="https://@evil.example/x.png">',
    "percent-encoded host": '<img src="https://%65vil.example/x.png">',
    "full-width host": '<img src="https://\uff45vil.example/x.png">',
    # Round 1, F2: a CSS load without a literal `url(` or `@import`, or spelt so that only the
    # tokenizer sees it.
    "image-set string": '<style>body{background:image-set("https://evil.example/x.png" 1x)}</style>',
    "image-set, second candidate": '<style>body{background:image-set("/a.png" 1x, "https://evil.example/x.png" 2x)}</style>',
    "image-set in a style attribute": "<p style=\"background:-webkit-image-set('https://evil.example/x.png' 1x)\">x</p>",
    "css escape in the function name": "<style>body{background:\\75rl(https://evil.example/x.png)}</style>",
    "css escape in a string": '<style>@import "\\68ttps://evil.example/a.css";</style>',
    "css escape that eats a space": '<style>@import "\\68 ttps://evil.example/a.css";</style>',
    "comment after import": '<style>@import/**/"https://evil.example/a.css";</style>',
    "import of a data document": '<style>@import "data:text/css;base64,QGltcG9ydA==";</style>',
    # Round 1, F3: places where the HTML parser and a browser's tree builder differ.
    "self-closing style": "<style/>@import url(https://evil.example/a.css);</style>",
    "self-closing style, spaced, upper case": "<STYLE />@import url(https://evil.example/a.css);</STYLE>",
    "svg style with slashes as entities": "<svg><style>@import url(https:&#x2f;&#x2f;evil.example/a.css);</style></svg>",
    "svg style with the function name as an entity": "<svg><style>@import &#117;rl(https://evil.example/a.css)</style></svg>",
    # Round 1, F4: documents and scripts given by value.
    "srcdoc": '<iframe srcdoc="&lt;script src=https://evil.example/x.js&gt;&lt;/script&gt;"></iframe>',
    "srcdoc loading an image": '<iframe srcdoc="&lt;img src=&quot;https://evil.example/x.png&quot;&gt;"></iframe>',
    "javascript url in an iframe": '<iframe src="javascript:fetch(1)"></iframe>',
    "javascript url in a link": '<a href="javascript:fetch(1)">x</a>',
    "javascript url with a tab in the scheme": '<a href="java\tscript:fetch(1)">x</a>',
    "javascript url in a form action": '<form action="javascript:fetch(1)"></form>',
    "svg fill from another origin": '<svg><rect fill="url(https://evil.example/x.svg#g)"></rect></svg>',
    "svg filter from another origin": "<svg><rect filter=\"url('//evil.example/x.svg#f')\"></rect></svg>",
    "svg stroke from another origin": '<svg><path stroke="url(https://evil.example/x.svg#g)"></path></svg>',
    "svg mask from another origin": '<svg><rect mask="url(https://evil.example/x.svg#m)"></rect></svg>',
    "svg clip-path from another origin": '<svg><rect clip-path="url(https://evil.example/x.svg#c)"></rect></svg>',
    "svg marker from another origin": '<svg><path marker-end="url(https://evil.example/x.svg#e)"></path></svg>',
    "cursor from another origin": '<svg><rect cursor="url(https://evil.example/x.cur), auto"></rect></svg>',
    "data document in an iframe": '<iframe src="data:text/html;base64,PHNjcmlwdD4="></iframe>',
    "data stylesheet in a link": '<link rel="stylesheet" href="data:text/css;base64,QGltcG9ydA==">',
    "data object": '<object data="data:application/x-shockwave-flash;base64,AAAA"></object>',
}


@pytest.mark.parametrize("name", sorted(LOADS))
def test_a_page_that_loads_something_is_flagged(name):
    found = loads.scan_html(f"<!doctype html><html><head>{LOADS[name]}</head><body><p>x</p></body></html>")
    assert found, f"{name}: nothing was flagged in {LOADS[name]!r}"


@pytest.mark.parametrize("name", sorted(LOADS))
def test_a_mutation_of_the_real_page_is_flagged_too(name):
    """The shipped page plus one added element, so the finding cannot come from the targets."""
    page = _shipped_page().replace("</body>", f"{LOADS[name]}</body>", 1)
    assert loads.scan_html(page), name


NOT_LOADS = {
    "anchor to another origin": '<a href="https://example.org/x">x</a>',
    "anchor, protocol-relative": '<a href="//example.org/x">x</a>',
    "area": '<map name="m"><area href="https://example.org/x"></map>',
    "form action": '<form action="https://example.org/x"><input name="q"></form>',
    "same-origin stylesheet": '<link rel="stylesheet" href="/local.css">',
    "same-origin image": '<img src="a.png" alt="see https://example.org/x">',
    "data uri": '<img src="data:image/png;base64,AAAA">',
    "title attribute": '<p title="https://example.org/x">x</p>',
    "text": "<p>https://example.org/x and //example.org/y</p>",
    "escaped script in text": "<p>&lt;script src=&quot;https://example.org/x.js&quot;&gt;</p>",
    "comment": "<!-- <script src=https://example.org/x.js> -->",
    "css without a url": "<style>body { margin: 0 }</style>",
    "inline icon": '<link rel="icon" href="data:image/png;base64,AAAA">',
    "inline image": '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E" alt="x">',
    "inline font": "<style>@font-face { font-family: x; src: url(data:font/woff2;base64,AAAA) }</style>",
    "same-origin css url": "<style>body { background: url(/local.png) }</style>",
    "same-origin image-set": '<style>body { background: image-set("/a.png" 1x, "/b.png" 2x) }</style>',
    "a string in css that is not a url": '<style>p::before { content: "see https://example.org" }</style>',
    "same-origin fill": '<svg><rect fill="url(#g)"></rect></svg>',
    "harmless srcdoc": '<iframe srcdoc="&lt;p&gt;https://example.org/x&lt;/p&gt;"></iframe>',
    "host-like text in an attribute that fetches nothing": '<p data-note="//example.org/x and https://[::1]/">x</p>',
    "ipv6 target as text": "<p>https://[2606:4700:4700::1111]/a and https://@host/</p>",
    "comment marks inside a url": '<style>body { background: url(/a.png) } /* https://example.org/x */</style>',
    # Round 2: a data: URL that is not a document, and text in an attribute that holds no CSS.
    "empty data url on an icon": '<link rel="icon" href="data:,">',
    "data url with no media type": '<link rel="icon" href="data:;base64,AAAA">',
    "plain text data url": '<iframe src="data:text/plain,hello"></iframe>',
    "url( in a title": '<a title="see url(https://x.test/a)" href="/x">x</a>',
    "url( in an alt": '<img src="/a.png" alt="see url(//x.test/a)">',
    "url( in an aria-label": '<p aria-label="url(https://x.test/a)">x</p>',
    "url( in a data attribute": '<p data-note="url(\'https://x.test/a\')">x</p>',
}


@pytest.mark.parametrize("name", sorted(NOT_LOADS))
def test_what_a_reader_must_act_on_and_plain_text_are_not_flagged(name):
    assert loads.scan_html(f"<p>x</p>{NOT_LOADS[name]}") == [], name


def test_a_finding_names_the_tag_and_the_url():
    (finding,) = loads.scan_html(LOADS["stylesheet"])
    assert "<link" in finding
    assert "fonts.googleapis.com" in finding


def test_a_finding_stays_on_one_line_however_long_the_value():
    long_url = "https://example.org/" + "a" * 500 + "\nb"
    (finding,) = loads.scan_html(f'<img src="{long_url}">')
    assert "\n" not in finding and len(finding) < 300
    assert "example.org" in finding


HEADERS_CLEAN = (
    "HTTP/1.1 200 OK\r\n"
    "date: Thu, 24 Sep 2026 12:00:00 GMT\r\n"
    "server: uvicorn\r\n"
    "content-length: 812\r\n"
    "content-type: text/html; charset=utf-8\r\n"
    "cache-control: no-store\r\n"
    "\r\n"
)


def test_ordinary_response_headers_are_clean():
    assert loads.scan_headers(HEADERS_CLEAN) == []


@pytest.mark.parametrize(
    "header",
    [
        "link: <https://fonts.googleapis.com/css2>; rel=preload; as=style",
        "link: <//cdn.example.com/a.css>; rel=stylesheet",
        "link: <https://[2606:4700::1]/a.css>; rel=stylesheet",
        "link: <https://@example.org/a.css>; rel=stylesheet",
        "location: https://example.org/",
        "refresh: 0; url=https://example.org/",
    ],
)
def test_a_header_that_names_another_origin_is_flagged(header):
    assert loads.scan_headers(HEADERS_CLEAN.replace("\r\n\r\n", f"\r\n{header}\r\n\r\n"))


def test_the_command_line_prints_findings_and_exits_zero_either_way():
    """Exit 0 always: a non-zero status is the scanner failing to run, which the check calls blind."""
    run = lambda mode, text: subprocess.run(  # noqa: E731
        [sys.executable, str(_PATH), mode], input=text, capture_output=True, text=True
    )
    clean = run("html", _shipped_page())
    assert (clean.returncode, clean.stdout) == (0, "")
    dirty = run("html", LOADS["stylesheet"])
    assert dirty.returncode == 0 and "fonts.googleapis.com" in dirty.stdout
    heads = run("headers", HEADERS_CLEAN + "link: <https://example.org/a.css>\r\n")
    assert heads.returncode == 0 and "example.org" in heads.stdout
    assert run("nonsense", "").returncode == 64
