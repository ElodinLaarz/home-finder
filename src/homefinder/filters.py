"""Hard structured filters — with measured commute, the only gates that may
exclude a listing. Deterministic and auditable: every drop carries a reason."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import FiltersConfig
from .models import Listing


@dataclass
class FilterDecision:
    listing: Listing
    kept: bool
    reasons: list[str] = field(default_factory=list)


def normalize_property_type(value: str | None) -> str:
    """'Single Family' / 'singleFamily' / 'single-family' -> 'singlefamily'."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def check_listing(listing: Listing, cfg: FiltersConfig) -> FilterDecision:
    reasons: list[str] = []

    def gate(name: str, value, threshold, keep_fn) -> None:
        if threshold is None:
            return
        if value is None:
            if cfg.drop_if_missing:
                reasons.append(f"{name} missing (drop_if_missing=true)")
            return  # fail-open: missing data never silently drops a listing
        if not keep_fn(value, threshold):
            reasons.append(f"{name}={value} fails threshold {threshold}")

    if cfg.property_types:
        allowed = {normalize_property_type(t) for t in cfg.property_types}
        actual = normalize_property_type(listing.property_type)
        if not actual:
            if cfg.drop_if_missing:
                reasons.append("property_type missing (drop_if_missing=true)")
        elif actual not in allowed:
            reasons.append(f"property_type={listing.property_type!r} not allowed")

    gate("price", listing.price, cfg.max_price, lambda v, t: v <= t)
    gate("price", listing.price, cfg.min_price, lambda v, t: v >= t)
    gate("beds", listing.beds, cfg.min_beds, lambda v, t: v >= t)
    gate("baths", listing.baths, cfg.min_baths, lambda v, t: v >= t)
    gate("sqft", listing.sqft, cfg.min_sqft, lambda v, t: v >= t)
    gate("lot_sqft", listing.lot_sqft, cfg.min_lot_sqft, lambda v, t: v >= t)
    gate("year_built", listing.year_built, cfg.min_year_built, lambda v, t: v >= t)
    gate("hoa_fee", listing.hoa_fee, cfg.max_hoa_fee, lambda v, t: v <= t)

    return FilterDecision(listing=listing, kept=not reasons, reasons=reasons)


def apply_hard_filters(
    listings: list[Listing], cfg: FiltersConfig
) -> tuple[list[Listing], list[FilterDecision]]:
    """Returns (kept listings, drop decisions with reasons)."""
    kept: list[Listing] = []
    dropped: list[FilterDecision] = []
    for listing in listings:
        decision = check_listing(listing, cfg)
        if decision.kept:
            kept.append(listing)
        else:
            dropped.append(decision)
    return kept, dropped
