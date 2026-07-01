"""Google Street View Static imagery.

RentCast provides no listing photos, so a Street View shot of the address is
the only visual signal available to the scoring model — useful for street
context (road type, adjacency, curb appeal), not interiors.

The metadata endpoint is free and consumes no quota; the image endpoint is
checked against it first so a no-imagery location never bills (and never
sends the model a gray placeholder). Images bill on the Street View Static
SKU: 10,000 free/month, then $7/1k.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"
IMAGE_SIZE = "640x400"  # max is 640x640


def fetch_street_view(
    lat: float, lng: float, api_key: str, client: httpx.Client
) -> bytes | None:
    """JPEG bytes of the nearest street-level pano, or None if unavailable."""
    location = f"{lat},{lng}"
    try:
        meta = client.get(
            METADATA_URL, params={"location": location, "key": api_key}
        ).json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("streetview: metadata check failed for %s: %s", location, e)
        return None
    if meta.get("status") != "OK":
        return None

    try:
        response = client.get(
            IMAGE_URL,
            params={
                "size": IMAGE_SIZE,
                "location": location,
                "fov": 90,
                "return_error_code": "true",
                "key": api_key,
            },
        )
    except httpx.HTTPError as e:
        log.warning("streetview: image fetch failed for %s: %s", location, e)
        return None
    if response.status_code != 200:
        log.warning("streetview: image fetch failed (%d) for %s", response.status_code, location)
        return None
    return response.content
