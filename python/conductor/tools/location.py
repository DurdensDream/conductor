"""normalize_location — the "external API call" archetype.

Parses a free-text location string and resolves it through a geocoder. The geocoder here
is a deterministic in-process lookup, but it is modelled as an *external dependency*: it
lives behind a function that the workflow calls as its own retryable activity, so a real
geocoding API (flaky, rate-limited) could be dropped in with no change to the tool or the
durability story. Also infers remote/hybrid/onsite from the title + description."""

from __future__ import annotations

import re

from ..models import Location, RawPosting, RemoteType

# A tiny deterministic "geocoder": city -> (state/region, country). Stands in for an
# external geocoding service. Keys are lowercased city tokens.
_GEO: dict[str, tuple[str | None, str]] = {
    "bangalore": ("Karnataka", "India"),
    "bengaluru": ("Karnataka", "India"),
    "mumbai": ("Maharashtra", "India"),
    "san francisco": ("California", "United States"),
    "new york": ("New York", "United States"),
    "austin": ("Texas", "United States"),
    "london": (None, "United Kingdom"),
    "berlin": (None, "Germany"),
    "toronto": ("Ontario", "Canada"),
    "remote": (None, ""),
}

_REMOTE_RE = re.compile(r"\b(fully remote|100% remote|remote|work from home|wfh)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on-?site|in office|in-person)\b", re.I)


def geocode(city_token: str) -> tuple[str | None, str] | None:
    """The 'external' call. Deterministic here; a real API would slot in behind this."""
    return _GEO.get(city_token.strip().lower())


def _remote_type(text: str, raw_loc: str) -> RemoteType:
    blob = f"{text} {raw_loc}"
    if _HYBRID_RE.search(blob):
        return RemoteType.HYBRID
    if _REMOTE_RE.search(blob):
        return RemoteType.REMOTE
    if _ONSITE_RE.search(blob):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


def normalize_location(posting: RawPosting) -> Location:
    raw = posting.location_raw.strip()
    remote_type = _remote_type(f"{posting.title} {posting.description}", raw)

    # Split "City, State, Country" style strings; try the whole string then the first part.
    parts = [p.strip() for p in re.split(r"[,/|]", raw) if p.strip()]
    city = state = country = None
    resolved = False
    for candidate in ([raw] + parts) if raw else []:
        geo = geocode(candidate)
        if geo is not None:
            state, country = geo
            city = None if candidate.lower() == "remote" else candidate.title()
            resolved = bool(city or country)
            break
    if not resolved and parts:
        # Fall back to positional parse: first token = city, last = country.
        city = parts[0].title()
        if len(parts) >= 2:
            country = parts[-1].title()
        resolved = True

    if remote_type == RemoteType.REMOTE and not resolved:
        resolved = True  # "Remote" is a resolved location even without a city

    return Location(
        city=city or None,
        state=state or None,
        country=country or None,
        remote_type=remote_type,
        raw=raw,
        resolved=resolved,
    )
