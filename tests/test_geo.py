from homefinder.config import CommuteConfig, DepartureConfig, LatLng
from homefinder.geo import GeoGate, contour_minutes, fallback_disk, geojson_to_area


def commute_cfg(max_minutes=35, buffer_minutes=10) -> CommuteConfig:
    return CommuteConfig(
        destination=LatLng(lat=39.96, lng=-83.0),
        max_minutes=max_minutes,
        departure=DepartureConfig(timezone="America/New_York"),
        isochrone={"buffer_minutes": buffer_minutes},
    )


def test_contour_minutes_padded_and_capped():
    assert contour_minutes(commute_cfg(35, 10)) == 45
    assert contour_minutes(commute_cfg(55, 20)) == 60  # Mapbox hard cap


def test_budget_over_contour_cap_falls_back_to_disk(store):
    # A clamped isochrone would be SMALLER than the real gate (rule-1 breach);
    # the gate must fall back to the uncapped crow-flies disk instead.
    from homefinder.geo import load_gate

    cfg = commute_cfg(max_minutes=70, buffer_minutes=10)
    gate = load_gate(store, cfg, token="fake-token", client=None, now="2026-07-01T10:00:00+00:00")
    assert gate.kind == "disk"
    # disk radius reflects the full 80-minute budget (~120 km at 90 km/h)
    assert gate.contains(40.9, -83.0)  # ~105 km north of destination


def test_fallback_disk_contains_center_and_nearby():
    cfg = commute_cfg()
    gate = GeoGate(fallback_disk(cfg), "disk")
    assert gate.contains(39.96, -83.0)
    assert gate.contains(40.2, -83.0)  # ~27km north, inside a 45min@90kmh disk
    assert not gate.contains(41.5, -83.0)  # ~170km north


def test_geojson_union_handles_multiple_features():
    # denoise<1 can return several disjoint Polygon features per contour;
    # the gate must accept points in ANY of them.
    geojson = {
        "features": [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                }
            },
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
                }
            },
        ]
    }
    gate = GeoGate(geojson_to_area(geojson), "isochrone")
    assert gate.contains(0.5, 0.5)
    assert gate.contains(5.5, 5.5)
    assert not gate.contains(3.0, 3.0)


def test_boundary_point_passes():
    geojson = {
        "features": [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                }
            }
        ]
    }
    gate = GeoGate(geojson_to_area(geojson), "isochrone")
    assert gate.contains(0.0, 0.5)  # exactly on the edge -> generous gate keeps it
