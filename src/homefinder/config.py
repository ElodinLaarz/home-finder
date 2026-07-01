"""Config models and loading. Search parameters come from config.yaml;
secrets come only from environment variables."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator

from .util import WEEKDAYS


class LatLng(BaseModel):
    lat: float
    lng: float


class SearchConfig(BaseModel):
    center: LatLng
    radius_miles: float = 20


class DepartureConfig(BaseModel):
    day_of_week: str = "tuesday"
    time: str = "08:00"
    timezone: str

    @field_validator("day_of_week")
    @classmethod
    def _day(cls, v: str) -> str:
        if v.lower() not in WEEKDAYS:
            raise ValueError(f"day_of_week must be one of {sorted(WEEKDAYS)}")
        return v.lower()

    @field_validator("time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM (24h)")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v


class IsochroneConfig(BaseModel):
    buffer_minutes: int = 10
    profile: str = "driving"
    cache_days: int = 30


class CommuteConfig(BaseModel):
    destination: LatLng
    max_minutes: int
    departure: DepartureConfig
    isochrone: IsochroneConfig = Field(default_factory=IsochroneConfig)


class FiltersConfig(BaseModel):
    property_types: list[str] = Field(default_factory=lambda: ["Single Family"])
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[float] = None
    min_baths: Optional[float] = None
    min_sqft: Optional[int] = None
    min_lot_sqft: Optional[int] = None
    min_year_built: Optional[int] = None
    max_hoa_fee: Optional[int] = None
    drop_if_missing: bool = False


class RubricCriterion(BaseModel):
    key: str
    description: str
    weight: float = 1.0


class ScoringConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5"
    instant_threshold: float = 8.0
    max_per_run: int = 25
    use_street_view: bool = True
    rubric: list[RubricCriterion] = Field(default_factory=list)


class NotifyConfig(BaseModel):
    enabled: bool = True
    digest_max_listings: int = 30


class StateConfig(BaseModel):
    path: str = "homefinder.db"


class RunConfig(BaseModel):
    mark_inactive_after_runs: int = 3


class AppConfig(BaseModel):
    search: SearchConfig
    commute: CommuteConfig
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    run: RunConfig = Field(default_factory=RunConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig.model_validate(data)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name} (see .env.example)"
        )
    return value


def optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None
