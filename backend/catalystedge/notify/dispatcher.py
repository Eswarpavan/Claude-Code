"""Queue, dedupe, rate-limit, retry and log alert emails.

Kinds:
  paper_buy        every executed paper buy               dedupe: one per filled order
  high_confidence  any displayed signal with confidence >= 80   dedupe: one per ticker per news event
  digest           optional daily summary                  dedupe: one per trading day

Every email says whether the confidence is UNCALIBRATED and ends with "Not financial advice".
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.clock import ensure_utc
from catalystedge.db.models import Notification, PaperFill, PaperOrder, PaperPosition, Signal, SignalEvent
from catalystedge.notify.providers import Email, EmailProvider

HIGH_CONFIDENCE = 80.0
RETRY_DELAYS_MIN = (1, 5, 30, 120)          # then give up
MAX_ATTEMPTS = len(RETRY_DELAYS_MIN) + 1
FOOTER = "Not financial advice. CatalystEdge is a research and paper-trading tool."


def _queue(session: Session, kind: str, dedupe_key: str, payload: dict, symbol: str | None = None,
           signal_id: int | None = None) -> bool:
    """Insert once; a second call with the same dedupe key is a no-op. Returns True if newly queued."""
    row = session.execute(insert(Notification).values(
        kind=kind, dedupe_key=dedupe_key, symbol=symbol, signal_id=signal_id, payload=payload, status="queued",
    ).on_conflict_do_nothing(index_elements=[Notification.dedupe_key]).returning(Notification.id)).scalar_one_or_none()
    return row is not None


def _conf_label(sig: Signal) -> str:
    return f"{sig.confidence:.0f}%" + ("" if sig.calibrated else " (UNCALIBRATED)")


def queue_high_confidence(session: Session, sig: Signal) -> bool:
    if not sig.displayed or sig.confidence < HIGH_CONFIDENCE:
        return False
    event_ids = sorted(session.scalars(select(SignalEvent.event_id).where(SignalEvent.signal_id == sig.id)))
    event_ref = event_ids[0] if event_ids else f"signal{sig.id}"
    payload = {
        "subject": f"CatalystEdge: {sig.symbol} signal {_conf_label(sig)}",
        "lines": [
            f"{sig.symbol}: {sig.catalyst_type.replace('_', ' ')} (rule {sig.rule_id})",
            f"Confidence {_conf_label(sig)}; expected return {sig.expected_return_pct:+.1f}% over "
            f"{sig.holding_days_min}-{sig.holding_days_max} trading days",
            f"Stop-loss {float(sig.stop_price):.2f} · suggested size ${float(sig.suggested_size_usd):.2f}",
            f"Why: {sig.reason}",
            *[f"Risk: {r}" for r in (sig.risk_notes or [])],
        ],
    }
    return _queue(session, "high_confidence", f"high_confidence:{sig.symbol}:{event_ref}", payload, sig.symbol, sig.id)


def queue_paper_buy(session: Session, order: PaperOrder) -> bool:
    if order.side != "buy" or order.status != "filled":
        return False
    fill_id = session.scalar(select(PaperFill.id).where(PaperFill.order_id == order.id))
    pos = session.scalar(select(PaperPosition).where(PaperPosition.entry_fill_id == fill_id)) if fill_id else None
    sig = session.get(Signal, order.signal_id) if order.signal_id else None
    lines = [f"Bought {order.symbol} at the {order.execute_on.isoformat()} open ({order.origin} order)."]
    if pos is not None:
        lines.append(f"{float(pos.qty):.6f} shares, cost ${float(pos.cost_basis):.2f}; "
                     f"stop {float(pos.stop_price):.2f}, target {float(pos.target_price):.2f}, "
                     f"time stop {pos.time_stop_date.isoformat()}")
    if sig is not None:
        lines += [f"Signal: {sig.catalyst_type.replace('_', ' ')}, confidence {_conf_label(sig)} (rule {sig.rule_id})",
                  f"Why: {sig.reason}"]
    payload = {"subject": f"CatalystEdge paper buy: {order.symbol}", "lines": lines}
    return _queue(session, "paper_buy", f"paper_buy:{order.id}", payload, order.symbol, order.signal_id)


def queue_digest(session: Session, day: dt.date, lines: list[str]) -> bool:
    return _queue(session, "digest", f"digest:{day.isoformat()}",
                  {"subject": f"CatalystEdge daily digest {day.isoformat()}", "lines": lines})


def queue_test(session: Session, now: dt.datetime) -> int:
    """A one-off test email, sent by the worker like any alert (proves scheduler + email end to end)."""
    stamp = ensure_utc(now).strftime("%Y-%m-%d %H:%M:%S UTC")
    _queue(session, "test", f"test:{stamp}", {
        "subject": "CatalystEdge test email",
        "lines": [f"This is a test from CatalystEdge, requested at {stamp}.",
                  "If you can read this, email alerts work: 80%+ signals will arrive the same way."]})
    return session.scalar(select(Notification.id).where(Notification.dedupe_key == f"test:{stamp}"))


def render(payload: dict, to: str) -> Email:
    lines = list(payload.get("lines", []))
    text = "\n".join(lines + ["", FOOTER])
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    html_doc = (f"<div style='font-family:system-ui,sans-serif;font-size:14px'>{body}"
                f"<hr><p style='color:#666;font-size:12px'>{html.escape(FOOTER)}</p></div>")
    return Email(to=to, subject=payload.get("subject", "CatalystEdge"), text=text, html=html_doc)


@dataclass
class DispatchReport:
    sent: int = 0
    failed: int = 0
    deferred: int = 0
    skipped_reason: str | None = None


def _next_attempt_at(n: Notification) -> dt.datetime | None:
    raw = (n.payload or {}).get("_next_attempt_at")
    return dt.datetime.fromisoformat(raw) if raw else None


def dispatch(session: Session, provider: EmailProvider | None, to: str | None, now: dt.datetime,
             daily_cap: int = 50, per_run: int = 10) -> DispatchReport:
    now = ensure_utc(now)
    report = DispatchReport()
    if provider is None or not to:
        report.skipped_reason = "no email provider or ALERT_EMAIL_TO configured; messages stay queued"
        return report
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = session.scalar(select(func.count()).select_from(Notification).where(
        Notification.status == "sent", Notification.sent_at >= day_start)) or 0
    due = session.scalars(select(Notification).where(Notification.status.in_(("queued", "failed")),
                                                     Notification.attempts < MAX_ATTEMPTS)
                          .order_by(Notification.created_at, Notification.id)).all()
    for n in due:
        nxt = _next_attempt_at(n)
        if nxt is not None and nxt > now:
            report.deferred += 1
            continue
        if sent_today >= daily_cap or report.sent >= per_run:
            report.deferred += 1
            report.skipped_reason = "rate limit reached; the rest will go out on a later run"
            continue
        n.attempts += 1
        n.provider = provider.name
        try:
            provider.send(render(n.payload, to))
        except Exception as e:  # provider/network failure: retry with backoff
            n.status = "failed"
            n.last_error = f"{type(e).__name__}: {e}"[:500]
            if n.attempts < MAX_ATTEMPTS:
                delay = dt.timedelta(minutes=RETRY_DELAYS_MIN[n.attempts - 1])
                n.payload = {**n.payload, "_next_attempt_at": (now + delay).isoformat()}
            report.failed += 1
            continue
        n.status, n.sent_at, n.last_error = "sent", now, None
        report.sent += 1
        sent_today += 1
    session.flush()
    return report
