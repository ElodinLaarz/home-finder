"""RentCast for-sale listings client.

Endpoint facts (verified against developers.rentcast.io, 2026-07):
- GET https://api.rentcast.io/v1/listings/sale, auth via X-Api-Key header.
- Geographic search: latitude/longitude/radius (miles, max 100).
- status defaults to Active; propertyType accepts e.g. "Single Family".
- Pagination: limit (max 500) / offset; body is a bare JSON array. Results
  sort by lastSeenDate desc, which can shuffle between pages of a live
  dataset — pages are merged by id to dedupe.
- Payload contains NO photos, NO description text, NO APN, NO portal URL.
- `id` is an address slug, stable per PROPERTY (a relist reuses it) — which
  is exactly the identity the state store wants.
- Every HTTP request counts against the monthly quota (free tier: 50/month).
"""

from __future__ import annotations

import logging

import httpx

from .config import FiltersConfig, SearchConfig
from .dedupe import canonical_id, geohash_encode
from .models import Listing

log = logging.getLogger(__name__)

BASE_URL = "https://api.rentcast.io/v1/listings/sale"
PAGE_LIMIT = 500
MAX_PAGES = 20  # safety valve: 10k listings is far beyond a single-area search

SOURCE = "rentcast"


def fetch_sale_listings(
    search: SearchConfig,
    filters: FiltersConfig,
    api_key: str,
    client: httpx.Client,
) -> list[dict]:
    """Pull all active sale listings in the search area, merged across pages."""
    params: dict = {
        "latitude": search.center.lat,
        "longitude": search.center.lng,
        "radius": search.radius_miles,
        "status": "Active",
        "limit": PAGE_LIMIT,
    }
    # Server-side propertyType filter saves pages (= API credits) when the
    # config wants exactly one type; the local hard filter still re-verifies.
    if len(filters.property_types) == 1:
        params["propertyType"] = filters.property_types[0]

    merged: dict[str, dict] = {}
    for page_index in range(MAX_PAGES):
        response = client.get(
            BASE_URL,
            params={**params, "offset": page_index * PAGE_LIMIT},
            headers={"X-Api-Key": api_key},
        )
        if response.status_code == 404:
            # RentCast signals "no records match" (including paginating past
            # the last page) with a 404, not an empty array.
            break
        if response.status_code != 200:
            raise RuntimeError(
                f"RentCast request failed: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError(f"RentCast returned unexpected payload: {page!r:.300}")
        for item in page:
            if item.get("id"):
                merged[str(item["id"])] = item
        log.info("rentcast: page %d returned %d listings", page_index + 1, len(page))
        if len(page) < PAGE_LIMIT:
            break
    else:
        log.warning("rentcast: hit MAX_PAGES=%d; results may be truncated", MAX_PAGES)
    return list(merged.values())


def normalize(raw: dict) -> Listing | None:
    """Map a RentCast sale-listing object to the internal model.

    Returns None for records unusable downstream (no coordinates — nothing
    to geo-gate or route).
    """
    lat, lng = raw.get("latitude"), raw.get("longitude")
    if lat is None or lng is None:
        return None
    source_id = str(raw["id"])
    hoa = raw.get("hoa") or {}
    return Listing(
        id=canonical_id(SOURCE, source_id),
        source=SOURCE,
        source_id=source_id,
        address=raw.get("formattedAddress") or source_id,
        street=raw.get("addressLine1"),
        city=raw.get("city"),
        state=raw.get("state"),
        zip_code=raw.get("zipCode"),
        lat=lat,
        lng=lng,
        geohash=geohash_encode(lat, lng),
        property_type=raw.get("propertyType"),
        price=raw.get("price"),
        beds=raw.get("bedrooms"),
        baths=raw.get("bathrooms"),
        sqft=raw.get("squareFootage"),
        lot_sqft=raw.get("lotSize"),  # already square feet
        year_built=raw.get("yearBuilt"),
        hoa_fee=hoa.get("fee"),
        status=raw.get("status") or "Active",
        listed_date=raw.get("listedDate"),
        days_on_market=raw.get("daysOnMarket"),
        mls_name=raw.get("mlsName"),
        mls_number=raw.get("mlsNumber"),
        raw=raw,
    )


def normalize_all(raw_listings: list[dict]) -> tuple[list[Listing], int]:
    """Returns (normalized listings, count skipped for missing coordinates)."""
    listings: list[Listing] = []
    skipped = 0
    for raw in raw_listings:
        try:
            listing = normalize(raw)
        except Exception as e:
            # One malformed record must never take the whole run down.
            skipped += 1
            log.warning("rentcast: skipping malformed listing %s: %s", raw.get("id"), e)
            continue
        if listing is None:
            skipped += 1
            log.warning("rentcast: skipping listing without coordinates: %s", raw.get("id"))
        else:
            listings.append(listing)
    return listings, skipped
