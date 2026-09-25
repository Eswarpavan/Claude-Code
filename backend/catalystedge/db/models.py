"""SQLAlchemy models. Mirrors docs/ARCHITECTURE.md §6.

Point-in-time rule: every row that can feed a decision has `available_at`,
the moment CatalystEdge could first have known it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


EVENT_TYPES = (
    "earnings_beat", "guidance_raise", "fda_approval", "positive_trial", "m_and_a_target",
    "upgrade", "insider_buy_cluster", "contract_win", "other",
)
POLARITIES = ("positive", "neutral", "negative", "mixed")


# --------------------------------------------------------------------------- reference


class Ticker(Base):
    __tablename__ = "tickers"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str | None] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    adv20_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    delisted_on: Mapped[dt.date | None] = mapped_column(Date)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())


class Source(Base):
    __tablename__ = "sources"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    fragile: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    official: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    credibility: Mapped[float] = mapped_column(Float, default=0.5, server_default=text("0.5"))
    daily_budget: Mapped[int | None] = mapped_column(Integer)
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default=text("300"))
    status: Mapped[str] = mapped_column(String(10), default="disabled", server_default="disabled")
    last_success_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(Text)
    budget_used_today: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    circuit_open_until: Mapped[dt.datetime | None] = mapped_column(TZ)
    __table_args__ = (
        CheckConstraint(_in("kind", ("news", "event", "price", "macro", "email")), name="ck_sources_kind"),
        CheckConstraint(_in("status", ("ok", "stale", "failed", "disabled")), name="ck_sources_status"),
        CheckConstraint("credibility BETWEEN 0 AND 1", name="ck_sources_credibility"),
    )


class RefreshRun(Base):
    __tablename__ = "refresh_runs"
    id: Mapped[Any] = mapped_column(UUID(as_uuid=True), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[dt.datetime] = mapped_column(TZ)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(String(10), default="running", server_default="running")
    progress_pct: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    __table_args__ = (CheckConstraint(_in("trigger", ("schedule", "open", "manual")), name="ck_refresh_trigger"),)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    refresh_id: Mapped[Any | None] = mapped_column(ForeignKey("refresh_runs.id", ondelete="SET NULL"))
    source_key: Mapped[str] = mapped_column(ForeignKey("sources.key"))
    started_at: Mapped[dt.datetime] = mapped_column(TZ)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(String(10))
    items_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    items_new: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    http_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- raw inputs


class NewsItem(Base):
    """Headline + link only. There is deliberately no body/summary column."""

    __tablename__ = "news_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(ForeignKey("sources.key"))
    provider_item_id: Mapped[str] = mapped_column(Text)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime] = mapped_column(TZ, index=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(TZ)
    available_at: Mapped[dt.datetime] = mapped_column(TZ, index=True)
    dedupe_hash: Mapped[int] = mapped_column(BigInteger, index=True)
    cluster_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    provider_tickers: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    provider_sentiment: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("source_key", "provider_item_id", name="uq_news_source_item"),
        CheckConstraint("available_at >= published_at", name="ck_news_available_after_published"),
    )


class NewsItemTicker(Base):
    __tablename__ = "news_item_tickers"
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), primary_key=True)
    method: Mapped[str] = mapped_column(String(64))
    link_confidence: Mapped[float] = mapped_column(Float)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class TickerLinkLog(Base):
    __tablename__ = "ticker_link_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    news_item_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"))
    headline: Mapped[str] = mapped_column(Text)
    candidates: Mapped[list[Any]] = mapped_column(JSONB)
    chosen: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())


class Filing(Base):
    __tablename__ = "filings"
    accession: Mapped[str] = mapped_column(String(25), primary_key=True)
    cik: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str | None] = mapped_column(String(12), index=True)
    form_type: Mapped[str] = mapped_column(String(24))
    items: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    accepted_at: Mapped[dt.datetime] = mapped_column(TZ, index=True)  # == available_at
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)


class TradingHalt(Base):
    """Nasdaq Trader's official halts feed (all U.S. exchanges, including LULD pauses)."""
    __tablename__ = "trading_halts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    halted_at: Mapped[dt.datetime] = mapped_column(TZ)
    reason_code: Mapped[str | None] = mapped_column(String(12))
    market: Mapped[str | None] = mapped_column(String(24))
    resumed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    source_url: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("symbol", "halted_at", name="uq_halts_symbol_time"),)


