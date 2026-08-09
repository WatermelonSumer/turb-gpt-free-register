"""Helpers for deriving stable registration source names from pickup URLs."""

from __future__ import annotations

import re
from urllib.parse import urlparse


GENERIC_API_SOURCE = "generic_api"

# Source values are persisted in config and job records, so keep them compact
# and host-like. Ports are intentionally excluded: the source is the domain.
_HOST_SOURCE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def pickup_domain(url: str | None) -> str:
    """Return the lower-case hostname used as a dynamic source key."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        # Accept a host/path pasted without a scheme while leaving arbitrary
        # API paths and plain text on the legacy generic_api source.
        if not parsed.hostname and "://" not in raw:
            parsed = urlparse(f"//{raw}")
        host = str(parsed.hostname or "").strip().rstrip(".").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    try:
        # IDNA keeps persisted keys ASCII and stable across URL spellings.
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return ""
    return host if _HOST_SOURCE_RE.fullmatch(host) else ""


def is_dynamic_source(value: str | None) -> bool:
    """Whether a value is a domain-shaped dynamic pickup source."""
    source = str(value or "").strip().lower().rstrip(".")
    return bool(source and _HOST_SOURCE_RE.fullmatch(source))


def source_for_pickup_url(url: str | None, fallback: str = GENERIC_API_SOURCE) -> str:
    """Derive a source key, falling back for non-URL legacy endpoints."""
    fallback_source = str(fallback or GENERIC_API_SOURCE).strip().lower().rstrip(".")
    return pickup_domain(url) or fallback_source


def is_valid_source(value: str | None, static_sources: set[str] | tuple[str, ...] = ()) -> bool:
    """Validate either a built-in source or a dynamic hostname source."""
    source = str(value or "").strip().lower()
    static = {str(item).strip().lower() for item in static_sources}
    return source in static or is_dynamic_source(source)
