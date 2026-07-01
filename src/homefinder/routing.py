"""Stage-2 precise gate: Google Routes API computeRouteMatrix, traffic-aware.

Runs only on listings that cleared the cheap gates and have no stored
commute — a handful per run after the initial seed.

Wire facts (verified against developers.google.com, 2026-07):
- POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix
- X-Goog-FieldMask header is REQUIRED; include `status` or every element
  looks successful.
- proto3 JSON elides zero-valued fields: a missing originIndex means 0.
- duration comes back as a seconds string like "712s".
- departureTime must be RFC3339 UTC and in the future.
- Billing is per element on the "Compute Route Matrix Pro" SKU when
  TRAFFIC_AWARE (5,000 free elements/month, then $10/1k) — one element per
  listing here, so the free cap is ~5,000 new listings/month.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .config import LatLng

log = logging.getLogger(__name__)

MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "originIndex,destinationIndex,status,condition,duration"
CHUNK_SIZE = 100  # max 625 elements/request with one destination; keep requests small


def compute_commutes(
    origins: list[tuple[str, float, float]],  # (listing_id, lat, lng)
    destination: LatLng,
    departure: datetime,
    api_key: str,
    client: httpx.Client,
) -> dict[str, float | None]:
    """Predicted door-to-door drive minutes at the departure time.

    Returns listing_id -> minutes, or None when no drivable route was found
    (the caller keeps such listings, flagged, rather than dropping them).
    """
    departure_utc = departure.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results: dict[str, float | None] = {}

    for start in range(0, len(origins), CHUNK_SIZE):
        chunk = origins[start : start + CHUNK_SIZE]
        body = {
            "origins": [
                {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}
                for (_listing_id, lat, lng) in chunk
            ],
            "destinations": [
                {
                    "waypoint": {
                        "location": {
                            "latLng": {
                                "latitude": destination.lat,
                                "longitude": destination.lng,
                            }
                        }
                    }
                }
            ],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "departureTime": departure_utc,
        }
        response = client.post(
            MATRIX_URL,
            json=body,
            headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Google Routes matrix failed: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        for listing_id, minutes in parse_matrix_response(response.json(), chunk):
            results[listing_id] = minutes
        log.info(
            "routing: chunk %d-%d routed (%d elements)",
            start,
            start + len(chunk),
            len(chunk),
        )
    return results


def parse_matrix_response(
    elements: list[dict], chunk: list[tuple[str, float, float]]
) -> list[tuple[str, float | None]]:
    """Only definitive answers are returned. A per-element error status is a
    transient failure (quota blip, backend error) — those listings are OMITTED
    so their commute stays unchecked and gets retried next run, instead of
    being persisted as a permanent "no route"."""
    out: list[tuple[str, float | None]] = []
    for element in elements:
        origin_index = element.get("originIndex", 0)  # proto3 elides 0
        if not 0 <= origin_index < len(chunk):
            log.warning("routing: element with out-of-range originIndex: %s", element)
            continue
        listing_id = chunk[origin_index][0]
        status = element.get("status") or {}
        if status.get("code"):
            log.warning(
                "routing: transient element error for %s (will retry next run): %s",
                listing_id, status,
            )
        elif element.get("condition") != "ROUTE_EXISTS":
            out.append((listing_id, None))  # definitive: no drivable route
        else:
            # proto3 elides a zero duration entirely
            seconds = float(element.get("duration", "0s").rstrip("s"))
            out.append((listing_id, round(seconds / 60.0, 1)))
    return out
