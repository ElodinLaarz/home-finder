from conftest import make_listing

from homefinder.dedupe import (
    canonical_id,
    find_match,
    geohash_encode,
    normalize_street,
    same_home,
    street_number,
)


def test_geohash_known_value():
    # Classic reference point from the geohash spec examples.
    assert geohash_encode(57.64911, 10.40744, precision=8) == "u4pruydq"


def test_geohash_precision():
    assert len(geohash_encode(39.0, -84.0, precision=6)) == 6


def test_canonical_id():
    assert canonical_id("rentcast", "abc") == "rentcast:abc"


def test_normalize_street_expands_abbreviations():
    assert normalize_street("123 N Main St") == "123 north main street"
    assert normalize_street("123 North Main Street") == "123 north main street"


def test_street_number():
    assert street_number("123 Main St") == "123"
    assert street_number("Main St") is None


def test_same_home_by_apn():
    a = make_listing(apn="12-345-678")
    b = make_listing(id="other:x", source="other", source_id="x", apn="12345678")
    assert same_home(a, b)


def test_same_home_by_fuzzy_street():
    a = make_listing(street="123 N Main St")
    b = make_listing(
        id="other:x", source="other", source_id="x", street="123 North Main Street"
    )
    assert same_home(a, b)


def test_different_street_number_never_matches():
    a = make_listing(street="123 Main St")
    b = make_listing(id="other:x", source="other", source_id="x", street="125 Main St")
    assert not same_home(a, b)


def test_find_match_blocks_by_distance():
    target = make_listing()
    far_same_street = make_listing(
        id="other:far", source="other", source_id="far", lat=39.99, lng=-83.81
    )
    near = make_listing(
        id="other:near",
        source="other",
        source_id="near",
        lat=target.lat + 0.0001,
        lng=target.lng,
        street="123 Main Street",
    )
    assert find_match(target, [far_same_street, near]) is near
