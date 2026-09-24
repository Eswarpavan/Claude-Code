"""Offline tests for verify_sources.py: no network, fake HTTP responses."""

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
import verify_sources as vs  # noqa: E402

TODAY = dt.date(2026, 9, 24)
ALL_KEYS = {
    "FINNHUB_API_KEY": "fh_SECRET_123",
    "MARKETAUX_API_KEY": "mx_SECRET_456",
    "ALPHAVANTAGE_API_KEY": "av_SECRET_789",
    "TIINGO_API_KEY": "tg_SECRET_abc",
    "OPENFDA_API_KEY": "fda_SECRET_def",
    "FRED_API_KEY": "fred_SECRET_ghi",
    "RESEND_API_KEY": "re_SECRET_jkl",
    "SEC_USER_AGENT": "Test User test@example.com",
}


def resp(status=200, body=None, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    return vs.Response(status, {k.lower(): v for k, v in (headers or {}).items()}, raw, 5)


class FakeHTTP:
    """Routes by host + path substring; records every call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or []
        self.default = default or (lambda url: resp(200, {}))
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        for needle, fn in self.routes:
            if needle in url:
                return fn(url)
        return self.default(url)


def run(env, fake, only=None):
    return vs.run(env, only, fetch=fake, sleep=lambda s: None, today=TODAY)


class RedactionTests(unittest.TestCase):
    def test_keys_never_appear_in_report_even_when_echoed_by_provider(self):
        # Provider error bodies often echo the request URL, including the key.
        fake = FakeHTTP(default=lambda url: resp(401, {"error": f"bad request {url}"}))
        results, client = run(ALL_KEYS, fake)
        md = vs.to_markdown(results, client, dt.datetime(2026, 9, 24, tzinfo=vs.UTC))
        blob = md + client.redact(json.dumps([vs.dataclasses.asdict(r) for r in results], default=str))
        for name, secret in ALL_KEYS.items():
            if name != "SEC_USER_AGENT":
                self.assertNotIn(secret, blob, f"{name} leaked")
        self.assertIn("***", blob)


class SkipAndBlockTests(unittest.TestCase):
    def test_missing_key_is_skipped_without_any_call(self):
        fake = FakeHTTP()
        results, _ = run({}, fake, only={"finnhub", "marketaux"})
        self.assertEqual([r.status for r in results], ["skipped", "skipped"])
        self.assertEqual(fake.calls, [])

    def test_network_denial_is_reported_as_blocked_not_as_a_provider_limit(self):
        fake = FakeHTTP(default=lambda url: vs.Response(0, {}, b"", 1, error="URLError: 403 CONNECT"))
        results, client = run(ALL_KEYS, fake, only={"finnhub", "sec"})
        self.assertEqual({r.status for r in results}, {"blocked"})
        md = vs.to_markdown(results, client, dt.datetime(2026, 9, 24, tzinfo=vs.UTC))
        self.assertIn("never reached the provider", md)

    def test_disabled_stubs_are_listed_and_never_called(self):
        fake = FakeHTTP()
        results, _ = run({}, fake)
        by = {r.source: r for r in results}
        self.assertEqual(by["benzinga"].status, "skipped")
        self.assertEqual(by["investing_rss"].status, "skipped")
        self.assertFalse(any("benzinga" in u or "investing.com" in u for u, _ in fake.calls))

    def test_sec_requires_declared_user_agent(self):
        fake = FakeHTTP()
        results, _ = run({"SEC_USER_AGENT": "python-urllib"}, fake, only={"sec"})
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(fake.calls, [])

    def test_sec_sends_declared_user_agent(self):
        fake = FakeHTTP(routes=[("submissions", lambda u: resp(200, {"filings": {"recent": {"form": ["4", "8-K"]}}}))])
        run(ALL_KEYS, fake, only={"sec"})
        self.assertTrue(all(h["User-Agent"] == ALL_KEYS["SEC_USER_AGENT"] for _, h in fake.calls))


class VerdictTests(unittest.TestCase):
    def verdicts(self, results):
        return {ch.item: ch.verdict for r in results for ch in r.checks}

    def test_finnhub_free_tier_matches_docs(self):
        fake = FakeHTTP(routes=[
            ("/quote", lambda u: resp(200, {"o": 227.1}, {"X-Ratelimit-Limit": "60"})),
            ("/company-news", lambda u: resp(200, [])),
            ("/stock/candle", lambda u: resp(403, {"error": "You don't have access to this resource."})),
            ("/calendar/earnings", lambda u: resp(200, {"earningsCalendar": [{"symbol": "X"}]})),
        ])
        results, _ = run(ALL_KEYS, fake, only={"finnhub"})
        v = self.verdicts(results)
        self.assertEqual(v["Rate limit"], "confirmed")
        self.assertEqual(v["/stock/candle on free key"], "confirmed")
        self.assertEqual(v["Company-news history depth"], "confirmed")
        self.assertEqual(results[0].rate_headers, {"x-ratelimit-limit": "60"})

    def test_finnhub_longer_news_history_is_reported_as_differs(self):
        fake = FakeHTTP(routes=[
            ("/quote", lambda u: resp(200, {"o": 1}, {"X-Ratelimit-Limit": "60"})),
            ("/company-news", lambda u: resp(200, [{"headline": "old"}])),
        ])
        v = self.verdicts(run(ALL_KEYS, fake, only={"finnhub"})[0])
        self.assertEqual(v["Company-news history depth"], "differs")

    def test_marketaux_three_articles_per_request(self):
        fake = FakeHTTP(default=lambda u: resp(200, {"meta": {"limit": 3}, "data": [{}, {}, {}]}))
        v = self.verdicts(run(ALL_KEYS, fake, only={"marketaux"})[0])
        self.assertEqual(v["Articles per request (asked for 50)"], "confirmed")

    def test_alphavantage_premium_full_history(self):
        fake = FakeHTTP(routes=[
            ("NEWS_SENTIMENT", lambda u: resp(200, {"feed": [{"time_published": "20220301T120000"}]})),
            ("TIME_SERIES_DAILY", lambda u: resp(200, {"Information": "outputsize=full is a premium feature"})),
            ("EARNINGS", lambda u: resp(200, {"quarterlyEarnings": [
                {"reportedDate": f"{2000 + i // 4}-01-20", "estimatedEPS": "1.0"} for i in range(80)]})),
        ])
        v = self.verdicts(run(ALL_KEYS, fake, only={"alphavantage"})[0])
        self.assertEqual(v["NEWS_SENTIMENT history start"], "confirmed")
        self.assertEqual(v["TIME_SERIES_DAILY outputsize=full"], "confirmed")
        self.assertEqual(v["EARNINGS history with estimates (for backtest)"], "confirmed")

    def test_alphavantage_quota_message_is_not_mistaken_for_data(self):
        msg = {"Information": "We have detected your API key and our standard API rate limit is 25 requests per day."}
        fake = FakeHTTP(default=lambda u: resp(200, msg))
        v = self.verdicts(run(ALL_KEYS, fake, only={"alphavantage"})[0])
        self.assertEqual(v["NEWS_SENTIMENT history start"], "unknown")
        self.assertEqual(v["EARNINGS history with estimates (for backtest)"], "unknown")

    def test_tiingo_news_forbidden_on_free(self):
        fake = FakeHTTP(routes=[
            ("/api/test", lambda u: resp(200, {"message": "You successfully sent a request"})),
            ("/prices", lambda u: resp(200, [{"date": "1980-12-31T00:00:00.000Z"}])),
            ("/daily/lehmq", lambda u: resp(200, {"endDate": "2012-03-02"})),
            ("/tiingo/news", lambda u: resp(403, {"detail": "Error: You do not have permission to access the News API"})),
        ])
        v = self.verdicts(run(ALL_KEYS, fake, only={"tiingo"})[0])
        self.assertEqual(v["News API on your plan"], "confirmed")
        self.assertEqual(v["EOD history depth (primary price source)"], "confirmed")
        self.assertEqual(v["Delisted tickers available (LEHMQ)"], "confirmed")

    def test_one_crashing_probe_does_not_hide_the_others(self):
        fake = FakeHTTP(routes=[("finnhub.io", lambda u: resp(200, [1]))])  # list where a dict is expected -> AttributeError
        results, _ = run(ALL_KEYS, fake, only={"finnhub", "fred"})
        self.assertEqual([r.status for r in results], ["failed", "ok"])
        self.assertIn("probe crashed", results[0].note)


class BudgetTests(unittest.TestCase):
    def test_quota_budget_per_provider(self):
        fake = FakeHTTP()
        run(ALL_KEYS, fake)
        hosts = [urlsplit(u).netloc for u, _ in fake.calls]
        self.assertLessEqual(hosts.count("www.alphavantage.co"), 3)   # of 25/day
        self.assertLessEqual(hosts.count("api.marketaux.com"), 2)     # of 100/day
        self.assertLessEqual(hosts.count("api.tiingo.com"), 4)        # of 50/hour
        self.assertLessEqual(len(fake.calls), 25)
        self.assertFalse(any("resend.com/emails" in u for u, _ in fake.calls), "must never send email")

    def test_same_host_calls_are_spaced(self):
        slept = []
        fake = FakeHTTP(routes=[("/quote", lambda u: resp(200, {"o": 1}, {"X-Ratelimit-Limit": "60"}))])
        vs.run(ALL_KEYS, {"finnhub"}, fetch=fake, sleep=slept.append, today=TODAY)
        self.assertGreaterEqual(len(slept), 3)  # 4 finnhub calls → ≥ 3 waits
        self.assertTrue(all(0 < s <= vs.MIN_GAP_S for s in slept))


class EnvFileTests(unittest.TestCase):
    def test_env_file_parsing(self):
        p = Path(self._testMethodName + ".env")
        p.write_text('# c\nFINNHUB_API_KEY="abc"\nEMPTY=\nSEC_USER_AGENT=Name me@x.com\n')
        try:
            env = vs.load_env_file(p)
        finally:
            p.unlink()
        self.assertEqual(env["FINNHUB_API_KEY"], "abc")
        self.assertEqual(env["SEC_USER_AGENT"], "Name me@x.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
