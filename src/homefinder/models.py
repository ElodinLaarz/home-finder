"""Normalized data models shared across pipeline stages."""

from __future__ import annotations

import enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChangeType(str, enum.Enum):
    NEW = "new"
    PRICE_CHANGED = "price_changed"
    STATUS_CHANGED = "status_changed"
    BACK_ON_MARKET = "back_on_market"
    UNCHANGED = "unchanged"


class Listing(BaseModel):
    """A for-sale listing normalized from any source."""

    id: str  # canonical identity, e.g. "rentcast:<source_id>"
    source: str = "rentcast"
    source_id: str
    address: str
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    lat: float
    lng: float
    geohash: str = ""
    property_type: Optional[str] = None
    price: Optional[int] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_sqft: Optional[int] = None
    year_built: Optional[int] = None
    hoa_fee: Optional[int] = None
    status: str = "Active"
    listed_date: Optional[str] = None
    days_on_market: Optional[int] = None
    mls_name: Optional[str] = None
    mls_number: Optional[str] = None
    apn: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Change(BaseModel):
    """Result of reconciling one fetched listing against stored state."""

    listing: Listing
    change: ChangeType
    old_price: Optional[int] = None
    old_status: Optional[str] = None


class CriterionScore(BaseModel):
    key: str
    score: float  # 0-10
    rationale: str


class ListingScore(BaseModel):
    listing_id: str
    overall: float  # weighted 0-10
    criteria: list[CriterionScore] = Field(default_factory=list)
    summary: str = ""
    model: str = ""
    scored_at: str = ""  # UTC ISO-8601


class CommuteResult(BaseModel):
    listing_id: str
    minutes: Optional[float] = None  # None = no drivable route / lookup failed
    ok: bool = False  # minutes <= threshold (False when minutes is None)


class RunStats(BaseModel):
    run_id: str
    started_at: str
    finished_at: str = ""
    n_fetched: int = 0
    n_after_geo: int = 0
    n_after_filters: int = 0
    n_new: int = 0
    n_changed: int = 0
    n_scored: int = 0
    n_notified: int = 0
    seed: bool = False
    notes: str = ""
