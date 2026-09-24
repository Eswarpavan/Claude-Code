"""Offline fixtures. See http/manifest.json: these are hand-written responses in
each provider's documented format, not real news (links go to example.com).
reference/company_tickers.json is a small subset in SEC's format; live runs
download the full file from SEC instead."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx

ROOT = Path(__file__).parent


def load_json(relative: str):
    return json.loads((ROOT / relative).read_text())


def recorded_at() -> dt.datetime:
    return dt.datetime.fromisoformat(load_json("http/manifest.json")["recorded_at"])


class FixtureTransport(httpx.MockTransport):
    """Serves fixture files for known (host, path, query subset) routes. Anything
    unmatched returns 404, so a typo in an adapter URL fails loudly in tests."""

    def __init__(self) -> None:
        self.manifest = load_json("http/manifest.json")
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        for route in self.manifest["routes"]:
            if route["host"] != request.url.host or route["path"] != request.url.path:
                continue
            if all(query.get(k) == v for k, v in route.get("query", {}).items()):
                return httpx.Response(200, json=load_json(f"http/{route['file']}"))
        return httpx.Response(404, json={"error": f"no fixture for {request.url.host}{request.url.path}"})
