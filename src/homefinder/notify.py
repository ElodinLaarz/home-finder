"""Telegram delivery: one consolidated digest (chunked under the 4096-char
limit) plus instant pings for hot matches.

Formatting facts (verified against core.telegram.org, 2026-07):
- HTML parse mode: escape only < > & in text, and & as &amp; inside hrefs.
- sendMessage text max 4096 chars after entities parsing; no auto-split.
- link previews disabled via link_preview_options={"is_disabled": true}.
- sendPhoto captions cap at 1024 chars; photos upload via multipart (never
  by URL here — Street View URLs embed the API key).
- Rate limit ~1 msg/sec per chat; on 429 sleep parameters.retry_after.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote_plus

import httpx

from .models import Change, ChangeType, Listing, ListingScore

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096
CHUNK_BUDGET = 3900  # headroom under the hard limit
CAPTION_LIMIT = 1024
SEND_INTERVAL_SECONDS = 1.05


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TAG_RE = re.compile(r"<(/?)(b|i|a|code)(?:\s[^>]*)?>")


def truncate_html(text: str, limit: int) -> str:
    """Truncate at a Telegram-safe boundary: no partial tags or entities, and
    every opened tag closed (a cut inside a tag makes the API reject the whole
    message with a 400). Handles only the tags this module emits."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 32]  # headroom for the ellipsis + closing tags
    open_bracket = cut.rfind("<")
    if open_bracket > cut.rfind(">"):
        cut = cut[:open_bracket]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:] and len(cut) - amp < 10:
        cut = cut[:amp]
    open_stack: list[str] = []
    for match in _TAG_RE.finditer(cut):
        if match.group(1):
            if open_stack and open_stack[-1] == match.group(2):
                open_stack.pop()
        else:
            open_stack.append(match.group(2))
    return cut + "…" + "".join(f"</{t}>" for t in reversed(open_stack))


def maps_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def zillow_link(address: str) -> str:
    return f"https://www.zillow.com/homes/{quote_plus(address)}_rb/"


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._last_send = 0.0

    def _call(self, method: str, *, data: dict, files: dict | None = None) -> None:
        wait = SEND_INTERVAL_SECONDS - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        url = API_URL.format(token=self._token, method=method)
        for attempt in (1, 2):
            response = self._client.post(url, data=data, files=files)
            self._last_send = time.monotonic()
            if response.status_code == 429 and attempt == 1:
                retry_after = (
                    response.json().get("parameters", {}).get("retry_after", 3)
                )
                log.warning("telegram: 429, retrying after %ss", retry_after)
                time.sleep(retry_after + 1)
                continue
            if response.status_code != 200:
                raise RuntimeError(
                    f"Telegram {method} failed: HTTP {response.status_code} "
                    f"{response.text[:300]}"
                )
            return

    def send_message(self, html: str) -> None:
        self._call(
            "sendMessage",
            data={
                "chat_id": self._chat_id,
                "text": html,
                "parse_mode": "HTML",
                "link_preview_options": json.dumps({"is_disabled": True}),
            },
        )

    def send_photo(self, jpeg: bytes, caption_html: str) -> None:
        self._call(
            "sendPhoto",
            data={
                "chat_id": self._chat_id,
                "caption": truncate_html(caption_html, CAPTION_LIMIT),
                "parse_mode": "HTML",
            },
            files={"photo": ("home.jpg", jpeg, "image/jpeg")},
        )


# -- digest formatting ---------------------------------------------------------


def _fmt_price(price: int | None) -> str:
    return f"${price:,}" if price is not None else "price n/a"


def _fmt_commute(minutes: float | None, checked: bool) -> str:
    if minutes is not None:
        return f"{minutes:g} min drive"
    return "⚠ commute unknown" if checked else "commute unchecked"


def listing_links(listing: Listing) -> str:
    return (
        f'<a href="{esc(zillow_link(listing.address))}">Zillow</a> · '
        f'<a href="{esc(maps_link(listing.lat, listing.lng))}">Map</a>'
    )


def format_new_listing(
    listing: Listing,
    score: ListingScore | None,
    commute: tuple[float | None, bool],
) -> str:
    parts = [
        f"{listing.beds:g}bd" if listing.beds is not None else None,
        f"{listing.baths:g}ba" if listing.baths is not None else None,
        f"{listing.sqft:,} sqft" if listing.sqft is not None else None,
        f"built {listing.year_built}" if listing.year_built else None,
        _fmt_commute(*commute),
    ]
    detail = " · ".join(p for p in parts if p)
    head = f"<b>{score.overall:g}</b> — " if score else "<b>unscored</b> — "
    lines = [
        f"{head}<b>{esc(listing.address)}</b>",
        f"{_fmt_price(listing.price)} · {detail}",
        listing_links(listing),
    ]
    if score and score.summary:
        lines.append(f"<i>{esc(score.summary)}</i>")
    return "\n".join(lines)


def format_change(change: Change, commute: tuple[float | None, bool]) -> str:
    listing = change.listing
    if change.change is ChangeType.PRICE_CHANGED and change.old_price is not None:
        arrow = "📉" if (listing.price or 0) < change.old_price else "📈"
        what = f"{arrow} {_fmt_price(change.old_price)} → {_fmt_price(listing.price)}"
    elif change.change is ChangeType.BACK_ON_MARKET:
        what = f"🔁 back on market at {_fmt_price(listing.price)}"
    else:
        what = f"status {change.old_status} → {listing.status}"
    return (
        f"<b>{esc(listing.address)}</b>\n"
        f"{what} · {_fmt_commute(*commute)}\n"
        f"{listing_links(listing)}"
    )


def build_digest(
    title: str,
    new_blocks: list[str],
    changed_blocks: list[str],
    footer: str,
    max_listings: int,
) -> list[str]:
    """Assemble digest messages, chunked to stay under the Telegram limit."""
    blocks: list[str] = [f"<b>🏠 {esc(title)}</b>"]
    shown_new = new_blocks[:max_listings]
    if shown_new:
        blocks.append("<b>— NEW (best first) —</b>")
        blocks.extend(shown_new)
        if len(new_blocks) > max_listings:
            blocks.append(f"…and {len(new_blocks) - max_listings} more new listings.")
    shown_changed = changed_blocks[:max_listings]
    if shown_changed:
        blocks.append("<b>— CHANGED —</b>")
        blocks.extend(shown_changed)
        if len(changed_blocks) > max_listings:
            blocks.append(
                f"…and {len(changed_blocks) - max_listings} more changed listings."
            )
    if footer:
        blocks.append(esc(footer))

    messages: list[str] = []
    current = ""
    for block in blocks:
        block = truncate_html(block, CHUNK_BUDGET)
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > CHUNK_BUDGET and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
