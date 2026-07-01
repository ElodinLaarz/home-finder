"""Pipeline orchestration: fetch → geo gate → hard filters → reconcile →
traffic-aware routing gate → preference scoring (rank-only) → notify → persist.

Modes:
- seed: populate state (including commutes) without scoring or notifying —
  run once first; a normal run against an empty state refuses to start.
- dry_run: fetch + gates + reconcile against real APIs, but no routing spend,
  no scoring, no Telegram; prints the digest and rolls back all state writes.

Cost-safety invariants:
- Required env vars are validated up front, before any paid call.
- State is checkpoint-committed right after routing results and after each
  score is stored, so a later failure never discards paid API results.
- Notifications are sent AFTER state is committed: a Telegram outage can cost
  you one digest (visible as a failed Actions run) but never causes duplicate
  pings or duplicate API spend.
"""

from __future__ import annotations

import logging

import anthropic
import httpx

from . import geo, notify, rentcast, routing, scoring, streetview
from .config import AppConfig, optional_env, require_env
from .filters import apply_hard_filters
from .models import Change, ChangeType, Listing, ListingScore, RunStats
from .store import Store, new_run_id
from .util import next_departure, utcnow_iso

log = logging.getLogger(__name__)

CHANGE_TYPES_FOR_DIGEST = (
    ChangeType.PRICE_CHANGED,
    ChangeType.STATUS_CHANGED,
    ChangeType.BACK_ON_MARKET,
)


