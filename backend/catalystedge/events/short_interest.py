"""FINRA equity short interest (official FINRA Query API), as context on shown signals. Never a catalyst.

Dataset otcMarket/consolidatedShortInterest: twice-monthly settlement dates. Queried only for the symbols of
shown signals, cached for a day. FINRA may require free API credentials (FINRA_API_CLIENT_ID /
FINRA_API_CLIENT_SECRET, created in the FINRA API Console); without them the public request is tried and, if
FINRA refuses, the source reports "auth required" instead of failing silently.
"""

from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from catalystedge.core.http import HttpClient, ProviderError, SourceError
from catalystedge.db.models import ShortInterest
from catalystedge.events.common import IngestReport

URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials"


def _token(http: HttpClient, client_id: str | None, secret: str | None) -> str | None:
    if not (client_id and secret):
        return None
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    http.register_secret(basic)
    data = http.post_json("finra", TOKEN_URL, {}, headers={"Authorization": f"Basic {basic}"}, ttl_s=0)
    tok = data.get("access_token")
    http.register_secret(tok)
    return tok


def parse_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        try:
            out.append({"symbol": str(r["symbolCode"]).upper(),
                        "settlement_date": dt.date.fromisoformat(str(r["settlementDate"])[:10]),
                        "short_shares": int(float(r["currentShortPositionQuantity"])),
                        "avg_daily_volume": int(float(r["averageDailyVolumeQuantity"]))
                        if r.get("averageDailyVolumeQuantity") is not None else None,
                        "days_to_cover": float(r["daysToCoverQuantity"]) if r.get("daysToCoverQuantity") is not None
                        else None})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def ingest_short_interest(session: Session, http: HttpClient, symbols: Sequence[str], client_id: str | None = None,
                          secret: str | None = None) -> IngestReport:
    report = IngestReport("finra_short_interest")
    if not symbols:
        return report
    try:
        tok = _token(http, client_id, secret)
    except SourceError as e:
        report.status, report.errors = "failed", [http.redact(str(e))]
        return report
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    for sym in sorted(set(symbols))[:25]:
        body = {"compareFilters": [{"compareType": "equal", "fieldName": "symbolCode", "fieldValue": sym}],
                "sortFields": ["-settlementDate"], "limit": 2}
        try:
            rows = parse_rows(http.post_json("finra", URL, body, headers=headers))
        except ProviderError as e:
            if e.status in (401, 403):
                report.status = "disabled"
                report.errors = ["auth required: set FINRA_API_CLIENT_ID and FINRA_API_CLIENT_SECRET (free, FINRA "
                                 "API Console)"]
                return report
            report.status, report.errors = "partial", [http.redact(str(e))]
            continue
        except SourceError as e:
            report.status, report.errors = "partial", [http.redact(str(e))]
            continue
        report.fetched += len(rows)
        for r in rows:
            res = session.execute(insert(ShortInterest).values(**r, source="finra").on_conflict_do_nothing()
                                  .returning(ShortInterest.symbol))
            report.stored += res.scalar_one_or_none() is not None
    session.flush()
    return report


def short_interest_note(session: Session, symbol: str) -> dict | None:
    row = session.scalar(select(ShortInterest).where(ShortInterest.symbol == symbol)
                         .order_by(ShortInterest.settlement_date.desc()).limit(1))
    if row is None:
        return None
    dtc = f", {row.days_to_cover:.1f} days to cover" if row.days_to_cover is not None else ""
    return {"settlement_date": row.settlement_date.isoformat(), "short_shares": row.short_shares,
            "days_to_cover": row.days_to_cover,
            "text": f"Short interest {row.short_shares:,} shares{dtc} (FINRA, settled {row.settlement_date})."}
