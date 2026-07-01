from homefinder.routing import parse_matrix_response


CHUNK = [("id-a", 39.0, -83.0), ("id-b", 39.1, -83.1), ("id-c", 39.2, -83.2)]


def test_parse_maps_by_index_not_position():
    # Elements arrive out of order; originIndex 0 is ELIDED by proto3 JSON.
    elements = [
        {"originIndex": 2, "condition": "ROUTE_EXISTS", "duration": "600s", "status": {}},
        {"condition": "ROUTE_EXISTS", "duration": "1712s", "status": {}},  # index 0
        {"originIndex": 1, "condition": "ROUTE_NOT_FOUND", "status": {}},
    ]
    result = dict(parse_matrix_response(elements, CHUNK))
    assert result["id-c"] == 10.0
    assert result["id-a"] == round(1712 / 60, 1)
    assert result["id-b"] is None


def test_parse_transient_element_error_is_omitted_for_retry():
    # An error status is transient (quota blip etc.) — the listing must stay
    # UNCHECKED so it is re-routed next run, not persisted as "no route".
    elements = [
        {"originIndex": 0, "status": {"code": 8, "message": "quota"}},
    ]
    assert parse_matrix_response(elements, CHUNK) == []


def test_parse_elided_zero_duration():
    # proto3 elides zero values — a 0s duration arrives with no duration key.
    elements = [{"originIndex": 0, "condition": "ROUTE_EXISTS", "status": {}}]
    assert dict(parse_matrix_response(elements, CHUNK))["id-a"] == 0.0


def test_out_of_range_index_ignored():
    elements = [{"originIndex": 99, "condition": "ROUTE_EXISTS", "duration": "60s"}]
    assert parse_matrix_response(elements, CHUNK) == []