class ShortInterest(Base):
    """FINRA equity short interest (twice-monthly settlement dates)."""
    __tablename__ = "short_interest"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    settlement_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    short_shares: Mapped[int] = mapped_column(BigInteger)
    avg_daily_volume: Mapped[int | None] = mapped_column(BigInteger)
    days_to_cover: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24))


class MacroRelease(Base):
    """Federal Reserve / BLS / FRED releases: market context only, never a stock catalyst."""
    __tablename__ = "macro_releases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[dt.datetime] = mapped_column(TZ, index=True)
    kind: Mapped[str | None] = mapped_column(String(24))
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("source", "url", name="uq_macro_source_url"),)


class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    accession: Mapped[str] = mapped_column(ForeignKey("filings.accession", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    insider_name: Mapped[str] = mapped_column(Text)
    insider_role: Mapped[str | None] = mapped_column(Text)
    txn_code: Mapped[str] = mapped_column(String(1))
    acquired_disposed: Mapped[str] = mapped_column(String(1))
    shares: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    value_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    txn_date: Mapped[dt.date] = mapped_column(Date)
    available_at: Mapped[dt.datetime] = mapped_column(TZ)
    is_10b5_1: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Earnings(Base):
    __tablename__ = "earnings"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    fiscal_period: Mapped[str] = mapped_column(String(10), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    report_date: Mapped[dt.date] = mapped_column(Date)
    timing: Mapped[str] = mapped_column(String(8), default="unknown", server_default="unknown")
    eps_est: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    eps_actual: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    rev_est: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    rev_actual: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    surprise_pct: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    __table_args__ = (CheckConstraint(_in("timing", ("bmo", "amc", "unknown")), name="ck_earnings_timing"),)


class FdaEvent(Base):
    __tablename__ = "fda_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(12), index=True)
    application_no: Mapped[str] = mapped_column(Text)
    product: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    action_date: Mapped[dt.date] = mapped_column(Date)
    available_at: Mapped[dt.datetime] = mapped_column(TZ)
    url: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("application_no", "action", "action_date", name="uq_fda_event"),)


class TrialEvent(Base):
    __tablename__ = "trial_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(12), index=True)
    nct_id: Mapped[str] = mapped_column(String(16))
    change: Mapped[str] = mapped_column(Text)
    change_date: Mapped[dt.date] = mapped_column(Date)
    available_at: Mapped[dt.datetime] = mapped_column(TZ)
    url: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("nct_id", "change", "change_date", name="uq_trial_event"),)


class PriceDaily(Base):
    __tablename__ = "prices_daily"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    high: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    low: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    adj_close: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    volume: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(20))
    fetched_at: Mapped[dt.datetime] = mapped_column(TZ)
    available_at: Mapped[dt.datetime] = mapped_column(TZ)
    __table_args__ = (
        CheckConstraint("high >= low AND open > 0 AND close > 0 AND volume >= 0", name="ck_prices_sane"),
    )


class MacroSeries(Base):
    __tablename__ = "macro_series"
    series_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    available_at: Mapped[dt.datetime] = mapped_column(TZ)


# --------------------------------------------------------------------------- derived events


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True)
    event_type: Mapped[str] = mapped_column(String(24))
    origin: Mapped[str] = mapped_column(String(10))
    # Raw news is purged after 48 h; the event keeps its own headline/url for audit.
    news_item_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id", ondelete="SET NULL"))
    accession: Mapped[str | None] = mapped_column(ForeignKey("filings.accession", ondelete="SET NULL"))
    fda_event_id: Mapped[int | None] = mapped_column(ForeignKey("fda_events.id", ondelete="SET NULL"))
    earnings_key: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(String(40))
    available_at: Mapped[dt.datetime] = mapped_column(TZ, index=True)
    polarity: Mapped[str] = mapped_column(String(10))
    strength: Mapped[str | None] = mapped_column(String(10))
    sentiment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    mixed_resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    materiality: Mapped[float | None] = mapped_column(Float)
    novelty: Mapped[float | None] = mapped_column(Float)
    credibility: Mapped[float | None] = mapped_column(Float)
    priced_in: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    classifier_version: Mapped[str] = mapped_column(String(20))
    llm_notes: Mapped[str | None] = mapped_column(Text)
    # primary = from the original source (SEC, newswire, company, agency); verified = an aggregator headline
    # matched to a primary source; unverified = aggregator/discovery only
    verification: Mapped[str | None] = mapped_column(String(12))
    original_url: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint(_in("event_type", EVENT_TYPES), name="ck_events_type"),
        CheckConstraint(_in("origin", ("news", "filing", "fda", "trial", "earnings", "gov")), name="ck_events_origin"),
        CheckConstraint(_in("polarity", POLARITIES), name="ck_events_polarity"),
        UniqueConstraint("symbol", "news_item_id", "event_type", name="uq_events_news"),
    )


