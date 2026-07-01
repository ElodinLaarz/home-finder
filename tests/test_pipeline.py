"""End-to-end pipeline runs against mocked HTTP APIs (no scoring, no Telegram)."""

import json
import sqlite3

import pytest
import respx
from httpx import Response

from homefinder.config import AppConfig
from homefinder.pipeline import run

RENTCAST_URL = "https://api.rentcast.io/v1/listings/sale"
ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

ROUTE_OK = Response(
    200, json=[{"condition": "ROUTE_EXISTS", "duration": "1500s", "status": {}}]
)


def rentcast_listing(id_, lat, lng, property_type="Single Family", price=300000):
    return {
        "id": id_,
        "formattedAddress": f"{id_} St, Springfield, OH 45501",
        "addressLine1": f"{id_} St",
        "city": "Springfield",
        "state": "OH",
        "zipCode": "45501",
        "latitude": lat,
        "longitude": lng,
        "propertyType": property_type,
        "bedrooms": 3,
        "bathrooms": 2,
        "squareFootage": 1800,
        "lotSize": 8000,
        "yearBuilt": 1995,
        "status": "Active",
        "price": price,
        "listedDate": "2026-06-20T00:00:00.000Z",
        "daysOnMarket": 11,
    }


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "search": {"center": {"lat": 39.96, "lng": -83.0}, "radius_miles": 20},
            "commute": {
                "destination": {"lat": 39.96, "lng": -83.0},
                "max_minutes": 35,
                "departure": {
                    "day_of_week": "tuesday",
                    "time": "08:00",
                    "timezone": "America/New_York",
                },
            },
            "filters": {"property_types": ["Single Family"], "max_price": 500000},
            "scoring": {"enabled": False, "rubric": []},
            "notify": {"enabled": False},
            "state": {"path": str(tmp_path / "state.db")},
        }
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    for name in ("MAPBOX_TOKEN", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def db_rows(cfg, sql):
    conn = sqlite3.connect(cfg.state.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


@respx.mock
def test_cold_start_guard_requires_seed(cfg, env):
    with pytest.raises(RuntimeError, match="--seed"):
        run(cfg)


@respx.mock
def test_full_run_and_second_run(cfg, env, capsys):
    listings = [
        rentcast_listing("100-Main", 39.97, -83.01),  # keeper
        rentcast_listing("200-Condo", 39.97, -83.02, property_type="Condo"),
        rentcast_listing("300-Far", 45.0, -83.0),  # outside crow-flies disk
    ]
    rc = respx.get(RENTCAST_URL)
    routes = respx.post(ROUTES_URL).mock(return_value=ROUTE_OK)

    # Seed against an empty market (RentCast signals no matches with a 404).
    rc.mock(return_value=Response(404, json={"error": "No listings found"}))
    run(cfg, seed=True)

    rc.mock(return_value=Response(200, json=listings))
    stats = run(cfg)
    assert stats.n_fetched == 3
    assert stats.n_after_geo == 2  # far listing geo-dropped
    assert stats.n_after_filters == 1  # condo dropped
    assert stats.n_new == 1
    assert routes.called
    body = json.loads(routes.calls.last.request.content)
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    assert len(body["origins"]) == 1

    printed = capsys.readouterr().out
    assert "100-Main St" in printed
    assert "25 min drive" in printed

    # Second run: same feed -> everything unchanged, no routing, no digest.
    routes.reset()
    stats2 = run(cfg)
    assert stats2.n_new == 0
    assert stats2.n_changed == 0
    assert not routes.called
    assert "100-Main St" not in capsys.readouterr().out


@respx.mock
def test_commute_over_threshold_excluded_from_digest(cfg, env, capsys):
    rc = respx.get(RENTCAST_URL).mock(return_value=Response(404))
    respx.post(ROUTES_URL).mock(
        return_value=Response(
            200,
            json=[{"condition": "ROUTE_EXISTS", "duration": "3600s", "status": {}}],
        )
    )
    run(cfg, seed=True)
    rc.mock(
        return_value=Response(200, json=[rentcast_listing("100-Main", 39.97, -83.01)])
    )
    stats = run(cfg)
    assert stats.n_new == 1  # tracked in state
    assert "100-Main St" not in capsys.readouterr().out  # gated out of the digest


@respx.mock
def test_listing_failing_filters_is_not_marked_missing(cfg, env, capsys):
    """A still-listed home whose price rises above the ceiling stays active in
    state — 'missing' means absent from the FEED, not failing a gate."""
    rc = respx.get(RENTCAST_URL).mock(
        return_value=Response(200, json=[rentcast_listing("100-Main", 39.97, -83.01)])
    )
    respx.post(ROUTES_URL).mock(return_value=ROUTE_OK)
    run(cfg, seed=True)

    # price jumps over max_price=500000; listing still in the feed
    rc.mock(
        return_value=Response(
            200, json=[rentcast_listing("100-Main", 39.97, -83.01, price=600000)]
        )
    )
    for _ in range(4):  # more runs than mark_inactive_after_runs=3
        run(cfg)

    rows = db_rows(cfg, "SELECT is_active, missing_runs FROM listings")
    assert rows == [{"is_active": 1, "missing_runs": 0}]


@respx.mock
def test_seed_run_skips_notification(cfg, env, capsys):
    respx.get(RENTCAST_URL).mock(
        return_value=Response(200, json=[rentcast_listing("100-Main", 39.97, -83.01)])
    )
    respx.post(ROUTES_URL).mock(return_value=ROUTE_OK)
    stats = run(cfg, seed=True)
    assert stats.seed is True
    assert "100-Main" not in capsys.readouterr().out


@respx.mock
def test_dry_run_skips_routing_and_rolls_back(cfg, env, capsys):
    respx.get(RENTCAST_URL).mock(
        return_value=Response(200, json=[rentcast_listing("100-Main", 39.97, -83.01)])
    )
    routes = respx.post(ROUTES_URL).mock(return_value=ROUTE_OK)

    stats = run(cfg, dry_run=True)
    assert stats.n_new == 1
    assert not routes.called  # dry runs must not spend routing budget
    printed = capsys.readouterr().out
    assert "100-Main St" in printed
    assert "commute unchecked" in printed

    # State was rolled back -> the same listing is NEW again next time.
    stats2 = run(cfg, dry_run=True)
    assert stats2.n_new == 1
    assert db_rows(cfg, "SELECT COUNT(*) AS n FROM listings") == [{"n": 0}]


@respx.mock
def test_limit_skips_missing_bookkeeping(cfg, env, capsys):
    listings = [
        rentcast_listing("100-Main", 39.97, -83.01),
        rentcast_listing("101-Oak", 39.97, -83.02),
    ]
    rc = respx.get(RENTCAST_URL).mock(return_value=Response(200, json=listings))
    respx.post(ROUTES_URL).mock(return_value=ROUTE_OK)
    run(cfg, seed=True)

    # Repeated truncated debug runs must not bump missing_runs on the rest.
    for _ in range(4):
        run(cfg, limit=1)
    rows = db_rows(cfg, "SELECT missing_runs, is_active FROM listings")
    assert all(r == {"missing_runs": 0, "is_active": 1} for r in rows)
