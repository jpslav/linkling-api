"""The FastAPI application: create, follow and delete.

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

Every response leaves through ``NoStoreMiddleware``. ADR-0003 settles ``302`` for a
follow, ``410`` for a deleted link and ``404`` for an unknown name, each with
``Cache-Control: no-store``; but 404 and 405 are heuristically cacheable per RFC 9110 and
the ones Starlette's router generates never reach a handler of ours. Setting the header in
one place is what stops a cached "gone" outliving the deletion that caused it.
"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import MutableHeaders

from . import db, links, names
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


class NoStoreMiddleware:
    """Put ``Cache-Control: no-store`` on every response, whoever produced it."""

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


class CreateLink(BaseModel):
    """The create request.

    ``extra="forbid"`` is deliberate: when LL-010 adds ``expires``, a caller who sends it
    to a service that does not have it yet gets a 422 instead of a link that silently
    never expires.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None
    created_by: str | None = None


def _validate_target(raw: str) -> str:
    if not raw:
        raise HTTPException(422, "url is required.")
    if any(character < "\x21" or character > "\x7e" for character in raw):
        raise HTTPException(
            422,
            "url must be printable ASCII with no spaces; percent-encode anything else.",
        )
    parts = urlsplit(raw)
    if parts.scheme not in _ALLOWED_TARGET_SCHEMES:
        raise HTTPException(422, "url must be an absolute http:// or https:// URL.")
    if not parts.netloc:
        raise HTTPException(422, "url must have a host.")
    return raw


def _validate_created_by(raw: str | None) -> str | None:
    if raw is None:
        return None
    if len(raw) > _MAX_CREATED_BY:
        raise HTTPException(422, f"created_by must be at most {_MAX_CREATED_BY} characters.")
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
    )
    app.state.config = settings
    app.add_middleware(NoStoreMiddleware)

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = db.connect(request.app.state.config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_key(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """ADR-0006a: one team key, ``Authorization: Bearer <key>``.

        Compared with ``hmac.compare_digest`` so a wrong key cannot be found one character
        at a time. Following a link does not depend on this and never will: R-011 and R-012
        are only meaningful together.
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
        expected = request.app.state.config.api_key
        if not hmac.compare_digest(token.strip(), expected):
            raise unauthorised

    # NOTE: the connection is taken as `= Depends(get_conn)` rather than through an
    # `Annotated` alias. `from __future__ import annotations` makes every annotation a
    # string, and FastAPI resolves those against module globals -- an alias defined inside
    # this factory is invisible there, and each handler's `conn` silently became a query
    # parameter. The suite said so in 46 failures reading `loc: ["query", "conn"]`.

    @app.post("/-/api/links", status_code=201, dependencies=[Depends(require_key)])
    def create_link(
        body: CreateLink, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, str]:
        target = _validate_target(body.url)
        created_by = _validate_created_by(body.created_by)

        if body.name is None:
            try:
                link = links.create_generated(
                    conn, target=target, created_by=created_by
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
                    conn, name=name, target=target, created_by=created_by
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

    @app.api_route("/{name}", methods=["GET", "HEAD"])
    @app.api_route("/{name}/", methods=["GET", "HEAD"])
    def follow(name: str, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        """The whole experience of the person clicking: one hop, no credential, no cookie.

        A query string on the short link is dropped rather than merged into the target
        (ADR-0001e): the target carries its own parameters and merging two query strings is
        ambiguous in a way nobody would remember.
        """
        folded = names.normalise(name)
        link = None if folded is None else links.lookup(conn, folded)
        if link is None:
            raise HTTPException(UNKNOWN_STATUS, "No such link.")
        if link.deleted:
            raise HTTPException(
                GONE_STATUS, "That link was deleted. Its name stays reserved."
            )
        return Response(status_code=REDIRECT_STATUS, headers={"Location": link.target})

    return app
