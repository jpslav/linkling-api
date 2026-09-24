"""The FastAPI application: create, follow, delete, one link's counts and the stats page.

The route space is ADR-0001's: link names live at the root, everything the service serves
itself lives under ``/-/``, and a custom name may not begin with ``-`` -- which is what
makes a new service route collision-free by construction, with no reserved-word list for
anyone to check against live links.

Three things here look like configuration and are not:

- ``docs_url``, ``redoc_url`` and ``openapi_url`` are ``None``. FastAPI's defaults mount
  ``/docs``, ``/redoc`` and ``/openapi.json`` **at the root**, and ``docs`` and ``redoc``
  are names a person may legitimately want for a link. Left on, the framework would have
  occupied the link namespace.
- the redirect is a plain ``Response``, never ``RedirectResponse``, which percent-encodes
  the URL it is given (verified: it turned ``a b|c`` into ``a%20b%7Cc``). ADR-0005 stores
  the target byte-exact, and a target normalised on the way out is that promise broken one
  step later.
- ``/{name}/`` is its own route. Starlette's ``redirect_slashes`` would otherwise answer a
  trailing slash with a 307 to the bare name -- a second hop, carrying no ``no-store`` --
  where ADR-0001e says ``/<name>/`` *is* ``/<name>`` and R-005 asks for exactly one hop.

Every response the application produces leaves through ``NoStoreMiddleware``. ADR-0003
settles ``302`` for a follow, ``410`` for a deleted **or expired** link and ``404`` for an
unknown name, each with ``Cache-Control: no-store``; but 404 and 405 are heuristically
cacheable per RFC 9110 and the ones Starlette's router generates never reach a handler of
ours. Setting the header in one place is what stops a cached "gone" outliving the deletion
or expiry that caused it.

**One response does not pass through it**, and it is stated here rather than implied: a
500 from ``ServerErrorMiddleware``, which Starlette places outside every middleware added
here, carries no ``Cache-Control`` at all. That is tolerable only because 500 is not in
RFC 9110's heuristically cacheable set; it is not a guarantee this module makes.
"""

from __future__ import annotations

import base64
import hmac
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import MutableHeaders

from . import counts, db, links, names, stats
from .config import Config, load_config

#: ADR-0003: the status a live link answers with.
REDIRECT_STATUS = 302
#: ADR-0003: expired and deleted links.
GONE_STATUS = 410
#: ADR-0003: unknown names.
UNKNOWN_STATUS = 404

#: A target must be an absolute http(s) URL of printable ASCII with no spaces. That is
#: narrow on purpose: it is what makes a byte-exact ``Location`` header safe to emit, and
#: it makes header injection through a CR or LF impossible rather than unlikely. Widening
#: this later (an internationalised URL, say) is additive; narrowing it is not.
_ALLOWED_TARGET_SCHEMES = ("http", "https")
_MAX_CREATED_BY = 256

#: A target longer than this is refused. Without a cap, a 1 MB URL is accepted, stored,
#: and then unfollowable: the `Location` header exceeds what clients and proxies will
#: carry, so the link "succeeds" and 502s for everyone who follows it -- with its name
#: reserved forever, because deletion does not free a name. The number has to sit *below*
#: the smallest header buffer in the path to do its job, and the common one is nginx's
#: `proxy_buffer_size`, documented as defaulting to one memory page -- 4 or 8 KB depending
#: on the platform. 2 KiB leaves room for the rest of the response head under either, and
#: is still far above any real tracking URL. Widening it later is additive.
_MAX_TARGET_LENGTH = 2048

#: The largest request body the service will read. The key check cannot run before this:
#: FastAPI parses the body while solving the route, so an unauthenticated caller would
#: otherwise make the process buffer a body of any size before being told 401.
_MAX_BODY_BYTES = 64 * 1024


