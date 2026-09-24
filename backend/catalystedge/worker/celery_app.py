"""Celery worker + beat schedule. Runs independently of the UI, so paper trades and
80%+ alerts happen even when nobody has the app open (needs an always-on machine).

Start (Docker Compose does this for you):
  celery -A catalystedge.worker.celery_app worker -l info
  celery -A catalystedge.worker.celery_app beat -l info
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab

from catalystedge.config import get_settings

settings = get_settings()
broker = settings.redis_url or "redis://localhost:6379/0"
app = Celery("catalystedge", broker=broker, backend=None)
app.conf.update(
    timezone="America/New_York",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    broker_connection_retry_on_startup=True,
)
log = logging.getLogger("catalystedge.worker")

_ctx = None


def ctx():
    global _ctx
    if _ctx is None:
        from catalystedge.jobs import build_context

        _ctx = build_context(settings)
    return _ctx


def _run(name: str, fn, ttl_s: int = 1800) -> dict:
    from catalystedge.jobs import job_lock

    with job_lock(ctx().kv, name, ttl_s) as got:
        if not got:
            return {"skipped": "already running"}
        result = fn(ctx())
        log.info("%s: %s", name, result)
        return result


@app.task(name="news_poll", autoretry_for=(Exception,), retry_backoff=60, max_retries=3)
def news_poll() -> dict:
    from catalystedge.jobs import job_news

    return _run("news", job_news)


@app.task(name="events_poll", autoretry_for=(Exception,), retry_backoff=60, max_retries=3)
def events_poll() -> dict:
    from catalystedge.jobs import job_events

    return _run("events", job_events)


@app.task(name="eod", autoretry_for=(Exception,), retry_backoff=120, max_retries=3)
def eod() -> dict:
    """After the close: prices -> signals -> outcomes, exits, buy decisions, marking."""
    from catalystedge.jobs import job_paper_decide, job_prices, job_signals

    def run(c):
        return {"prices": job_prices(c), "signals": job_signals(c), "paper": job_paper_decide(c)}

    return _run("eod", run, ttl_s=3600)


@app.task(name="paper_execute", autoretry_for=(Exception,), retry_backoff=60, max_retries=3)
def paper_execute() -> dict:
    from catalystedge.jobs import job_paper_execute

    return _run("paper_execute", job_paper_execute)


@app.task(name="email_dispatch")
def email_dispatch() -> dict:
    from catalystedge.jobs import job_email

    return _run("email", job_email, ttl_s=300)


@app.task(name="retention")
def retention() -> dict:
    from catalystedge.jobs import job_retention

    return _run("retention", job_retention)


@app.task(name="refresh")
def refresh(refresh_id: str) -> dict:
    import uuid

    from catalystedge.jobs import run_refresh

    return run_refresh(ctx(), uuid.UUID(refresh_id))


def beat_schedule(profile: str) -> dict:
    """Local: news every 5 min in market hours. Cloud (Neon free tier): every 15 min, hourly otherwise."""
    market_news = "*/15" if profile == "cloud" else "*/5"
    weekdays = "mon-fri"
    return {
        "news-market-hours": {"task": "news_poll", "schedule": crontab(minute=market_news, hour="6-20",
                                                                       day_of_week=weekdays)},
        "news-off-hours": {"task": "news_poll", "schedule": crontab(minute=0, hour="0-5,21-23")},
        "news-weekend": {"task": "news_poll", "schedule": crontab(minute=30, hour="*/2", day_of_week="sat,sun")},
        "events": {"task": "events_poll", "schedule": crontab(minute="*/30" if profile == "cloud" else "*/10",
                                                              hour="6-21", day_of_week=weekdays)},
        # EOD bars are usually ready ~30 min after the close; later runs catch up (jobs are idempotent).
        "eod-1": {"task": "eod", "schedule": crontab(minute=45, hour=16, day_of_week=weekdays)},
        "eod-2": {"task": "eod", "schedule": crontab(minute=30, hour=18, day_of_week=weekdays)},
        # Fill at the official open: shortly after 09:30 via the quote, again later via the daily bar.
        "execute-open": {"task": "paper_execute", "schedule": crontab(minute=35, hour=9, day_of_week=weekdays)},
        "execute-late": {"task": "paper_execute", "schedule": crontab(minute=40, hour="11,16", day_of_week=weekdays)},
        "email": {"task": "email_dispatch", "schedule": crontab(minute="*")},
        "retention": {"task": "retention", "schedule": crontab(minute=0, hour=3)},
    }


app.conf.beat_schedule = beat_schedule(settings.profile)