# --------------------------------------------------------------------------- signals & outcomes


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())
    catalyst_type: Mapped[str] = mapped_column(String(24))
    rule_id: Mapped[str] = mapped_column(String(40))
    rule_score: Mapped[float] = mapped_column(Float)
    model_prob: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    calibration_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_snapshots.id"))
    expected_return_pct: Mapped[float] = mapped_column(Float)
    expected_return_basis: Mapped[str] = mapped_column(String(10))
    holding_days_min: Mapped[int] = mapped_column(SmallInteger)
    holding_days_max: Mapped[int] = mapped_column(SmallInteger)
    entry_ref_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    stop_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    target_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    suggested_size_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    risk_notes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    reason: Mapped[str] = mapped_column(Text)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    shap: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")
    displayed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_signals_confidence"),
        CheckConstraint(_in("expected_return_basis", ("prior", "backtest", "live")), name="ck_signals_basis"),
        CheckConstraint(_in("status", ("active", "expired", "decayed", "invalidated")), name="ck_signals_status"),
        CheckConstraint("holding_days_min >= 1 AND holding_days_max >= holding_days_min", name="ck_signals_hold"),
        CheckConstraint("stop_price < entry_ref_price", name="ck_signals_stop_below_entry"),
        # Rule 3 + 5: a displayed signal is positive-only and at least 65.
        CheckConstraint("NOT displayed OR confidence >= 65", name="ck_signals_display_threshold"),
        # Rule 6: calibrated requires a calibration snapshot.
        CheckConstraint("NOT calibrated OR calibration_id IS NOT NULL", name="ck_signals_calibrated_has_snapshot"),
        UniqueConstraint("symbol", "catalyst_type", "as_of_date", name="uq_signals_symbol_type_date"),
    )


class SignalEvent(Base):
    __tablename__ = "signal_events"
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"), primary_key=True)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True)
    horizon_days: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    entry_date: Mapped[dt.date] = mapped_column(Date)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    exit_date: Mapped[dt.date] = mapped_column(Date)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    return_pct: Mapped[float] = mapped_column(Float)
    excess_vs_spy_pct: Mapped[float | None] = mapped_column(Float)
    hit: Mapped[bool] = mapped_column(Boolean)
    computed_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())
    __table_args__ = (
        CheckConstraint("horizon_days IN (1, 3, 10)", name="ck_outcomes_horizon"),
        CheckConstraint("exit_date > entry_date OR horizon_days = 1", name="ck_outcomes_dates"),
    )


class CalibrationSnapshot(Base):
    __tablename__ = "calibration_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())
    basis: Mapped[str] = mapped_column(String(10))
    event_family: Mapped[str] = mapped_column(String(40))
    model_version: Mapped[str | None] = mapped_column(String(40))
    n: Mapped[int] = mapped_column(Integer)
    buckets: Mapped[list[Any]] = mapped_column(JSONB)
    brier: Mapped[float | None] = mapped_column(Float)
    ece: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(14))
    __table_args__ = (
        CheckConstraint(_in("basis", ("backtest", "live", "blend")), name="ck_calib_basis"),
        CheckConstraint(_in("verdict", ("good", "fair", "poor", "insufficient")), name="ck_calib_verdict"),
    )


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(TZ)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    data_sources: Mapped[list[str]] = mapped_column(ARRAY(Text))
    event_families: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(String(10))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(12))
    event_ref: Mapped[str] = mapped_column(Text)
    decision_date: Mapped[dt.date] = mapped_column(Date)
    entry_date: Mapped[dt.date] = mapped_column(Date)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    exit_date: Mapped[dt.date] = mapped_column(Date)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    exit_reason: Mapped[str] = mapped_column(String(14))
    return_pct: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        CheckConstraint("entry_date > decision_date", name="ck_bt_entry_after_decision"),
        CheckConstraint("exit_date > entry_date", name="ck_bt_no_same_day_exit"),
    )


class ModelRegistryRow(Base):
    __tablename__ = "model_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    kind: Mapped[str] = mapped_column(String(14))
    version: Mapped[str] = mapped_column(String(60))
    source_uri: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="missing", server_default="missing")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    beats_baseline: Mapped[bool | None] = mapped_column(Boolean)
    validated_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    __table_args__ = (
        CheckConstraint(_in("kind", ("sentiment", "ranker", "llm", "ts_foundation")), name="ck_models_kind"),
        CheckConstraint(_in("status", ("missing", "downloading", "ready", "failed")), name="ck_models_status"),
    )


