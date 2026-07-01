# homefinder

Personal home-finder: pulls for-sale **house** listings for a target area from
RentCast, keeps only homes reachable within a set drive time of a fixed
location, ranks the survivors against a personal rubric with a Claude vision
model, and pushes new/changed matches to Telegram.

Single-user, single-writer, batch. One scheduled GitHub Actions run per day; a
concurrency guard guarantees runs never overlap, which is why the state store
needs no locking.

## Four rules baked in

1. **Structured filters are the only hard gates** — property type, price,
   beds/baths, size, and *measured* commute time are the only things that can
   exclude a listing.
2. **The model ranks; it never rejects.** A low (or failed) score sinks a
   listing to the bottom of the digest — it is never dropped.
3. **Human-in-the-loop by construction.** The only action the system takes is
   messaging *you*.
4. **Clean sourcing.** Licensed aggregator API; no scraping.

## Pipeline

```
RentCast fetch ─▶ geo pre-filter ─▶ hard filters ─▶ reconcile vs state
   (paginated)     (isochrone or      (type/price/     (new / price_changed /
                    crow-flies disk)   beds/sqft/…)      status / back-on-market)
                                                            │
        Telegram ◀─ score (Claude, ◀─ Google Routes matrix ◀┘
        digest +     rank-only)        traffic-aware @ your departure time
        hot pings                      (new listings only — commutes are stored)
```

## Setup

### 1. Accounts & keys (copy `.env.example` to `.env`)

| Service | What for | Free tier | Notes |
|---|---|---|---|
| [RentCast](https://app.rentcast.io/app/api) | listings | 50 calls/month | 1-2 calls/run → once daily fits free; **twice daily does not** |
| [Google Maps Platform](https://console.cloud.google.com) | commute gate + Street View | 5k traffic-aware matrix elements + 10k Street View images/month | enable **Routes API** and **Street View Static API** |
| [Anthropic](https://console.anthropic.com) | scoring | — | ~$0.01/listing on `claude-haiku-4-5`; a few $/month |
| [Mapbox](https://account.mapbox.com) *(optional)* | isochrone pre-filter | 100k/month | without it a generous crow-flies disk is used (see note below) |
| [Turso](https://turso.tech) | hosted state | 5 GB, 10M writes/month | optional for local runs (plain SQLite file); **required for scheduled GitHub Actions runs** — the workflow refuses to start without it, since an ephemeral runner has no disk to keep state on |
| Telegram | delivery | free | see below |

**Telegram:** message `@BotFather` → `/newbot` → copy the token. Open your new
bot, press **Start**, then read your numeric chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates` (`result[0].message.chat.id`).

**Turso:** `turso db create homefinder`, then
`TURSO_DATABASE_URL=libsql://<db>-<org>.turso.io` and
`TURSO_AUTH_TOKEN=$(turso db tokens create homefinder)`. The local file in
`state.path` becomes an embedded replica synced against the remote. Install the
client with `uv sync --extra turso` (Windows: needs CPython 3.9–3.13 for
prebuilt wheels).

### 2. Configure the search

Edit `config.yaml` — every value marked `CHANGEME`: search center, commute
destination + threshold + departure window, price/beds/baths/size gates, and
the scoring rubric (plain-language criteria with weights).

### 3. Seed, then verify

```sh
uv sync
uv run python -m homefinder --seed       # populate state; no scoring, no messages
uv run python -m homefinder --dry-run    # print what would be sent; roll back writes
uv run python -m homefinder              # real run
```

Run `--seed` **first**: it stores the current market (including measured
commutes) as the baseline so your first real digest contains only genuinely
new listings instead of the whole market — and so the scoring budget isn't
blown on day one. A normal run against empty state refuses to start and tells
you to seed. Dry runs skip Stage-2 routing (those elements are billed per
listing and a rollback would throw the results away), so they show
"commute unchecked" for listings that haven't been routed by a real run yet.

### 4. Schedule it

Push to GitHub, add every key from `.env.example` as an **Actions secret**,
and the included workflow (`.github/workflows/homefinder.yml`) runs daily at
11:00 UTC. `workflow_dispatch` lets you trigger manual/seed/dry runs; the
`concurrency` group keeps everything single-writer.

## Costs (order of magnitude)

RentCast free (1×/day) · Google well within free SKU caps after the seed run ·
scoring ≈ $0.05–0.25/day on Haiku · Mapbox/Telegram/Actions free.
**Total: ~$2–8/month**, dominated by scoring — which you can raise to
`claude-sonnet-5`/`claude-opus-4-8` in `config.yaml` if Haiku's judgment feels
shallow (roughly 3–10× scoring cost, still dollars).

## Known limitations (by data source, not by design)

- **RentCast provides no listing photos and no description text.** The vision
  model therefore sees structured facts, price history, commute, and (when
  enabled) one Google **Street View** image for street context — not
  interiors. Interior judgment stays with you, via the Zillow link on every
  digest entry. If this proves too thin, the richer-but-ToS-gray option is a
  portal scrape feeding the same `Listing` model.
- **RentCast status is only Active/Inactive** — no Pending/Sold. A listing
  missing from the feed for `run.mark_inactive_after_runs` consecutive runs is
  marked inactive; if it reappears you get a "back on market" change.
- **Mapbox's ToS expects isochrone results to be displayed on a Mapbox map.**
  For a personal headless gate that's a gray area — leave `MAPBOX_TOKEN`
  unset and the pre-filter degrades to a deliberately oversized crow-flies
  disk (slightly more Google matrix elements, no accuracy loss: Stage 2 is the
  real gate either way).
- **Isochrones cap at 60 contour minutes** (Mapbox limit). Budgets over
  ~50 min lose pre-filter padding; the crow-flies fallback has no cap.

## State

SQLite schema: `listings` (current state + commute), `price_history`
(append-only), `scores` (append-only, criteria + rationale JSON — this is the
audit log), `runs` (funnel counts per run), `kv` (isochrone cache).
Free-tier Turso keeps 1 day of point-in-time restore; for belt-and-braces,
`GET https://<db>-<org>.turso.io/dump` is a full SQL export worth cron-ing
somewhere occasionally.

## Development

```sh
uv run pytest            # full suite, no network
uv run python -m homefinder --dry-run --limit 20 -v
```

`--limit` truncates the feed, so runs using it skip the missing-listing
bookkeeping (otherwise everything outside the slice would look delisted).

Module map: `rentcast.py` fetch/normalize · `geo.py` stage-1 gate ·
`filters.py` hard gates · `store.py` state + reconcile/classify ·
`dedupe.py` identity (geohash, cross-source matcher for future sources) ·
`routing.py` stage-2 Google matrix · `streetview.py` imagery ·
`scoring.py` Claude rank-only scoring · `notify.py` Telegram ·
`pipeline.py` orchestration · `__main__.py` CLI.
