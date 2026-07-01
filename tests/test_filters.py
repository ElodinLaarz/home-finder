from conftest import make_listing

from homefinder.config import FiltersConfig
from homefinder.filters import apply_hard_filters, check_listing, normalize_property_type


def test_property_type_normalization():
    assert normalize_property_type("Single Family") == "singlefamily"
    assert normalize_property_type("single-family") == "singlefamily"
    assert normalize_property_type(None) == ""


def test_type_gate_rejects_condo():
    cfg = FiltersConfig(property_types=["Single Family"])
    decision = check_listing(make_listing(property_type="Condo"), cfg)
    assert not decision.kept
    assert "property_type" in decision.reasons[0]


def test_numeric_gates():
    cfg = FiltersConfig(max_price=250000, min_beds=4, min_sqft=2000)
    decision = check_listing(make_listing(), cfg)
    assert not decision.kept
    assert len(decision.reasons) == 3  # price, beds, sqft all fail


def test_missing_fields_fail_open_by_default():
    cfg = FiltersConfig(min_sqft=2000, min_year_built=2000)
    listing = make_listing(sqft=None, year_built=None)
    assert check_listing(listing, cfg).kept


def test_missing_fields_fail_closed_when_configured():
    cfg = FiltersConfig(min_sqft=2000, drop_if_missing=True)
    listing = make_listing(sqft=None)
    decision = check_listing(listing, cfg)
    assert not decision.kept
    assert "missing" in decision.reasons[0]


def test_apply_hard_filters_splits():
    cfg = FiltersConfig(max_price=350000)
    good = make_listing()
    bad = make_listing(id="rentcast:x", source_id="x", price=400000)
    kept, dropped = apply_hard_filters([good, bad], cfg)
    assert [l.id for l in kept] == [good.id]
    assert [d.listing.id for d in dropped] == [bad.id]


def test_hoa_gate():
    cfg = FiltersConfig(max_hoa_fee=100)
    assert not check_listing(make_listing(hoa_fee=250), cfg).kept
    assert check_listing(make_listing(hoa_fee=50), cfg).kept
    assert check_listing(make_listing(hoa_fee=None), cfg).kept  # fail-open
