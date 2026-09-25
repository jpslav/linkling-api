"""The privacy manifest: what the service stores, table by table and column by column.

ADR-0008 §c puts the "what we store" truth in the repository that does the storing, and has
the running service serve the same file at ``/-/privacy.json`` so the *deployed* copy can be
checked rather than only the repository's. ADR-0020 records why the file is package data
beside this module rather than at the repository root. It is read with ``importlib.resources``,
so the same code finds it in an editable install, a non-editable one and the image.

Two tests keep it true (``tests/test_ll015_privacy_manifest.py``). One compares its columns
with a freshly migrated schema in both directions. The other checks each column's
``on_delete`` and ``on_expiry`` claim against what deleting and expiring a link actually do.
"""

from __future__ import annotations

from importlib import resources

MANIFEST_NAME = "what-we-store.json"

#: What deleting or expiring a link does to one column, in the manifest's words. ADR-0020
#: defines each one.
EFFECTS = ("kept", "set", "emptied", "nulled", "removed")


def manifest_bytes() -> bytes:
    """The manifest exactly as shipped. Raises ``FileNotFoundError`` if it is missing."""
    return resources.files(__package__).joinpath(MANIFEST_NAME).read_bytes()
