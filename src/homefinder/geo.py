"""Stage-1 geographic pre-filter — the cheap, deliberately generous gate.

Primary: a Mapbox isochrone polygon computed with an off-peak profile and
padded minutes, so it is strictly larger than the true peak-hour reachable
area and can never false-exclude a home the precise Stage-2 check would keep.

Fallback (no MAPBOX_TOKEN): a crow-flies disk sized at a highway-speed
average, which over-approximates even harder. Two things make the fallback
worth having: the tool stays runnable with one less account, and Mapbox's
ToS expects Isochrone results to be displayed on a Mapbox map — a headless
gate is a gray area the disk avoids entirely, at slightly higher Stage-2
routing volume.

The polygon is cached in the state DB's kv table and refreshed after
cache_days (it only changes if the destination or time budget changes).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta

import httpx
from shapely.affinity import scale
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from .config import CommuteConfig
from .store import Store

log = logging.getLogger(__name__)

ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}"
MAX_CONTOUR_MINUTES = 60  # hard Mapbox limit on contours_minutes
FALLBACK_KMH = 90.0  # faster than any real commute average -> generous disk


class GeoGate:
    def __init__(self, area, kind: str) -> None:
        self.area = area
        self.kind = kind  # "isochrone" | "disk"
        self._prepared = prep(area)

    def contains(self, lat: float, lng: float) -> bool:
        # intersects (not contains) so boundary points pass — generous gate.
        return self._prepared.intersects(Point(lng, lat))


def padded_minutes(cfg: CommuteConfig) -> int:
    return cfg.max_minutes + cfg.isochrone.buffer_minutes


def contour_minutes(cfg: CommuteConfig) -> int:
    return min(padded_minutes(cfg), MAX_CONTOUR_MINUTES)


def _cache_key(cfg: CommuteConfig) -> str:
    return (
        f"isochrone:{cfg.destination.lat:.5f},{cfg.destination.lng:.5f}"
        f":{contour_minutes(cfg)}:{cfg.isochrone.profile}"
    )


def fetch_isochrone_geojson(
    cfg: CommuteConfig, token: str, client: httpx.Client
) -> dict:
    url = ISOCHRONE_URL.format(
        profile=cfg.isochrone.profile,
        lng=cfg.destination.lng,
        lat=cfg.destination.lat,
    )
    response = client.get(
        url,
        params={
            "contours_minutes": contour_minutes(cfg),
            "polygons": "true",
            # keep disjoint reachable pockets rather than denoising them away;
            # point-in-polygon unions ALL returned features.
            "denoise": "0.2",
            "access_token": token,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Mapbox isochrone failed: HTTP {response.status_code} {response.text[:300]}"
        )
    return response.json()


def geojson_to_area(geojson: dict):
    geometries = [shape(f["geometry"]) for f in geojson.get("features", [])]
    if not geometries:
        raise RuntimeError("Mapbox isochrone response contained no polygon features")
    return unary_union(geometries)


def fallback_disk(cfg: CommuteConfig):
    """Crow-flies disk in degrees, corrected for longitude compression.
    Uses the UNCAPPED padded budget — unlike isochrones it has no 60-min limit."""
    radius_km = FALLBACK_KMH * padded_minutes(cfg) / 60.0
    lat = cfg.destination.lat
    dlat = radius_km / 111.32
    dlng = radius_km / (111.32 * math.cos(math.radians(lat)))
    unit_circle = Point(cfg.destination.lng, lat).buffer(1.0, quad_segs=32)
    return scale(unit_circle, xfact=dlng, yfact=dlat, origin=(cfg.destination.lng, lat))


def load_gate(
    store: Store,
    cfg: CommuteConfig,
    token: str | None,
    client: httpx.Client,
    now: str,
) -> GeoGate:
    if not token:
        log.info("geo: no MAPBOX_TOKEN — using generous crow-flies disk pre-filter")
        return GeoGate(fallback_disk(cfg), "disk")
    if padded_minutes(cfg) > MAX_CONTOUR_MINUTES:
        # A clamped isochrone would be SMALLER than the real commute gate and
        # could false-exclude — rule 1 forbids that. Use the uncapped disk.
        log.warning(
            "geo: commute budget %d min exceeds Mapbox's %d-min contour cap — "
            "using crow-flies disk instead of a too-small isochrone",
            padded_minutes(cfg), MAX_CONTOUR_MINUTES,
        )
        return GeoGate(fallback_disk(cfg), "disk")

    key = _cache_key(cfg)
    cached = store.get_kv(key)
    if cached is not None:
        value, updated_at = cached
        age = datetime.fromisoformat(now) - datetime.fromisoformat(updated_at)
        if age <= timedelta(days=cfg.isochrone.cache_days):
            log.info("geo: using cached isochrone (age %sd)", age.days)
            return GeoGate(geojson_to_area(json.loads(value)), "isochrone")

    geojson = fetch_isochrone_geojson(cfg, token, client)
    area = geojson_to_area(geojson)  # validate before caching
    store.set_kv(key, json.dumps(geojson), now)
    log.info("geo: fetched fresh isochrone (%d min contour)", contour_minutes(cfg))
    return GeoGate(area, "isochrone")
