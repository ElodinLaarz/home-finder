from conftest import make_listing

from homefinder.models import Change, ChangeType, ListingScore
from homefinder.notify import (
    build_digest,
    esc,
    format_change,
    format_new_listing,
    maps_link,
    zillow_link,
    MAX_MESSAGE,
)


def test_esc():
    assert esc("A & B <Realty>") == "A &amp; B &lt;Realty&gt;"


def test_links():
    assert zillow_link("123 Main St, Springfield, OH 45501").startswith(
        "https://www.zillow.com/homes/123+Main+St"
    )
    assert maps_link(39.9, -83.8) == (
        "https://www.google.com/maps/search/?api=1&query=39.9,-83.8"
    )


def score(overall=8.4) -> ListingScore:
    return ListingScore(
        listing_id="rentcast:x", overall=overall, summary="quiet street, fair price"
    )


def test_format_new_listing_escapes_and_links():
    listing = make_listing(address="123 Main St & Oak, Springfield, OH")
    block = format_new_listing(listing, score(), (27.5, True))
    assert "123 Main St &amp; Oak" in block
    assert "27.5 min drive" in block
    # & inside href must be &amp; for Telegram HTML mode
    assert "&amp;query=39.92,-83.81" in block
    assert "<i>quiet street, fair price</i>" in block


def test_format_new_listing_unknown_commute_flagged():
    block = format_new_listing(make_listing(), None, (None, True))
    assert "⚠ commute unknown" in block
    assert "unscored" in block


def test_format_price_drop():
    change = Change(
        listing=make_listing(price=290000),
        change=ChangeType.PRICE_CHANGED,
        old_price=300000,
        old_status="Active",
    )
    block = format_change(change, (20.0, True))
    assert "📉" in block and "$300,000 → $290,000" in block


def test_format_back_on_market():
    change = Change(
        listing=make_listing(),
        change=ChangeType.BACK_ON_MARKET,
        old_price=300000,
        old_status="Inactive",
    )
    assert "🔁 back on market" in format_change(change, (20.0, True))


def test_digest_chunks_under_limit():
    blocks = [f"<b>listing {i}</b>\n" + "x" * 400 for i in range(30)]
    messages = build_digest("title", blocks, [], "footer", max_listings=30)
    assert len(messages) > 1
    assert all(len(m) <= MAX_MESSAGE for m in messages)
    # nothing lost across chunk boundaries
    combined = "\n\n".join(messages)
    assert all(f"<b>listing {i}</b>" in combined for i in range(30))


def test_digest_caps_new_and_changed_listings():
    new = [f"n{i}" for i in range(40)]
    changed = [f"c{i}" for i in range(35)]
    combined = "\n\n".join(build_digest("t", new, changed, "", max_listings=30))
    assert "…and 10 more new listings." in combined
    assert "…and 5 more changed listings." in combined


def test_truncate_html_never_cuts_tags_or_entities():
    from homefinder.notify import truncate_html

    text = "<b>head</b> " + "word &amp; " * 200 + '<a href="https://x">tail</a>'
    out = truncate_html(text, 300)
    assert len(out) <= 300
    assert out.count("<b>") == out.count("</b>")
    assert out.count("<a") == out.count("</a>")
    # no dangling partial entity at the cut
    tail = out.rsplit("&", 1)[-1]
    assert ";" in tail or "&" not in out[-10:]


def test_truncate_html_closes_open_tags():
    from homefinder.notify import truncate_html

    text = "<b><i>" + "x" * 500
    out = truncate_html(text, 100)
    assert out.endswith("</i></b>")
    assert len(out) <= 100


def test_truncate_html_noop_when_short():
    from homefinder.notify import truncate_html

    assert truncate_html("<b>ok</b>", 100) == "<b>ok</b>"
