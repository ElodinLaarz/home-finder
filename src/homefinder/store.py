"""State store: SQLite semantics everywhere.

Local runs use a plain SQLite file. When TURSO_DATABASE_URL/TURSO_AUTH_TOKEN
are set, the same file becomes a libSQL embedded replica synced against the
remote Turso database — same SQL, one extra sync() call. The GitHub Actions
concurrency guard makes this single-writer, so no locking is needed anywhere.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Optional

from .config import StateConfig, optional_env
from .models import Change, ChangeType, Listing, ListingScore, RunStats

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    apn TEXT,
    address TEXT NOT NULL,
    street TEXT, city TEXT, state TEXT, zip_code TEXT,
    lat REAL NOT NULL, lng REAL NOT NULL, geohash TEXT NOT NULL DEFAULT '',
    property_type TEXT,
    price INTEGER, beds REAL, baths REAL, sqft INTEGER, lot_sqft INTEGER,
    year_built INTEGER, hoa_fee INTEGER,
    status TEXT NOT NULL DEFAULT 'Active',
    listed_date TEXT, days_on_market INTEGER,
    mls_name TEXT, mls_number TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    commute_minutes REAL,
    commute_checked_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    missing_runs INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listings_geohash ON listings(geohash);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price INTEGER,
    status TEXT,
    PRIMARY KEY (listing_id, observed_at)
);

CREATE TABLE IF NOT EXISTS scores (
    listing_id TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    model TEXT,
    overall REAL,
    commute_minutes REAL,
    criteria_json TEXT,
    summary TEXT,
    PRIMARY KEY (listing_id, scored_at)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    n_fetched INTEGER, n_after_geo INTEGER, n_after_filters INTEGER,
    n_new INTEGER, n_changed INTEGER, n_scored INTEGER, n_notified INTEGER,
    seed INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MUTABLE_COLUMNS = (
    "price",
    "beds",
    "baths",
    "sqft",
    "lot_sqft",
    "year_built",
    "hoa_fee",
    "status",
    "days_on_market",
    "raw_json",
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def open_connection(cfg: StateConfig):
    """Plain SQLite locally; libSQL embedded replica when Turso env vars set."""
    url = optional_env("TURSO_DATABASE_URL")
    if url:
        token = optional_env("TURSO_AUTH_TOKEN")
        try:
            import libsql  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "TURSO_DATABASE_URL is set but the 'libsql' package is not "
                "installed (uv add libsql). Unset the env var to use plain SQLite."
            ) from e
        conn = libsql.connect(cfg.path, sync_url=url, auth_token=token)
        conn.sync()
        return conn
    conn = sqlite3.connect(cfg.path)
    return conn


class Store:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.init_schema()

    @classmethod
    def open(cls, cfg: StateConfig) -> "Store":
        return cls(open_connection(cfg))

    def init_schema(self) -> None:
        for statement in SCHEMA.strip().split(";\n"):
            if statement.strip():
                self.conn.execute(statement)
        self.conn.commit()
        # Explicit transaction so dry-run rollback doesn't depend on the
        # driver's implicit-BEGIN behavior (sqlite3 vs libsql may differ).
        try:
            self.conn.execute("BEGIN")
        except Exception:
            pass  # driver already opened a transaction implicitly

    def commit_and_sync(self) -> None:
        self.conn.commit()
        sync = getattr(self.conn, "sync", None)
        if sync is not None:
            sync()

    def checkpoint(self) -> None:
        """Commit (and sync, when remote) work so far, then open a new
        transaction. Used right after paid API results (routing, scores) are
        stored, so a later failure — Telegram down, runner killed — can't
        discard them and re-bill next run."""
        self.commit_and_sync()
        try:
            self.conn.execute("BEGIN")
        except Exception:
            pass

    def rollback(self) -> None:
        self.conn.rollback()

    # -- row helpers (no row_factory: keeps sqlite3/libsql compatibility) ----

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # -- listings ------------------------------------------------------------

    def get_listing_row(self, listing_id: str) -> Optional[dict[str, Any]]:
        return self._query_one("SELECT * FROM listings WHERE id = ?", (listing_id,))

    def active_listings(self) -> list[Listing]:
        return [
            self._row_to_listing(r)
            for r in self._query("SELECT * FROM listings WHERE is_active = 1")
        ]

    @staticmethod
    def _row_to_listing(row: dict[str, Any]) -> Listing:
        return Listing(
            id=row["id"],
            source=row["source"],
            source_id=row["source_id"],
            address=row["address"],
            street=row["street"],
            city=row["city"],
            state=row["state"],
            zip_code=row["zip_code"],
            lat=row["lat"],
            lng=row["lng"],
            geohash=row["geohash"],
            property_type=row["property_type"],
            price=row["price"],
            beds=row["beds"],
            baths=row["baths"],
            sqft=row["sqft"],
            lot_sqft=row["lot_sqft"],
            year_built=row["year_built"],
            hoa_fee=row["hoa_fee"],
            status=row["status"],
            listed_date=row["listed_date"],
            days_on_market=row["days_on_market"],
            mls_name=row["mls_name"],
            mls_number=row["mls_number"],
            apn=row["apn"],
            raw=json.loads(row["raw_json"] or "{}"),
        )

    def _insert_listing(self, listing: Listing, now: str) -> None:
        self.conn.execute(
            """INSERT INTO listings (
                id, source, source_id, apn, address, street, city, state,
                zip_code, lat, lng, geohash, property_type, price, beds, baths,
                sqft, lot_sqft, year_built, hoa_fee, status, listed_date,
                days_on_market, mls_name, mls_number, raw_json,
                first_seen, last_seen, missing_runs, is_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1)""",
            (
                listing.id, listing.source, listing.source_id, listing.apn,
                listing.address, listing.street, listing.city, listing.state,
                listing.zip_code, listing.lat, listing.lng, listing.geohash,
                listing.property_type, listing.price, listing.beds,
                listing.baths, listing.sqft, listing.lot_sqft,
                listing.year_built, listing.hoa_fee, listing.status,
                listing.listed_date, listing.days_on_market, listing.mls_name,
                listing.mls_number, json.dumps(listing.raw), now, now,
            ),
        )

    def _update_listing(self, listing: Listing, now: str) -> None:
        values: dict[str, Any] = {
            "price": listing.price,
            "beds": listing.beds,
            "baths": listing.baths,
            "sqft": listing.sqft,
            "lot_sqft": listing.lot_sqft,
            "year_built": listing.year_built,
            "hoa_fee": listing.hoa_fee,
            "status": listing.status,
            "days_on_market": listing.days_on_market,
            "raw_json": json.dumps(listing.raw),
        }
        # COALESCE: a transiently-missing field in one feed pull must not
        # clobber a known stored value (a None price would otherwise break
        # the PRICE_CHANGED comparison chain forever).
        assignments = ", ".join(f"{c} = COALESCE(?, {c})" for c in _MUTABLE_COLUMNS)
        self.conn.execute(
            f"""UPDATE listings SET {assignments},
                last_seen = ?, missing_runs = 0, is_active = 1
                WHERE id = ?""",
            (*(values[c] for c in _MUTABLE_COLUMNS), now, listing.id),
        )

    def _append_history(self, listing: Listing, now: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO price_history
               (listing_id, observed_at, price, status) VALUES (?,?,?,?)""",
            (listing.id, now, listing.price, listing.status),
        )

    # -- reconcile -----------------------------------------------------------

    def reconcile(self, listings: list[Listing], now: str) -> list[Change]:
        """Diff fetched listings against state, upsert, classify. The caller
        commits (or rolls back, for dry runs)."""
        changes: list[Change] = []
        for listing in listings:
            row = self.get_listing_row(listing.id)
            if row is None:
                self._insert_listing(listing, now)
                self._append_history(listing, now)
                changes.append(Change(listing=listing, change=ChangeType.NEW))
                continue

            old_price, old_status = row["price"], row["status"]
            was_active = bool(row["is_active"])
            self._update_listing(listing, now)

            if not was_active:
                change = ChangeType.BACK_ON_MARKET
            elif (
                listing.price is not None
                and old_price is not None
                and listing.price != old_price
            ):
                change = ChangeType.PRICE_CHANGED
            elif listing.status != old_status:
                change = ChangeType.STATUS_CHANGED
            else:
                change = ChangeType.UNCHANGED

            # Record price appearance (NULL -> value) in history too, so the
            # observation chain stays intact even though it isn't announced.
            price_appeared = old_price is None and listing.price is not None
            if change is not ChangeType.UNCHANGED or price_appeared:
                self._append_history(listing, now)
            changes.append(
                Change(
                    listing=listing,
                    change=change,
                    old_price=old_price,
                    old_status=old_status,
                )
            )
        return changes

    def mark_missing(self, seen_ids: list[str], deactivate_after: int) -> int:
        """Bump missing_runs on active listings absent from this run's feed;
        deactivate those missing too long. Returns number deactivated."""
        rows = self._query("SELECT id FROM listings WHERE is_active = 1")
        missing = [r["id"] for r in rows if r["id"] not in set(seen_ids)]
        for listing_id in missing:
            self.conn.execute(
                "UPDATE listings SET missing_runs = missing_runs + 1 WHERE id = ?",
                (listing_id,),
            )
        cur = self.conn.execute(
            "UPDATE listings SET is_active = 0 WHERE is_active = 1 AND missing_runs >= ?",
            (deactivate_after,),
        )
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

    # -- commute -------------------------------------------------------------

    def commute_minutes(self, listing_id: str) -> tuple[Optional[float], bool]:
        """Returns (minutes, checked): minutes may be None even when checked
        (no drivable route found)."""
        row = self._query_one(
            "SELECT commute_minutes, commute_checked_at FROM listings WHERE id = ?",
            (listing_id,),
        )
        if row is None:
            return None, False
        return row["commute_minutes"], row["commute_checked_at"] is not None

    def set_commute(self, listing_id: str, minutes: Optional[float], now: str) -> None:
        self.conn.execute(
            "UPDATE listings SET commute_minutes = ?, commute_checked_at = ? WHERE id = ?",
            (minutes, now, listing_id),
        )

    # -- scores --------------------------------------------------------------

    def add_score(self, score: ListingScore, commute_minutes: Optional[float]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO scores
               (listing_id, scored_at, model, overall, commute_minutes,
                criteria_json, summary)
               VALUES (?,?,?,?,?,?,?)""",
            (
                score.listing_id,
                score.scored_at,
                score.model,
                score.overall,
                commute_minutes,
                json.dumps([c.model_dump() for c in score.criteria]),
                score.summary,
            ),
        )

    def latest_scores(self, listing_ids: list[str]) -> dict[str, ListingScore]:
        out: dict[str, ListingScore] = {}
        for listing_id in listing_ids:
            row = self._query_one(
                """SELECT * FROM scores WHERE listing_id = ?
                   ORDER BY scored_at DESC LIMIT 1""",
                (listing_id,),
            )
            if row:
                out[listing_id] = ListingScore(
                    listing_id=row["listing_id"],
                    overall=row["overall"],
                    criteria=json.loads(row["criteria_json"] or "[]"),
                    summary=row["summary"] or "",
                    model=row["model"] or "",
                    scored_at=row["scored_at"],
                )
        return out

    # -- runs ----------------------------------------------------------------

    def has_prior_run(self) -> bool:
        return self._query_one("SELECT 1 AS one FROM runs LIMIT 1") is not None

    def record_run(self, stats: RunStats) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, started_at, finished_at, n_fetched, n_after_geo,
                n_after_filters, n_new, n_changed, n_scored, n_notified,
                seed, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stats.run_id, stats.started_at, stats.finished_at,
                stats.n_fetched, stats.n_after_geo, stats.n_after_filters,
                stats.n_new, stats.n_changed, stats.n_scored, stats.n_notified,
                int(stats.seed), stats.notes,
            ),
        )

    # -- kv (isochrone cache etc.) --------------------------------------------

    def get_kv(self, key: str) -> Optional[tuple[str, str]]:
        row = self._query_one("SELECT value, updated_at FROM kv WHERE key = ?", (key,))
        return (row["value"], row["updated_at"]) if row else None

    def set_kv(self, key: str, value: str, now: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
            (key, value, now),
        )
