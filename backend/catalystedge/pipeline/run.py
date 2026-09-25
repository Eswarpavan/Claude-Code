"""The Stage-1 news pipeline end to end:
collect (48 h window) -> dedupe -> ticker link -> sentiment -> non-event filter -> classify [-> store]."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.adapters.news.base import NewsAdapter, RawNews
from catalystedge.db.models import Event
from catalystedge.ml.sentiment import SentimentModel, SentimentScore
from catalystedge.pipeline.classify import EventCandidate, classify
from catalystedge.pipeline.collect import SourceReport, collect_news
from catalystedge.pipeline.dedupe import Cluster, cluster_items
from catalystedge.pipeline.noise import NoiseVerdict, non_event_reason
from catalystedge.pipeline.score import score_headlines
from catalystedge.pipeline.store import store_clusters, store_links
from catalystedge.pipeline.ticker_link import LinkResult, Universe, link_tickers
from catalystedge.pipeline.timing import news_available_at
from catalystedge.sources import SOURCES

NOVELTY_LOOKBACK = dt.timedelta(days=30)


@dataclass
class Processed:
    cluster: Cluster
    link: LinkResult
    sentiment: SentimentScore
    events: list[EventCandidate]
    noise: NoiseVerdict | None = None      # set when the non-event filter dropped it (then no events)

    @property
    def headline(self) -> str:
        return self.cluster.representative.headline


def process_news(adapters: Sequence[NewsAdapter], universe: Universe, model: SentimentModel, now: dt.datetime,
                 symbols: Sequence[str] = ()) -> tuple[list[Processed], list[SourceReport]]:
    items, reports = collect_news(adapters, now, symbols)
    return process_items(items, universe, model, now), reports


def process_items(items: Sequence[RawNews], universe: Universe, model: SentimentModel, now: dt.datetime
                  ) -> list[Processed]:
    """Dedupe -> link -> sentiment -> non-event filter -> classify, for already-collected items."""
    clusters = cluster_items(list(items))
    reps = [c.representative for c in clusters]
    scores = score_headlines(model, [(r.headline, r.published_at) for r in reps], now)
    out: list[Processed] = []
    for cluster, score in zip(clusters, scores, strict=True):
        link = link_tickers(cluster.representative.headline, cluster.provider_tickers, universe)
        headline = cluster.representative.headline
        noise = non_event_reason(headline)
        events = [] if noise else classify(headline, link.mentions, score)
        out.append(Processed(cluster, link, score, events, noise))
    return out


def credibility(cluster: Cluster) -> float:
    """Best source prior, plus a little for independent confirmation by other providers."""
    base = max(SOURCES[s].credibility for s in cluster.sources)
    return round(min(1.0, base + 0.05 * (len(cluster.sources) - 1)), 3)


SAME_CATALYST_WINDOW = dt.timedelta(hours=36)


def same_catalyst(session: Session, symbol: str, event_type: str, at: dt.datetime,
                  exclude_news_id: int | None = None) -> list[Event]:
    """Existing events for the same company and catalyst type within 36 hours (primary sources first)."""
    if event_type == "other":
        return []
    q = select(Event).where(Event.symbol == symbol, Event.event_type == event_type,
                            Event.available_at > at - SAME_CATALYST_WINDOW,
                            Event.available_at < at + SAME_CATALYST_WINDOW)
    if exclude_news_id is not None:
        q = q.where(Event.news_item_id.is_(None) | (Event.news_item_id != exclude_news_id))
    rows = session.scalars(q.order_by(Event.available_at)).all()
    return sorted(rows, key=lambda r: not is_primary_event(r))


def is_primary_event(e: Event) -> bool:
    return e.origin != "news" or e.verification == "primary"


def persist(session: Session, processed: Sequence[Processed], fetched_at: dt.datetime) -> int:
    """Store news, links, ambiguity log and events. Returns the number of new events."""
    rep_ids = store_clusters(session, [p.cluster for p in processed], fetched_at)
    new_events = 0
    for p in processed:
        rep = p.cluster.representative
        news_id = rep_ids[id(p.cluster)]
        store_links(session, news_id, rep.headline, p.link)
        available_at = news_available_at(rep.published_at, fetched_at)
        primary = p.cluster.is_primary
        for e in p.events:
            # One underlying announcement, one event. Only when a primary source is involved: a newswire or
            # filing confirms earlier aggregator reports (they become 'verified'), and an aggregator report of an
            # announcement already on record adds nothing. Aggregator-vs-aggregator stays as before.
            same = same_catalyst(session, e.symbol, e.event_type, available_at, exclude_news_id=news_id)
            if primary and same:
                for old in same:
                    if not is_primary_event(old):
                        old.verification, old.original_url = "verified", rep.url
                continue
            if not primary and same and is_primary_event(same[0]):
                continue
            recent = session.scalar(select(Event.id).where(
                Event.symbol == e.symbol, Event.event_type == e.event_type, Event.news_item_id != news_id,
                Event.available_at >= available_at - NOVELTY_LOOKBACK, Event.available_at < available_at).limit(1))
            novelty = 0.3 if (recent is not None and e.event_type != "other") else 1.0
            row = dict(symbol=e.symbol, event_type=e.event_type, origin="news", news_item_id=news_id,
                       headline=rep.headline, url=rep.url, source_key=rep.source_key, available_at=available_at,
                       polarity=e.polarity, strength=e.strength, sentiment=e.sentiment,
                       mixed_resolution=e.mixed_resolution, materiality=e.materiality, novelty=novelty,
                       credibility=credibility(p.cluster), reasons=list(e.reasons),
                       classifier_version=e.classifier_version,
                       verification="primary" if primary else "unverified",
                       original_url=rep.url if primary else None)
            inserted = session.execute(insert(Event).values(row).on_conflict_do_nothing(
                constraint="uq_events_news").returning(Event.id)).scalar_one_or_none()
            new_events += inserted is not None
    session.flush()
    return new_events
