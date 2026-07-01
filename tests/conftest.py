import sqlite3

import pytest

from homefinder.models import Listing
from homefinder.store import Store


def make_listing(**overrides) -> Listing:
    defaults = dict(
        id="rentcast:123-Main-St,-Springfield,-OH-45501",
        source="rentcast",
        source_id="123-Main-St,-Springfield,-OH-45501",
        address="123 Main St, Springfield, OH 45501",
        street="123 Main St",
        city="Springfield",
        state="OH",
        zip_code="45501",
        lat=39.92,
        lng=-83.81,
        geohash="dph9m1v2",
        property_type="Single Family",
        price=300000,
        beds=3,
        baths=2,
        sqft=1800,
        lot_sqft=8000,
        year_built=1995,
        status="Active",
    )
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture
def store() -> Store:
    return Store(sqlite3.connect(":memory:"))