# --------------------------------------------------------------------------- paper trading


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    start_cash: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("100"), server_default=text("100"))
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("cash >= 0", name="ck_account_cash_nonneg"),
    )


class BuyCandidate(Base):
    __tablename__ = "buy_candidates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"))
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    decision_date: Mapped[dt.date] = mapped_column(Date)
    decision: Mapped[str] = mapped_column(String(16))
    skip_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        CheckConstraint(_in("decision", ("bought", "skipped", "manual_pending")), name="ck_candidates_decision"),
        CheckConstraint("decision <> 'skipped' OR cardinality(skip_reasons) > 0", name="ck_candidates_skip_reason"),
        UniqueConstraint("account_id", "signal_id", "decision_date", name="uq_candidates"),
    )


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_positions.id", ondelete="CASCADE", use_alter=True, name="fk_orders_position")
    )
    symbol: Mapped[str] = mapped_column(String(12))
    side: Mapped[str] = mapped_column(String(4))
    notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    decision_date: Mapped[dt.date] = mapped_column(Date)
    execute_on: Mapped[dt.date] = mapped_column(Date, index=True)
    origin: Mapped[str] = mapped_column(String(8))
    exit_reason: Mapped[str | None] = mapped_column(String(14))
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    __table_args__ = (
        CheckConstraint(_in("side", ("buy", "sell")), name="ck_orders_side"),
        CheckConstraint(_in("origin", ("auto", "manual", "exit")), name="ck_orders_origin"),
        CheckConstraint(_in("status", ("pending", "filled", "cancelled", "rejected")), name="ck_orders_status"),
        CheckConstraint(
            "exit_reason IS NULL OR " + _in("exit_reason", ("target", "stop", "time_stop", "signal_decay", "manual")),
            name="ck_orders_exit_reason",
        ),
        # Rule 7: an order always executes on a later session than the one it was decided on.
        CheckConstraint("execute_on > decision_date", name="ck_orders_next_session"),
        CheckConstraint("side <> 'sell' OR position_id IS NOT NULL", name="ck_orders_sell_has_position"),
        CheckConstraint("(notional_usd IS NOT NULL) <> (qty IS NOT NULL)", name="ck_orders_size_one_of"),
    )


class PaperFill(Base):
    __tablename__ = "paper_fills"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id", ondelete="CASCADE"), unique=True)
    fill_date: Mapped[dt.date] = mapped_column(Date)
    raw_open: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    spread_bps: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    fees: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"), server_default=text("0"))
    __table_args__ = (CheckConstraint("qty > 0 AND fill_price > 0", name="ck_fills_positive"),)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(12))
    entry_fill_id: Mapped[int] = mapped_column(ForeignKey("paper_fills.id"))
    entry_date: Mapped[dt.date] = mapped_column(Date)
    qty: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stop_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    target_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    time_stop_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(6), default="open", server_default="open")
    exit_fill_id: Mapped[int | None] = mapped_column(ForeignKey("paper_fills.id"))
    exit_date: Mapped[dt.date | None] = mapped_column(Date)
    exit_reason: Mapped[str | None] = mapped_column(String(14))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    why: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    __table_args__ = (
        CheckConstraint(_in("status", ("open", "closed")), name="ck_positions_status"),
        # Rule 2 at the database level: no same-day round trip.
        CheckConstraint("exit_date IS NULL OR exit_date > entry_date", name="ck_positions_no_same_day_exit"),
        CheckConstraint("time_stop_date > entry_date", name="ck_positions_time_stop_after_entry"),
        CheckConstraint("status <> 'closed' OR (exit_date IS NOT NULL AND exit_fill_id IS NOT NULL)",
                        name="ck_positions_closed_complete"),
        Index("uq_positions_one_open_per_symbol", "account_id", "symbol", unique=True,
              postgresql_where="status = 'open'"),
    )


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id", ondelete="CASCADE"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    positions_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    equity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    spy_benchmark_equity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))


# --------------------------------------------------------------------------- notifications & settings


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    symbol: Mapped[str | None] = mapped_column(String(12))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    __table_args__ = (
        CheckConstraint(_in("kind", ("paper_buy", "high_confidence", "digest", "test")), name="ck_notifications_kind"),
        CheckConstraint(_in("status", ("queued", "sending", "sent", "failed")), name="ck_notifications_status"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[dt.datetime] = mapped_column(TZ, server_default=func.now(), onupdate=func.now())
