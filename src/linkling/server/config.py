"""Configuration, read from the environment once and never defaulted.

Both values are required and neither has a fallback, deliberately:

- an absent or empty ``LINKLING_API_KEY`` must never leave the service running with a
  key that ``Authorization: Bearer `` satisfies, which is what an empty-string default
  would do;
- an absent ``LINKLING_DB`` must never make the service create a database somewhere
  nobody thinks to look for it. ADR-0007a puts the file on a bind mount precisely so
  that "the database" is a path a person can name.

``LINKLING_DB`` is the same variable ``products/linkling/REQUIREMENTS.md`` uses for the
path R-009's ``sqlite3`` check opens, so the service and the check name the same file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

API_KEY_ENV = "LINKLING_API_KEY"  # ADR-0006a
DB_ENV = "LINKLING_DB"


class ConfigError(RuntimeError):
    """Raised at startup when a required setting is missing."""


@dataclass(frozen=True)
class Config:
    api_key: str
    db_path: str


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from ``env`` (the process environment by default)."""
    source: Mapping[str, str] = os.environ if env is None else env

    api_key = (source.get(API_KEY_ENV) or "").strip()
    if not api_key:
        raise ConfigError(
            f"{API_KEY_ENV} is not set. The service refuses to start without a team key: "
            "an empty key would accept any request carrying an empty bearer token."
        )
    if not api_key.isascii() or not api_key.isprintable():
        # A key an ordinary client cannot put in a header is a service that refuses every
        # write while looking perfectly healthy. Two ways in, both measured: httpx raises
        # `UnicodeEncodeError` rather than send a non-ASCII header value, and an embedded
        # CR or LF makes h11 refuse to build the header (`LocalProtocolError`). The check
        # is `isprintable()`, which is stricter than the wire strictly requires -- h11
        # will carry `\x01` and `\x7f` -- because a key containing one of those is a
        # configuration accident either way, and narrowing here costs nothing.
        raise ConfigError(
            f"{API_KEY_ENV} must be printable ASCII -- it is sent in an HTTP header, and "
            "a non-ASCII character or a newline in one stops every client from sending it."
        )

    db_path = (source.get(DB_ENV) or "").strip()
    if not db_path:
        raise ConfigError(
            f"{DB_ENV} is not set. The service refuses to start without an explicit "
            "database path rather than create one somewhere nobody will look for it."
        )

    return Config(api_key=api_key, db_path=db_path)
