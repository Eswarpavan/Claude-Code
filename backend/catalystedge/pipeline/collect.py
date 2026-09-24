"""Fetch from every news adapter, isolating failures per source, and apply the
48-hour window before anything else sees the items."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from catalystedge.adapters.news.base import AdapterDisabled, NewsAdapter, RawNews
from catalystedge.core.http import BudgetExhausted, CircuitOpen, QuotaExceeded, SourceError
from catalystedge.pipeline.window import cutoff, filter_window


@dataclass
class SourceReport:
    source_key: str
    status: str               # ok | disabled | budget | quota | circuit | failed
    fetched: int = 0
    kept: int = 0
    too_old: int = 0
    in_future: int = 0
    http_calls: int = 0
    error: str | None = None


def collect_news(adapters: Sequence[NewsAdapter], now: dt.datetime, symbols: Sequence[str] = ()
                 ) -> tuple[list[RawNews], list[SourceReport]]:
    since = cutoff(now)
    items: list[RawNews] = []
    reports: list[SourceReport] = []
    for adapter in adapters:
        report = SourceReport(adapter.source_key, "ok")
        before = adapter.http.stats.get(adapter.source_key)
        calls_before = before.http_calls if before else 0
        try:
            fetched = adapter.fetch(since=since, now=now, symbols=symbols)
        except AdapterDisabled as e:
            report.status, report.error = "disabled", adapter.disabled_reason or str(e)
            fetched = []
        except BudgetExhausted as e:
            report.status, report.error = "budget", str(e)
            fetched = []
        except QuotaExceeded as e:
            report.status, report.error = "quota", str(e)
            fetched = []
        except CircuitOpen as e:
            report.status, report.error = "circuit", str(e)
            fetched = []
        except SourceError as e:
            report.status, report.error = "failed", adapter.http.redact(str(e))
            fetched = []
        except Exception as e:  # a parser bug in one adapter must not stop the others
            report.status, report.error = "failed", adapter.http.redact(f"{type(e).__name__}: {e}")
            fetched = []
        window = filter_window(fetched, now, published=lambda i: i.published_at)
        report.fetched, report.kept = len(fetched), len(window.kept)
        report.too_old, report.in_future = len(window.too_old), len(window.in_future)
        after = adapter.http.stats.get(adapter.source_key)
        report.http_calls = (after.http_calls if after else 0) - calls_before
        items += window.kept
        reports.append(report)
    return items, reports
