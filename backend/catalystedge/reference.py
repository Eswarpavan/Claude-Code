"""Reference data: the universe of listed companies used for ticker linking."""

from __future__ import annotations

from catalystedge.config import Settings
from catalystedge.core.http import HttpClient
from catalystedge.fixtures import load_json
from catalystedge.pipeline.ticker_link import Universe

SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"


def load_universe(settings: Settings, http: HttpClient) -> tuple[Universe, str]:
    """Returns (universe, description). Live mode downloads SEC's full list (needs SEC_USER_AGENT);
    otherwise the small offline subset is used and the description says so."""
    if settings.data_mode == "live" and settings.sec_user_agent:
        data = http.get_json("sec_edgar", SEC_COMPANY_TICKERS, headers={"User-Agent": settings.sec_user_agent})
        return Universe.from_sec_company_tickers(data), f"SEC company list ({len(data)} tickers)"
    subset = load_json("reference/company_tickers.json")
    why = "fixture mode" if settings.data_mode == "fixtures" else "SEC_USER_AGENT not set"
    return Universe.from_sec_company_tickers(subset), f"offline subset of {len(subset)} tickers ({why})"
