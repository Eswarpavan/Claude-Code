"""Persist news clusters. Only headline, publisher, timestamps, link and provider
ticker tags are written; there is nowhere to put an article body."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.clock import ensure_utc
from catalystedge.db.models import NewsItem, NewsItemTicker, Source, Ticker, TickerLinkLog
from catalystedge.pipeline.dedupe import Cluster, simhash64
from catalystedge.pipeline.ticker_link import LinkResult, Universe
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


def seed_tickers(session: Session, universe: Universe) -> None:
    rows = [dict(symbol=c.symbol, cik=c.cik, name=c.name, aliases=list(c.aliases)) for c in universe.by_symbol.values()]
    if rows:
        stmt = insert(Ticker).values(rows)
        session.execute(stmt.on_conflict_do_update(index_elements=[Ticker.symbol], set_={
            "cik": stmt.excluded.cik, "name": stmt.excluded.name, "aliases": stmt.excluded.aliases}))


def store_links(session: Session, news_item_id: int, headline: str, result: LinkResult) -> None:
    """Accepted links go to news_item_tickers; every ambiguity goes to ticker_link_log."""
    if result.mentions:
        session.execute(insert(NewsItemTicker).values([
            dict(news_item_id=news_item_id, symbol=m.symbol, method=m.method, link_confidence=m.confidence,
                 is_primary=m.is_primary, ambiguous=False)
            for m in result.mentions
        ]).on_conflict_do_nothing())
    for a in result.ambiguities:
        session.add(TickerLinkLog(news_item_id=news_item_id, headline=headline, reason=a.reason, chosen=a.chosen,
                                  candidates=[{"symbol": s, "method": m, "confidence": c} for s, m, c in a.candidates]))
    session.flush()