class NoStoreMiddleware:
    """Put ``Cache-Control: no-store`` on every response that passes through it.

    That is every response but one: Starlette puts ``ServerErrorMiddleware`` outside the
    middleware an application adds, so a 500 it generates does not come through here.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_no_store(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["cache-control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_no_store)


class BodyLimitMiddleware:
    """Refuse an over-large request body before anything reads it.

    `Content-Length` is checked first because that is the case that can be refused
    without reading a byte; a chunked body with no length is counted as it arrives and
    cut off at the same ceiling.
    """

    def __init__(self, app, max_bytes: int = _MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _too_large(send, self.max_bytes)
            return

        # A chunked body declares no length, so it can only be counted as it arrives.
        # The flag is what the handler's `body_within_limit` dependency reads: cutting
        # the body off and letting the request proceed made the outcome depend on packet
        # timing -- the same over-limit request answered 422 when the excess arrived in
        # the first chunk and **201** when it arrived after the handler's first read.
        over_limit = {"hit": False}
        scope["linkling.body_over_limit"] = over_limit
        received = 0

        async def receive_counting():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    over_limit["hit"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        await self.app(scope, receive_counting, send)


def _content_length(scope) -> int | None:
    for key, value in scope.get("headers", []):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _is_team_key(request: Request, candidate: bytes) -> bool:
    """Whether ``candidate`` is the team key, whichever scheme it arrived in.

    Compared as bytes: `hmac.compare_digest` on `str` raises TypeError for any non-ASCII
    character, and Starlette decodes header bytes as latin-1 -- so a token carrying one
    byte >= 0x80 turned an unauthenticated 401 into a 500. ``compare_digest`` does not
    return early on the first differing byte, so the *content* of a wrong key cannot be
    found one byte at a time; it does not hide the key's length, and nothing here claims
    to.
    """
    return hmac.compare_digest(candidate, request.app.state.config.api_key.encode("utf-8"))


async def _too_large(send, limit: int) -> None:
    body = f'{{"detail":"Request body larger than {limit} bytes."}}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class CreateLink(BaseModel):
    """The create request.

    ``extra="forbid"`` is deliberate (ADR-0011b): a caller who sends a field this build
    does not yet recognise gets a 422 instead of a silently ignored request -- which is
    exactly how a caller who sent ``expires`` before LL-010 shipped it would otherwise
    have gotten a link that silently never expired.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None
    created_by: str | None = None
    expires: str | None = None


def _validate_target(raw: str) -> str:
    if not raw:
        raise HTTPException(422, "url is required.")
    if len(raw) > _MAX_TARGET_LENGTH:
        raise HTTPException(422, f"url must be at most {_MAX_TARGET_LENGTH} characters.")
    if any(character < "\x21" or character > "\x7e" for character in raw):
        raise HTTPException(
            422,
            "url must be printable ASCII with no spaces; percent-encode anything else.",
        )
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        # `urlsplit` raises on a malformed IPv6 host (`http://[`), which the printable-
        # ASCII check above does not catch. Uncaught it is a 500 on a call that is simply
        # wrong, and the caller cannot tell the two apart.
        raise HTTPException(422, f"url could not be parsed: {exc}") from exc
    if parts.scheme not in _ALLOWED_TARGET_SCHEMES:
        raise HTTPException(422, "url must be an absolute http:// or https:// URL.")
    if not parts.netloc:
        raise HTTPException(422, "url must have a host.")
    return raw


