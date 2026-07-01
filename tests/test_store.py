from conftest import make_listing

from homefinder.models import ChangeType, ListingScore, CriterionScore, RunStats


def test_reconcile_new_then_unchanged(store):
    listing = make_listing()
    changes = store.reconcile([listing], "2026-07-01T10:00:00+00:00")
    assert [c.change for c in changes] == [ChangeType.NEW]

    changes = store.reconcile([listing], "2026-07-02T10:00:00+00:00")
    assert [c.change for c in changes] == [ChangeType.UNCHANGED]


def test_reconcile_price_change(store):
    store.reconcile([make_listing()], "2026-07-01T10:00:00+00:00")
    changes = store.reconcile([make_listing(price=290000)], "2026-07-02T10:00:00+00:00")
    assert changes[0].change is ChangeType.PRICE_CHANGED
    assert changes[0].old_price == 300000

    history = store._query(
        "SELECT price FROM price_history WHERE listing_id = ? ORDER BY observed_at",
        (make_listing().id,),
    )
    assert [r["price"] for r in history] == [300000, 290000]


def test_reconcile_status_change(store):
    store.reconcile([make_listing()], "2026-07-01T10:00:00+00:00")
    changes = store.reconcile(
        [make_listing(status="Inactive")], "2026-07-02T10:00:00+00:00"
    )
    assert changes[0].change is ChangeType.STATUS_CHANGED
    assert changes[0].old_status == "Active"


def test_back_on_market(store):
    listing = make_listing()
    store.reconcile([listing], "2026-07-01T10:00:00+00:00")
    # missing for two runs with threshold 2 -> deactivated
    store.mark_missing([], deactivate_after=2)
    store.mark_missing([], deactivate_after=2)
    row = store.get_listing_row(listing.id)
    assert row["is_active"] == 0

    changes = store.reconcile([listing], "2026-07-05T10:00:00+00:00")
    assert changes[0].change is ChangeType.BACK_ON_MARKET
    assert store.get_listing_row(listing.id)["is_active"] == 1
    assert store.get_listing_row(listing.id)["missing_runs"] == 0


def test_mark_missing_resets_on_reappearance(store):
    listing = make_listing()
    store.reconcile([listing], "2026-07-01T10:00:00+00:00")
    store.mark_missing([], deactivate_after=3)
    assert store.get_listing_row(listing.id)["missing_runs"] == 1
    store.reconcile([listing], "2026-07-02T10:00:00+00:00")
    assert store.get_listing_row(listing.id)["missing_runs"] == 0


def test_commute_roundtrip(store):
    listing = make_listing()
    store.reconcile([listing], "2026-07-01T10:00:00+00:00")
    assert store.commute_minutes(listing.id) == (None, False)

    store.set_commute(listing.id, 27.5, "2026-07-01T10:05:00+00:00")
    assert store.commute_minutes(listing.id) == (27.5, True)

    # unroutable: minutes None but checked True
    store.set_commute(listing.id, None, "2026-07-01T10:06:00+00:00")
    assert store.commute_minutes(listing.id) == (None, True)


def test_scores_roundtrip(store):
    listing = make_listing()
    store.reconcile([listing], "2026-07-01T10:00:00+00:00")
    score = ListingScore(
        listing_id=listing.id,
        overall=7.5,
        criteria=[CriterionScore(key="quiet_street", score=8, rationale="cul-de-sac")],
        summary="solid",
        model="claude-haiku-4-5",
        scored_at="2026-07-01T10:10:00+00:00",
    )
    store.add_score(score, 27.5)
    loaded = store.latest_scores([listing.id])[listing.id]
    assert loaded.overall == 7.5
    assert loaded.criteria[0].key == "quiet_street"


def test_record_run_and_kv(store):
    store.record_run(RunStats(run_id="abc", started_at="2026-07-01T10:00:00+00:00"))
    assert store._query_one("SELECT run_id FROM runs")["run_id"] == "abc"

    store.set_kv("k", "v1", "2026-07-01T10:00:00+00:00")
    store.set_kv("k", "v2", "2026-07-02T10:00:00+00:00")
    assert store.get_kv("k") == ("v2", "2026-07-02T10:00:00+00:00")
    assert store.get_kv("missing") is None


def test_transient_none_price_does_not_clobber(store):
    listing_id = make_listing().id
    store.reconcile([make_listing()], "2026-07-01T10:00:00+00:00")
    # feed hiccup: price missing for one run
    changes = store.reconcile([make_listing(price=None)], "2026-07-02T10:00:00+00:00")
    assert changes[0].change is ChangeType.UNCHANGED
    assert store.get_listing_row(listing_id)["price"] == 300000  # kept

    # when the price comes back different, the change is still detected
    changes = store.reconcile([make_listing(price=250000)], "2026-07-03T10:00:00+00:00")
    assert changes[0].change is ChangeType.PRICE_CHANGED
    assert changes[0].old_price == 300000


def test_price_appearance_recorded_in_history_but_not_announced(store):
    listing_id = make_listing().id
    store.reconcile([make_listing(price=None)], "2026-07-01T10:00:00+00:00")
    changes = store.reconcile([make_listing(price=300000)], "2026-07-02T10:00:00+00:00")
    assert changes[0].change is ChangeType.UNCHANGED  # appearance, not a change
    history = store._query(
        "SELECT price FROM price_history WHERE listing_id = ? ORDER BY observed_at",
        (listing_id,),
    )
    assert [r["price"] for r in history] == [None, 300000]


def test_has_prior_run(store):
    assert not store.has_prior_run()
    store.record_run(RunStats(run_id="r1", started_at="2026-07-01T10:00:00+00:00"))
    assert store.has_prior_run()


def test_rollback_discards(store):
    store.reconcile([make_listing()], "2026-07-01T10:00:00+00:00")
    store.rollback()
    assert store.get_listing_row(make_listing().id) is None
