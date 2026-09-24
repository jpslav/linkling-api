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
