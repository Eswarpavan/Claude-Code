"""Step 3c: storing news (DB)."""

import datetime as dt

import pytest
from sqlalchemy import func, select

from catalystedge.adapters.news.registry import build_news_adapters
from catalystedge.clock import FrozenClock
from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.core.kv import InMemoryKV
from catalystedge.db.models import NewsItem, Source
from catalystedge.fixtures import FixtureTransport, recorded_at
from catalystedge.pipeline.collect import collect_news
from catalystedge.pipeline.dedupe import cluster_items
from catalystedge.pipeline.store import seed_sources, store_clusters

pytestmark = pytest.mark.db
NOW = recorded_at()


def _clusters():
    clock = FrozenClock(NOW)
    http = HttpClient(transport=FixtureTransport(), kv=InMemoryKV(clock), clock=clock, sleep=lambda s: None)
    items, _ = collect_news(build_news_adapters(Settings(_env_file=None, CATALYSTEDGE_DATA_MODE="fixtures"), http),
                            NOW, symbols=["AAPL"])
    return items, cluster_items(items)


def test_seed_sources_registers_stubs_as_disabled(db):
    seed_sources(db)
    rows = {s.key: s for s in db.scalars(select(Source))}
    assert rows["benzinga_news"].enabled is False and rows["benzinga_news"].status == "disabled"
    assert rows["finnhub_news"].enabled is True


def test_store_is_idempotent_and_links_clusters(db):
    seed_sources(db)
    items, clusters = _clusters()
    fetched = NOW + dt.timedelta(minutes=1)
    store_clusters(db, clusters, fetched)
    store_clusters(db, clusters, fetched)   # second poll: no duplicates
    assert db.scalar(select(func.count()).select_from(NewsItem)) == len(items)
    nvda_rows = db.scalars(select(NewsItem).where(NewsItem.provider_tickers.any("NVDA"))).all()
    assert len(nvda_rows) == 2 and len({r.cluster_id for r in nvda_rows}) == 1
    assert all(r.available_at >= fetched for r in db.scalars(select(NewsItem)))
