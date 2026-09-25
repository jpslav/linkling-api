"""A deliberate leak, for scripts/no-third-party-check.sh --mutate api-page only. Never shipped.

Python imports sitecustomize at startup, so mounting this into the service's site-packages makes
the stats page name a third-party stylesheet in its <head>, the way a page that wanted a web font
would. No browser runs, so nothing is ever fetched and the packet capture stays clean: the check
has to catch this by reading what the page says it would load, and name the host.
"""

try:
    from linkling.server import stats
except ImportError:  # the healthcheck runs this Python too, which is harmless; nothing to patch
    stats = None

if stats is not None:
    _render = stats.render

    def render(rows):
        return _render(rows).replace(
            "</head>",
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter"></head>',
            1,
        )

    stats.render = render
