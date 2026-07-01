"""Identity resolution.

With a single aggregator every listing carries a stable source id, so the
canonical key is simply "<source>:<source_id>". The cross-source matcher
(APN first, then geohash-block + street similarity) exists for the day a
second source (e.g. a scrape) is added; geohash is a *blocking* technique to
shrink the comparison space, never an identity key on its own.
"""

from __future__ import annotations

import difflib
import math
import re

from .models import Listing

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# Common USPS suffix/direction abbreviations, expanded so "123 Main St" and
# "123 Main Street" normalize identically.
_TOKEN_EXPANSIONS = {
    "st": "street",
    "ave": "avenue",
    "av": "avenue",
    "dr": "drive",
    "rd": "road",
    "ln": "lane",
    "ct": "court",
    "blvd": "boulevard",
    "pl": "place",
    "ter": "terrace",
    "trl": "trail",
    "cir": "circle",
    "hwy": "highway",
    "pkwy": "parkway",
    "sq": "square",
    "wy": "way",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
}


def canonical_id(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


def geohash_encode(lat: float, lng: float, precision: int = 8) -> str:
    lat_lo, lat_hi = -90.0, 90.0
    lng_lo, lng_hi = -180.0, 180.0
    chars: list[str] = []
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    even = True  # even bit -> longitude
    while len(chars) < precision:
        if even:
            mid = (lng_lo + lng_hi) / 2
            if lng >= mid:
                ch |= bits[bit]
                lng_lo = mid
            else:
                lng_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch |= bits[bit]
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            chars.append(_BASE32[ch])
            bit = 0
            ch = 0
    return "".join(chars)


def normalize_street(street: str) -> str:
    tokens = re.sub(r"[^\w\s]", " ", street.lower()).split()
    return " ".join(_TOKEN_EXPANSIONS.get(t, t) for t in tokens)


def street_number(street: str) -> str | None:
    m = re.match(r"\s*(\d+)", street)
    return m.group(1) if m else None


def _normalize_apn(apn: str) -> str:
    return re.sub(r"[^a-z0-9]", "", apn.lower())


def same_home(a: Listing, b: Listing, street_similarity: float = 0.85) -> bool:
    """Cross-source match: APN equality when both sides have one, otherwise
    same street number + fuzzy street-name similarity (+ zip when both known).
    Callers should block candidates by geohash prefix first (precision ~7)."""
    if a.apn and b.apn:
        return _normalize_apn(a.apn) == _normalize_apn(b.apn)

    sa, sb = a.street or a.address, b.street or b.address
    na, nb = street_number(sa), street_number(sb)
    if not na or na != nb:
        return False
    if a.zip_code and b.zip_code and a.zip_code != b.zip_code:
        return False
    ratio = difflib.SequenceMatcher(
        None, normalize_street(sa), normalize_street(sb)
    ).ratio()
    return ratio >= street_similarity


def _approx_meters(a: Listing, b: Listing) -> float:
    """Equirectangular distance — fine at neighborhood scale."""
    lat_m = (a.lat - b.lat) * 111_320
    lng_m = (a.lng - b.lng) * 111_320 * math.cos(math.radians((a.lat + b.lat) / 2))
    return math.hypot(lat_m, lng_m)


def find_match(
    listing: Listing, candidates: list[Listing], max_meters: float = 250
) -> Listing | None:
    """Find an existing listing that is the same physical home. Blocks by
    distance rather than geohash-prefix equality: a point can straddle a cell
    boundary, and at single-metro scale the distance check is cheap anyway.
    (The geohash column still exists for DB-side blocking if this ever needs
    to scale past in-memory comparison.)"""
    for candidate in candidates:
        if _approx_meters(listing, candidate) > max_meters:
            continue
        if same_home(listing, candidate):
            return candidate
    return None
