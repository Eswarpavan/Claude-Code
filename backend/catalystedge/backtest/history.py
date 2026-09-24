"""Historical event data for the EDGAR-based backtest (resumable, cached on disk).

Per company (SEC submissions API, one request per ~1,000 filings):
  * 8-K with items 1.01 / 2.02 / 7.01 / 8.01 -> EX-99 press-release headline (2 requests)
  * Form 4 -> raw ownership XML (1 request; path from `primaryDocument`)
Everything keeps SEC's `acceptanceDateTime` (UTC) as the moment it became public, so the
backtest replays exactly what was knowable (rule 7). Output: one JSONL file per symbol
in the cache directory, plus a `.done` marker so an interrupted run resumes.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable
from pathlib import Path

from catalystedge.adapters.events import sec
from catalystedge.core.http import HttpClient, SourceError

EIGHT_K_ITEMS = {"1.01", "2.02", "7.01", "8.01"}

# A deliberately mixed universe: large caps (analyst/earnings/contract news), biotech
# (FDA/trial), and small/mid caps (insider buying). ~100 names fits Tiingo's free
# 500 symbols/month with room for live use. Survivorship bias: these are companies that
# still exist in 2026; the report says so.
DEFAULT_UNIVERSE = (
    "AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO ORCL CRM ADBE INTC QCOM TXN MU NOW PANW CRWD SNOW "
    "JPM BAC WFC GS MS C SCHW BLK AXP V MA PYPL "
    "XOM CVX COP SLB OXY "
    "CAT DE BA LMT RTX GE HON UPS FDX UNP NOC GD "
    "WMT COST HD LOW TGT NKE SBUX MCD CMG LULU "
    "JNJ PFE MRK ABBV LLY BMY AMGN GILD REGN VRTX BIIB MRNA "
    "ALNY SRPT EXEL INCY NBIX IONS BMRN CRSP BEAM NTLA ARWR HALO ACAD AXSM KRYS TGTX CYTK MDGL INSM RYTM "
    "VKTX IOVA VCEL PTCT ARDX "
    "PLTR IONQ RKLB SOFI HOOD AFRM UPST DKNG ROKU ETSY"
).split()


def _write(path: Path, rows: Iterable[dict]) -> None:
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def _submissions(http: HttpClient, ua: str, cik: str, start: dt.date) -> list[dict]:
    """All filings since `start` as dicts (recent block plus older pages when needed)."""
    data = http.get_json("sec_edgar", f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                         headers={"User-Agent": ua})
    blocks = [data.get("filings", {}).get("recent", {})]
    for extra in data.get("filings", {}).get("files", []):
        if extra.get("filingTo", "9999") >= start.isoformat():
            blocks.append(http.get_json("sec_edgar", f"https://data.sec.gov/submissions/{extra['name']}",
                                        headers={"User-Agent": ua}))
    out = []
    for b in blocks:
        keys = [k for k in b if isinstance(b[k], list)]
        for i in range(len(b.get("accessionNumber", []))):
            row = {k: b[k][i] for k in keys}
            if row.get("filingDate", "") >= start.isoformat():
                out.append(row)
    return out


def fetch_company(http: HttpClient, ua: str, symbol: str, cik: str, start: dt.date, cache_dir: Path,
                  log: Callable[[str], None] = print) -> int:
    """Fetch one company's 8-K headlines and Form 4 transactions into <cache>/<symbol>.jsonl."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path, done = cache_dir / f"{symbol}.jsonl", cache_dir / f"{symbol}.done"
    if done.exists():
        return 0
    seen = set()
    if path.exists():
        seen = {json.loads(line)["accession"] for line in path.read_text().splitlines() if line.strip()}
    n = 0
    folder = f"{sec.ARCHIVES}/{int(cik)}"
    for f in _submissions(http, ua, cik, start):
        acc, form = f["accessionNumber"], f["form"]
        if acc in seen or form not in ("8-K", "4"):
            continue
        accepted = f.get("acceptanceDateTime", "")
        base = f"{folder}/{acc.replace('-', '')}"
        rec: dict = {"symbol": symbol, "cik": cik, "accession": acc, "form": form, "accepted_at": accepted}
        try:
            if form == "8-K":
                items = [x.strip() for x in (f.get("items") or "").split(",") if x.strip()]
                rec["items"] = items
                if set(items) & EIGHT_K_ITEMS:
                    entry = sec.FeedEntry("8-K", symbol, cik.zfill(10), "Filer", acc,
                                          dt.datetime.fromisoformat(accepted.replace("Z", "+00:00")), tuple(items),
                                          f"{base}/{acc}-index.htm")
                    rec["headline"] = sec.press_release_headline(http, ua, entry)
            else:
                doc = (f.get("primaryDocument") or "").split("/")[-1]
                if not doc.endswith(".xml"):
                    continue
                f4 = sec.parse_form4(http.get_text("sec_edgar", f"{base}/{doc}", headers={"User-Agent": ua}))
                rec["transactions"] = [t.__dict__ | {"txn_date": t.txn_date.isoformat()}
                                       for t in (f4.transactions if f4 else [])]
                # Financing context for the insider rule (IPO/offering/3.02) is derived later.
        except SourceError as e:
            rec["error"] = http.redact(str(e))[:200]
        _write(path, [rec])
        n += 1
    # Offering-type filings, used to exclude financing-driven insider buys.
    offerings = [{"symbol": symbol, "cik": cik, "accession": f["accessionNumber"], "form": f["form"],
                  "accepted_at": f.get("acceptanceDateTime", ""), "items": [x.strip() for x in
                  (f.get("items") or "").split(",") if x.strip()], "offering": True}
                 for f in _submissions(http, ua, cik, start)
                 if f["form"] in sec.OFFERING_FORMS or (f["form"] == "8-K" and "3.02" in (f.get("items") or ""))]
    _write(path, [o for o in offerings if o["accession"] not in seen])
    done.write_text(dt.datetime.now(dt.UTC).isoformat())
    log(f"{symbol}: {n} filings")
    return n


def load_company(cache_dir: Path, symbol: str) -> list[dict]:
    path = cache_dir / f"{symbol}.jsonl"
    if not path.exists():
        return []
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["accession"] + ("#o" if r.get("offering") else "")] = r
    return list(rows.values())
