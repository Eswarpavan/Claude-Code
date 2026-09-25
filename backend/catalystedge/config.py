"""Settings. Every secret comes from the environment (or .env); nothing is hard-coded."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    profile: Literal["local", "cloud"] = Field("local", alias="CATALYSTEDGE_PROFILE")
    # live = call real providers; fixtures = replay recorded responses (offline dev, tests).
    data_mode: Literal["live", "fixtures"] = Field("live", alias="CATALYSTEDGE_DATA_MODE")

    database_url: str = Field(
        "postgresql+psycopg://catalystedge:catalystedge@localhost:5432/catalystedge", alias="DATABASE_URL"
    )
    redis_url: str | None = Field(None, alias="REDIS_URL")               # cache, budgets, locks (Upstash in cloud)
    celery_broker_url: str | None = Field(None, alias="CELERY_BROKER_URL")  # task queue; defaults to REDIS_URL

    # Provider keys (all optional; a missing key disables that adapter).
    finnhub_api_key: str | None = Field(None, alias="FINNHUB_API_KEY")
    marketaux_api_key: str | None = Field(None, alias="MARKETAUX_API_KEY")
    alphavantage_api_key: str | None = Field(None, alias="ALPHAVANTAGE_API_KEY")
    tiingo_api_key: str | None = Field(None, alias="TIINGO_API_KEY")
    sec_user_agent: str | None = Field(None, alias="SEC_USER_AGENT")

    tiingo_news_enabled: bool = Field(False, alias="TIINGO_NEWS_ENABLED")
    fda_press_rss_url: str = Field(
        "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
        alias="FDA_PRESS_RSS_URL")
    fred_api_key: str | None = Field(None, alias="FRED_API_KEY")                  # free, fred.stlouisfed.org
    finra_client_id: str | None = Field(None, alias="FINRA_API_CLIENT_ID")        # optional, FINRA API Console
    finra_client_secret: str | None = Field(None, alias="FINRA_API_CLIENT_SECRET")
    sam_gov_api_key: str | None = Field(None, alias="SAM_GOV_API_KEY")        # free at api.data.gov
    # Official newswire RSS feeds (primary sources). Set a URL empty to switch that wire off.
    globenewswire_rss_url: str = Field(
        "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
        alias="GLOBENEWSWIRE_RSS_URL")
    prnewswire_rss_url: str = Field("https://www.prnewswire.com/rss/news-releases-list.rss",
                                    alias="PRNEWSWIRE_RSS_URL")
    businesswire_rss_url: str = Field("https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",
                                      alias="BUSINESSWIRE_RSS_URL")
    # Company-news queries on every poll, in addition to holdings and tickers with fresh positive events.
    # Market-wide headlines rarely carry company catalysts, so this list sets the discovery breadth.
    news_watchlist: str = Field(
        "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,AVGO,NFLX,JPM,LLY,UNH,XOM,COST,CRM,ORCL,PLTR,UBER,INTC",
        alias="NEWS_WATCHLIST")
    # Alpha Vantage does not document the zone of NEWS_SENTIMENT.time_published.
    # UTC is the conservative reading (items can only look older, never newer).
    alphavantage_news_tz: str = Field("UTC", alias="ALPHAVANTAGE_NEWS_TZ")

    # Models
    models_dir: Path = Field(Path("./models"), alias="MODELS_DIR")
    sentiment_model: str = Field("finbert", alias="SENTIMENT_MODEL")
    model_auto_download: bool = Field(True, alias="MODEL_AUTO_DOWNLOAD")

    # Email alerts (first configured provider wins: Resend, SendGrid, SMTP).
    resend_api_key: str | None = Field(None, alias="RESEND_API_KEY")
    sendgrid_api_key: str | None = Field(None, alias="SENDGRID_API_KEY")
    smtp_host: str | None = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str | None = Field(None, alias="SMTP_USER")
    smtp_password: str | None = Field(None, alias="SMTP_PASSWORD")
    email_from: str | None = Field(None, alias="EMAIL_FROM")
    alert_email_to: str | None = Field(None, alias="ALERT_EMAIL_TO")
    email_daily_cap: int = Field(50, alias="EMAIL_DAILY_CAP")
    daily_digest: bool = Field(False, alias="DAILY_DIGEST")

    # API / UI
    # Optional local LLM (Ollama): writes a plain-English "why" only; never changes scores. Off by default.
    llm_enabled: bool = Field(False, alias="LLM_ENABLED")
    ollama_url: str = Field("http://localhost:11434", alias="OLLAMA_URL")
    ollama_model: str = Field("qwen3:4b", alias="OLLAMA_MODEL")
    llm_timeout_s: float = Field(30.0, alias="LLM_TIMEOUT_S")

    app_password: str | None = Field(None, alias="APP_PASSWORD")
    app_secret: str | None = Field(None, alias="APP_SECRET")      # signs login tokens; random if unset
    cors_origins: str = Field("http://localhost:3000", alias="CORS_ORIGINS")
    refresh_cooldown_s: int = Field(300, alias="REFRESH_COOLDOWN_S")

    # Paper trading safety: auto-buy is OFF by default and additionally gated
    # server-side by calibration (rule 6). This flag alone can never enable it.
    auto_buy_requested: bool = Field(False, alias="AUTO_BUY")
    auto_buy_min_closed_trades: int = Field(30, alias="AUTO_BUY_MIN_CLOSED_TRADES")


@lru_cache
def get_settings() -> Settings:
    return Settings()
