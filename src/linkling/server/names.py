"""Link names: the grammar custom names must conform to, and the generator.

Two ADRs meet here.

ADR-0001 settles that names live at the root, that the service's own routes live under
``/-/`` (so a name may never begin with ``-``), that names fold to lowercase, and that the
custom-name grammar is ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`` over 1-64 ASCII characters.

ADR-0002 settles that a generated name is six characters drawn from 28 symbols -- the
consonants excluding ``y``, plus the digits ``2``-``9``, so a generated name can never
spell anything and contains none of the pairs people misread off a slide.

Two details here are not decoration:

- the ASCII check runs *before* the lowercase fold. ``"\\u212a"`` (the Kelvin sign) folds
  to an ASCII ``k``, so folding first would admit a non-ASCII name as a conforming one.
- the grammar is matched with ``re.fullmatch``. Written with ``re.match`` and ``$`` --
  which is how ADR-0001d prints it -- ``"q3-plan\\n"`` matches, because ``$`` also matches
  before a trailing newline.
"""

from __future__ import annotations

import re
import secrets

#: ADR-0002: the 28 symbols a generated name is drawn from.
ALPHABET = "bcdfghjklmnpqrstvwxz23456789"

#: ADR-0002: six characters. Shortening this later collides with names already issued.
GENERATED_LENGTH = 6

#: ADR-0001d: 1-64 characters.
MAX_NAME_LENGTH = 64

_GRAMMAR = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def fold(raw: str) -> str:
    """Fold a name the way ADR-0001c settles: lowercase, but only if it is ASCII."""
    return raw.lower() if raw.isascii() else raw


def is_conforming(name: str) -> bool:
    """Does an already-folded name conform to ADR-0001d?"""
    if not name.isascii():
        return False
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        return False
    return _GRAMMAR.fullmatch(name) is not None


def normalise(raw: str) -> str | None:
    """Fold ``raw`` and return it if it conforms, else ``None``."""
    folded = fold(raw)
    return folded if is_conforming(folded) else None


def generate() -> str:
    """A new candidate name.

    ``secrets`` rather than ``random``: ADR-0002's record settles that the set of link
    targets is confidential, and a ``random``-seeded sequence is reconstructable from a
    couple of observed names, which would hand the whole link set to anyone who has two.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(GENERATED_LENGTH))