def run(
    cfg: AppConfig,
    *,
    seed: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> RunStats:
    stats = RunStats(run_id=new_run_id(), started_at=utcnow_iso(), seed=seed)
    now = stats.started_at

    # Fail fast on missing credentials — BEFORE any paid API call.
    rentcast_key = require_env("RENTCAST_API_KEY")
    will_score = cfg.scoring.enabled and not seed and not dry_run
    will_notify = cfg.notify.enabled and not seed and not dry_run
    anthropic_key = require_env("ANTHROPIC_API_KEY") if will_score else None
    telegram_auth = (
        (require_env("TELEGRAM_BOT_TOKEN"), require_env("TELEGRAM_CHAT_ID"))
        if will_notify
        else None
    )
    google_key = optional_env("GOOGLE_MAPS_API_KEY")

    store = Store.open(cfg.state)
    http = httpx.Client(timeout=60)
    try:
        # Cold-start guard: a normal run against empty state would treat the
        # whole market as new — flooding the digest and the scoring budget.
        if not seed and not dry_run and not store.has_prior_run():
            raise RuntimeError(
                "State has no prior runs. Run `python -m homefinder --seed` "
                "first to baseline the market (or --dry-run to preview)."
            )

        # 1. Fetch + normalize -------------------------------------------------
        raw = rentcast.fetch_sale_listings(cfg.search, cfg.filters, rentcast_key, http)
        listings, skipped = rentcast.normalize_all(raw)
        feed_truncated = len(raw) >= rentcast.MAX_PAGES * rentcast.PAGE_LIMIT
        all_feed_ids = [l.id for l in listings]  # BEFORE any --limit slice
        if limit is not None:
            listings = listings[:limit]
        stats.n_fetched = len(listings)
        log.info("fetched %d listings (%d skipped)", len(listings), skipped)

        # 2. Geo pre-filter (generous gate) ------------------------------------
        gate = geo.load_gate(store, cfg.commute, optional_env("MAPBOX_TOKEN"), http, now)
        in_area = [l for l in listings if gate.contains(l.lat, l.lng)]
        stats.n_after_geo = len(in_area)
        log.info("geo gate (%s): %d/%d inside", gate.kind, len(in_area), len(listings))

        # 3. Hard structured filters -------------------------------------------
        kept, dropped = apply_hard_filters(in_area, cfg.filters)
        stats.n_after_filters = len(kept)
        for decision in dropped:
            log.debug("filtered out %s: %s", decision.listing.id, "; ".join(decision.reasons))
        log.info("hard filters: %d kept, %d dropped", len(kept), len(dropped))

        # 4. Reconcile vs state -------------------------------------------------
        changes = store.reconcile(kept, now)
        new_changes = [c for c in changes if c.change is ChangeType.NEW]
        changed = [c for c in changes if c.change in CHANGE_TYPES_FOR_DIGEST]
        stats.n_new = len(new_changes)
        stats.n_changed = len(changed)

        # "Seen" for missing-tracking means "present in the FEED", not "passed
        # the gates" — a still-listed home hovering over max_price must not be
        # deactivated as sold. Skip entirely when the feed view is partial.
        if limit is not None or feed_truncated or not all_feed_ids:
            log.warning("mark_missing skipped: partial or empty feed view")
            deactivated = 0
        else:
            deactivated = store.mark_missing(
                all_feed_ids, cfg.run.mark_inactive_after_runs
            )
        log.info(
            "reconcile: %d new, %d changed, %d unchanged, %d deactivated",
            len(new_changes), len(changed),
            len(changes) - len(new_changes) - len(changed), deactivated,
        )

        # 5. Traffic-aware routing for listings without a stored commute --------
        # Skipped in dry runs: elements are billed per listing, and a rollback
        # would throw the paid results away only to re-bill them next run.
        need_route = [
            c.listing for c in changes if not store.commute_minutes(c.listing.id)[1]
        ]
        if dry_run and need_route:
            log.info("routing skipped (dry run): %d listings stay unchecked", len(need_route))
        elif need_route and google_key:
            departure = next_departure(
                cfg.commute.departure.day_of_week,
                cfg.commute.departure.time,
                cfg.commute.departure.timezone,
            )
            commutes = routing.compute_commutes(
                [(l.id, l.lat, l.lng) for l in need_route],
                cfg.commute.destination,
                departure,
                google_key,
                http,
            )
            for listing_id, minutes in commutes.items():
                store.set_commute(listing_id, minutes, now)
            store.checkpoint()  # paid results survive any later failure
            log.info("routing: measured %d commutes", len(commutes))
        elif need_route:
            log.warning(
                "routing: GOOGLE_MAPS_API_KEY not set — %d listings keep unknown commute",
                len(need_route),
            )

        def commute_of(listing: Listing) -> tuple[float | None, bool]:
            return store.commute_minutes(listing.id)

        def commute_excluded(listing: Listing) -> bool:
            minutes, _checked = commute_of(listing)
            return minutes is not None and minutes > cfg.commute.max_minutes

        digest_new = [c.listing for c in new_changes if not commute_excluded(c.listing)]
        digest_changed = [c for c in changed if not commute_excluded(c.listing)]
        n_commute_excluded = (
            len(new_changes) + len(changed) - len(digest_new) - len(digest_changed)
        )

        # 6. Preference scoring (rank-only; new listings only) -------------------
        scores: dict[str, ListingScore] = {}
        images: dict[str, bytes] = {}
        if will_score and digest_new:
            llm = anthropic.Anthropic(api_key=anthropic_key)
            to_score = digest_new[: cfg.scoring.max_per_run]
            if len(digest_new) > len(to_score):
                log.warning(
                    "scoring: capping at %d of %d new listings (scoring.max_per_run)",
                    len(to_score), len(digest_new),
                )
            for listing in to_score:
                # Rule: the model never rejects. Any failure in this block
                # (Street View, API, parsing) leaves the listing unscored at
                # the bottom of the digest — never dropped.
                try:
                    image = None
                    if cfg.scoring.use_street_view and google_key:
                        image = streetview.fetch_street_view(
                            listing.lat, listing.lng, google_key, http
                        )
                        if image:
                            images[listing.id] = image
                    score = scoring.score_listing(
                        llm, cfg.scoring, listing, commute_of(listing)[0], image, now
                    )
                except Exception:
                    log.exception("scoring failed for %s — keeping unscored", listing.id)
                    continue
                scores[listing.id] = score
                store.add_score(score, commute_of(listing)[0])
                store.checkpoint()
            stats.n_scored = len(scores)
        elif digest_new and (seed or dry_run):
            log.info("scoring skipped (%s)", "seed" if seed else "dry run")

        # Best first; unscored listings sink to the bottom.
        digest_new.sort(
            key=lambda l: scores[l.id].overall if l.id in scores else -1.0,
            reverse=True,
        )

        # 7. Notify (state is committed first — see module docstring) -----------
        messages, pings = _build_messages(
            cfg, digest_new, digest_changed, scores, commute_of,
            n_commute_excluded, stats,
        )
        if seed:
            log.info(
                "seed run: skipping notifications (%d would have sent)",
                len(messages) + len(pings),
            )
        elif dry_run or not cfg.notify.enabled:
            for text in [p[1] for p in pings] + messages:
                print("\n----- telegram message (not sent) -----\n" + text)
        elif digest_new or digest_changed:
            store.checkpoint()
            telegram = notify.Telegram(telegram_auth[0], telegram_auth[1], http)
            sent = 0
            for listing_id, caption in pings:
                sent += _try_send(telegram, caption, images.get(listing_id))
            for text in messages:
                sent += _try_send(telegram, text, None)
            stats.n_notified = sent
            log.info("sent %d/%d telegram messages", sent, len(pings) + len(messages))
        else:
            log.info("nothing new or changed — no notification sent")

        # 8. Persist -------------------------------------------------------------
        stats.finished_at = utcnow_iso()
        store.record_run(stats)
        if dry_run:
            store.rollback()
            log.info("dry run: rolled back all state writes (run not recorded)")
        else:
            store.commit_and_sync()
        return stats
    finally:
        http.close()


def _try_send(telegram: notify.Telegram, html: str, image: bytes | None) -> int:
    """Send one message; a delivery failure is logged, never fatal — state is
    already committed and the Actions run status surfaces the log."""
    try:
        if image:
            telegram.send_photo(image, html)
        else:
            telegram.send_message(html)
        return 1
    except Exception:
        log.exception("telegram send failed — continuing")
        return 0


def _build_messages(
    cfg: AppConfig,
    digest_new: list[Listing],
    digest_changed: list[Change],
    scores: dict[str, ListingScore],
    commute_of,
    n_commute_excluded: int,
    stats: RunStats,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Returns (digest messages, [(listing_id, instant ping html), ...])."""
    if not digest_new and not digest_changed:
        return [], []

    pings: list[tuple[str, str]] = []
    for listing in digest_new:
        score = scores.get(listing.id)
        if score and score.overall >= cfg.scoring.instant_threshold:
            block = notify.format_new_listing(listing, score, commute_of(listing))
            pings.append((listing.id, f"🔥 <b>Hot match</b>\n\n{block}"))

    new_blocks = [
        notify.format_new_listing(l, scores.get(l.id), commute_of(l))
        for l in digest_new
    ]
    changed_blocks = [
        notify.format_change(c, commute_of(c.listing)) for c in digest_changed
    ]
    footer_bits = [f"{stats.n_fetched} fetched", f"{stats.n_after_filters} matched"]
    if n_commute_excluded:
        footer_bits.append(f"{n_commute_excluded} over commute limit")
    title = (
        f"Home digest — {stats.started_at[:10]}: "
        f"{len(digest_new)} new · {len(digest_changed)} changed"
    )
    messages = notify.build_digest(
        title, new_blocks, changed_blocks, " · ".join(footer_bits),
        cfg.notify.digest_max_listings,
    )
    return messages, pings
