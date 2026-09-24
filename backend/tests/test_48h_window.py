"""Step 2: hard rule 4, the 48-hour news window."""

import datetime as dt
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from catalystedge.pipeline.timing import news_available_at
from catalystedge.pipeline.window import (
    MAX_FUTURE_SKEW,
    NEWS_WINDOW,
    StaleNewsError,
    assert_fresh,
    cutoff,
    filter_window,
    in_window,
    purge_expired_news,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
H = dt.timedelta(hours=1)
S = dt.timedelta(seconds=1)


def test_window_is_48_hours():
    assert dt.timedelta(hours=48) == NEWS_WINDOW
    assert cutoff(NOW) == NOW - 48 * H


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (dt.timedelta(0), True),
        (47 * H, True),
        (48 * H, True),            # boundary is inclusive
        (48 * H + S, False),       # one second past: dropped
        (72 * H, False),
        (-MAX_FUTURE_SKEW, True),  # tiny provider clock drift tolerated
        (-MAX_FUTURE_SKEW - S, False),
    ],
)
def test_in_window_boundaries(age, expected):
    assert in_window(NOW - age, NOW) is expected


def test_timezones_are_normalised_not_ignored():
    new_york = dt.timezone(dt.timedelta(hours=-4))
    # 47h59m ago, expressed in New York time: still in window.
    published = (NOW - 48 * H + dt.timedelta(minutes=1)).astimezone(new_york)
    assert in_window(published, NOW)
    # Same wall-clock digits but interpreted as UTC would be 4h newer; make sure we don't do that.
    assert not in_window((NOW - 52 * H).astimezone(new_york), NOW)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="naive"):
        in_window(dt.datetime(2026, 9, 24, 12, 0), NOW)


def test_scoring_refuses_stale_items():
    assert_fresh(NOW - 10 * H, NOW)
    with pytest.raises(StaleNewsError):
        assert_fresh(NOW - 49 * H, NOW)


def test_filter_window_splits_old_fresh_and_future():
    items = [("fresh", NOW - H), ("old", NOW - 49 * H), ("future", NOW + H), ("edge", NOW - 48 * H)]
    res = filter_window(items, NOW, published=lambda i: i[1])
    assert [i[0] for i in res.kept] == ["fresh", "edge"]
    assert [i[0] for i in res.too_old] == ["old"]
    assert [i[0] for i in res.in_future] == ["future"]


def test_available_at_never_before_fetch():
    # Provider says 10:00 but we only saw it at 12:00: we could not have acted before 12:00.
    assert news_available_at(NOW - 2 * H, NOW) == NOW
    # Fetched after publication in the normal way.
    assert news_available_at(NOW, NOW + H) == NOW + H


def test_48_is_defined_in_exactly_one_place():
    """Guard against a second, drifting copy of the window elsewhere in the code."""
    pkg = Path(__file__).resolve().parents[1] / "catalystedge"
    pattern = re.compile(r"hours\s*=\s*48|48\s*\*\s*3600|172800|timedelta\(days\s*=\s*2\)")
    offenders = [p.relative_to(pkg) for p in pkg.rglob("*.py")
                 if p.name != "window.py" and pattern.search(p.read_text())]
    assert offenders == []


# ----------------------------------------------------------------------------- retention job (DB)


@pytest.mark.db
def test_purge_deletes_expired_news_but_keeps_event_audit_copy(db):
    db.execute(text("INSERT INTO sources (key, kind) VALUES ('finnhub_news', 'news')"))
    db.execute(text("INSERT INTO tickers (symbol, name) VALUES ('ABC', 'ABC Corp')"))

    def news(pid, age):
        return db.execute(text(
            "INSERT INTO news_items (source_key, provider_item_id, headline, url, published_at, fetched_at, "
            "available_at, dedupe_hash) VALUES ('finnhub_news', :pid, :h, 'https://x/' || :pid, :p, :p, :p, 1) "
            "RETURNING id"), {"pid": pid, "h": f"headline {pid}", "p": NOW - age}).scalar_one()

    old_id, edge_id, fresh_id = news("old", 49 * H), news("edge", 48 * H), news("fresh", H)
    db.execute(text("INSERT INTO news_item_tickers (news_item_id, symbol, method, link_confidence) "
                    "VALUES (:i, 'ABC', 'cashtag', 0.95)"), {"i": old_id})
    db.execute(text("INSERT INTO events (symbol, event_type, origin, news_item_id, headline, url, available_at, "
                    "polarity, classifier_version) VALUES ('ABC', 'upgrade', 'news', :i, 'headline old', "
                    "'https://x/old', :p, 'positive', 'v1')"), {"i": old_id, "p": NOW - 49 * H})

    assert purge_expired_news(db, NOW) == 1
    remaining = set(db.execute(text("SELECT id FROM news_items")).scalars())
    assert remaining == {edge_id, fresh_id}
    assert db.execute(text("SELECT count(*) FROM news_item_tickers")).scalar_one() == 0   # cascaded
    ev = db.execute(text("SELECT news_item_id, headline, url FROM events")).one()
    assert ev == (None, "headline old", "https://x/old")                                   # audit copy kept