def _validate_expires(raw: str | None, now: str) -> str | None:
    """ADR-0013: `expires` must be an ISO-8601 UTC timestamp in `links.TIMESTAMP_FORMAT`,
    strictly after ``now``.

    ``now`` is passed in rather than read here with `links._now()`, and the caller reads
    it exactly once per request and reuses it for the row's `created_at` too: two separate
    reads let a clock tick land between them, so an `expires` one second out could pass
    this check against the first read and still land at or before `created_at` from a
    second read moments later -- a link `410` from its first follow, with its name
    reserved forever.

    Parsed with `strptime` rather than `datetime.fromisoformat`: `fromisoformat` accepts
    shapes `TIMESTAMP_FORMAT` does not print (no seconds, a numeric offset, no `Z` at all).
    `strptime` alone is not enough either -- verified by driving it, not by reading its
    docs: it accepts 1- and 2-digit months/days/hours, a space-padded day, lowercase `t`/`z`
    (its literals match case-insensitively) and non-ASCII decimal digits (Arabic-Indic,
    fullwidth), storing whichever spelling it was given. `links.lookup`'s expiry check is a
    plain string comparison that depends on every stored value sharing one shape, so a
    value in a shape the service would never itself print breaks that ordering silently
    rather than loudly -- `"2026-01- 5T00:00:00Z"` sorts *before* every real January date
    despite naming the 5th, because a space sorts below every digit. Round-tripping the
    parsed value back through `strftime` and requiring it to reproduce the input catches
    every one of those, because `strftime` only ever emits the one canonical shape.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.strptime(raw, links.TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise HTTPException(
            422,
            "expires must be an ISO-8601 UTC timestamp shaped like "
            "2026-01-01T00:00:00Z.",
        ) from exc
    if parsed.strftime(links.TIMESTAMP_FORMAT) != raw:
        raise HTTPException(
            422,
            "expires must be an ISO-8601 UTC timestamp shaped like "
            "2026-01-01T00:00:00Z.",
        )
    if raw <= now:
        raise HTTPException(422, "expires must be in the future.")
    return raw


def _validate_created_by(raw: str | None) -> str | None:
    if raw is None:
        return None
    if len(raw) > _MAX_CREATED_BY:
        raise HTTPException(422, f"created_by must be at most {_MAX_CREATED_BY} characters.")
    if any(character < " " or character == "\x7f" for character in raw):
        # The stats page (LL-016) renders this, escaped, and the CLI (LL-003) will; a
        # stored `\r\n` is a surprise for each of them to discover. Refusing it up front
        # is additive-safe.
        raise HTTPException(422, "created_by must not contain control characters.")
    return raw


def create_app(config: Config | None = None) -> FastAPI:
    """Build the application. ``config`` defaults to the process environment."""
    settings = load_config() if config is None else config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="Linkling",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Off, because Starlette's slash redirect is a second hop built from the `Host`
        # header: `/q3-plan//` answered `307 Location: http://<whatever Host said>/q3-plan`.
        # The `/{name}/` route below is what ADR-0001e's "treated as" means; every other
        # spelling is simply not a link.
        redirect_slashes=False,
    )
    app.state.config = settings
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(BodyLimitMiddleware)

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = db.connect(request.app.state.config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def body_within_limit(request: Request) -> None:
        """Answer 413 for a chunked body the middleware had to cut off.

        The middleware can refuse a declared `Content-Length` outright, but a chunked
        body is only countable as it arrives, and by then the handler may already have
        read a complete, valid JSON prefix. Without this the same over-limit request
        answered 422 or 201 depending on when the excess landed.
        """
        if request.scope.get("linkling.body_over_limit", {}).get("hit"):
            raise HTTPException(
                413, f"Request body larger than {_MAX_BODY_BYTES} bytes."
            )

    def require_key(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """ADR-0006a: one team key, ``Authorization: Bearer <key>``, compared by
        ``_is_team_key``. Following a link does not depend on this and never will: R-011
        and R-012 are only meaningful together.
        """
        unauthorised = HTTPException(
            401,
            "This call needs the team API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        if not authorization:
            raise unauthorised
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise unauthorised
        if not _is_team_key(request, token.strip().encode("utf-8")):
            raise unauthorised

    def require_basic_key(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """ADR-0006b: the stats page's gate. HTTP Basic, the team key as the *password*.

        Basic rather than Bearer because a person opens this page in a browser, and Basic
        is browser-native, needs no session code and works with ``curl -u ":$KEY"``
        (ADR-0006b). The user name is ignored, since there is one key and no accounts, and
        the password is everything after the first colon (RFC 7617 section 2: only the
        user-id is barred from containing one), so a key with a colon in it still works.
        Compared by the same ``_is_team_key`` as Bearer.
        """
        unauthorised = HTTPException(
            401,
            "This page needs the team API key as the password.",
            headers={"WWW-Authenticate": 'Basic realm="Linkling", charset="UTF-8"'},
        )
        if not authorization:
            raise unauthorised
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() != "basic":
            raise unauthorised
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True)
        except ValueError:
            raise unauthorised from None
        _, colon, password = decoded.partition(b":")
        if not colon or not _is_team_key(request, password):
            raise unauthorised

    # NOTE: the connection is taken as `= Depends(get_conn)` rather than through an
    # `Annotated` alias. `from __future__ import annotations` makes every annotation a
    # string, and FastAPI resolves those against module globals -- an alias defined inside
    # this factory is invisible there, and each handler's `conn` silently became a query
    # parameter. The suite said so in 46 failures reading `loc: ["query", "conn"]`.

    @app.post(
        "/-/api/links",
        status_code=201,
        dependencies=[Depends(body_within_limit), Depends(require_key)],
    )
    def create_link(
        body: CreateLink, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, str]:
        # Read once and reuse for `created_at`: two separate reads of the clock would let
        # a tick land between the "is this still in the future" check and the row's own
        # timestamp, which is exactly the race `_validate_expires`'s docstring names.
        now = links._now()
        target = _validate_target(body.url)
        created_by = _validate_created_by(body.created_by)
        expires_at = _validate_expires(body.expires, now)

        if body.name is None:
            try:
                link = links.create_generated(
                    conn,
                    target=target,
                    created_by=created_by,
                    expires_at=expires_at,
                    created_at=now,
                )
            except links.GenerationExhausted as exc:
                raise HTTPException(503, str(exc)) from exc
        else:
            name = names.normalise(body.name)
            if name is None:
                raise HTTPException(
                    422,
                    "name must be 1-64 characters of lowercase ASCII letters, digits and "
                    "hyphens, not starting or ending with a hyphen.",
                )
            try:
                link = links.create(
                    conn,
                    name=name,
                    target=target,
                    created_by=created_by,
                    expires_at=expires_at,
                    created_at=now,
                )
            except links.NameTaken as exc:
                raise HTTPException(
                    409,
                    "That name is taken. A deleted name stays reserved -- ADR-0005.",
                ) from exc

        return {"name": link.name, "url": link.target}

    @app.delete("/-/api/links/{name}", status_code=204, dependencies=[Depends(require_key)])
    def delete_link(
        name: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Response:
        folded = names.normalise(name)
        if folded is None:
            raise HTTPException(UNKNOWN_STATUS, "No such link.")
        try:
            links.delete(conn, folded)
        except links.NameUnknown as exc:
            raise HTTPException(UNKNOWN_STATUS, "No such link.") from exc
        except links.NameDeleted as exc:
            raise HTTPException(
                GONE_STATUS, "That link was already deleted. Its name stays reserved."
            ) from exc
        return Response(status_code=204)

    @app.get("/-/api/links/{name}/stats", dependencies=[Depends(require_key)])
    def link_stats(
        name: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, dict[str, int]]:
        """R-008: one link's count for each UTC day, as ``{"days": {"YYYY-MM-DD": count}}``.

        The key is checked before the name is looked up, so a caller without it learns
        nothing about which names exist. Answers as the follow route does for a name that
        never existed (404) or was deleted (410), but not for an expired link: it answers
        200 with the counts ADR-0014 keeps (``docs/adr/0014-daily-counts-table-shape.md:42-46``),
        because the data is still there and the stats page shows it (``stats.py``, module
        docstring). Calls only ``counts.days_for``, never ``counts.record_follow``:
        reading a count must not add to it.
        """
        folded = names.normalise(name)
        if folded is None:
            raise HTTPException(UNKNOWN_STATUS, "No such link.")
        try:
            days = counts.days_for(conn, folded)
        except links.NameUnknown as exc:
            raise HTTPException(UNKNOWN_STATUS, "No such link.") from exc
        except links.NameDeleted as exc:
            raise HTTPException(
                GONE_STATUS, "That link was deleted. Its name stays reserved."
            ) from exc
        return {"days": days}

    @app.get("/-/stats", dependencies=[Depends(require_basic_key)])
    def stats_page(conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
        """R-010: every link with its count for each day, plain HTML, behind the key.

        Never calls into ``counts``: opening the page must not count as a follow.
        """
        return HTMLResponse(stats.render(stats.collect(conn)))

    @app.api_route("/{name}", methods=["GET", "HEAD"])
    @app.api_route("/{name}/", methods=["GET", "HEAD"])
    def follow(name: str, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        """The whole experience of the person clicking: one hop, no credential, no cookie.

        A query string on the short link is dropped rather than merged into the target
        (ADR-0001e): the target carries its own parameters and merging two query strings is
        ambiguous in a way nobody would remember.

        Every redirect is counted, once, before it is sent: ADR-0004's decision record
        (``docs/adr/0004-click-record-contents.md:89``) counts every request that follows a
        link, inspecting nothing -- so ``HEAD`` too --
        and ADR-0005 makes the increment synchronous.
        Only the folded name reaches the counter -- nothing about the request does.
        """
        folded = names.normalise(name)
        link = None if folded is None else links.lookup(conn, folded)
        if link is None:
            raise HTTPException(UNKNOWN_STATUS, "No such link.")
        if link.deleted:
            raise HTTPException(
                GONE_STATUS, "That link was deleted. Its name stays reserved."
            )
        if link.expired:
            raise HTTPException(GONE_STATUS, "That link has expired.")
        if not counts.record_follow(conn, folded):
            # Deleted between the lookup and the increment: answer as the lookup would
            # have a moment later, so no 302 ever leaves uncounted.
            raise HTTPException(
                GONE_STATUS, "That link was deleted. Its name stays reserved."
            )
        return Response(status_code=REDIRECT_STATUS, headers={"Location": link.target})

    return app
