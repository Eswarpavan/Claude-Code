"""Persist news clusters. Only headline, publisher, timestamps, link and provider
ticker tags are written; there is nowhere to put an article body."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.clock import ensure_utc
from catalystedge.db.models import NewsItem, Source
from catalystedge.pipeline.dedupe import Cluster, simhash64
from catalystedge.pipeline.timing import news_available_at
from catalystedge.sources import SOURCES


def seed_sources(session: Session) -> None:
    rows = [
        dict(key=s.key, kind=s.kind, enabled=s.enabled_by_default, fragile=s.fragile, official=s.official,
             credibility=s.credibility, daily_budget=s.daily_budget, ttl_seconds=s.cache_ttl_s,
             status="stale" if s.enabled_by_default else "disabled", last_error=s.note or None)
        for s in SOURCES.values()
    ]
    stmt = insert(Source).values(rows)
    session.execute(stmt.on_conflict_do_update(index_elements=[Source.key], set_={
        "kind": stmt.excluded.kind, "fragile": stmt.excluded.fragile, "official": stmt.excluded.official,
        "credibility": stmt.excluded.credibility, "daily_budget": stmt.excluded.daily_budget,
        "ttl_seconds": stmt.excluded.ttl_seconds,
    }))


def store_clusters(session: Session, clusters: list[Cluster], fetched_at: dt.datetime) -> dict[int, int]:
    """Insert every member of every cluster (ON CONFLICT DO NOTHING) and point
    them at the representative's row. Returns {id(cluster): representative row id}."""
    fetched_at = ensure_utc(fetched_at)
    out: dict[int, int] = {}
    for cluster in clusters:
        ids: dict[tuple[str, str], int] = {}
        for m in cluster.members:
            values = dict(
                source_key=m.source_key, provider_item_id=m.provider_item_id, headline=m.headline, url=m.url,
                publisher=m.publisher, published_at=m.published_at, fetched_at=fetched_at,
                available_at=news_available_at(m.published_at, fetched_at),
                dedupe_hash=simhash64(m.headline), provider_tickers=sorted(m.provider_tickers),
                provider_sentiment=m.provider_sentiment,
            )
            row_id = session.execute(
                insert(NewsItem).values(values).on_conflict_do_nothing(constraint="uq_news_source_item")
                .returning(NewsItem.id)
            ).scalar_one_or_none()
            if row_id is None:  # already stored by an earlier poll
                row_id = session.execute(select(NewsItem.id).where(
                    NewsItem.source_key == m.source_key, NewsItem.provider_item_id == m.provider_item_id)
                ).scalar_one()
            ids[(m.source_key, m.provider_item_id)] = row_id
        rep = cluster.representative
        rep_id = ids[(rep.source_key, rep.provider_item_id)]
        for row_id in ids.values():
            session.get(NewsItem, row_id).cluster_id = rep_id
        out[id(cluster)] = rep_id
    session.flush()
    return out
