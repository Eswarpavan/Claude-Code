"""Optional local LLM (Ollama) that writes a plain-English "why" for a displayed signal.

Hard limits, enforced here and by tests:
- OFF unless LLM_ENABLED=true.
- Only adds text (`signal.features["llm_why"]`). It never changes confidence, polarity,
  catalyst type, display status, stops, targets, sizes or trades.
- If Ollama is not installed, not reachable, slow, or returns junk, nothing happens:
  every failure is swallowed and reported as a status string.
- Only the facts already on the signal are sent, to a server on your own machine.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

MAX_CHARS = 700
CACHE_TTL_S = 7 * 24 * 3600
PROMPT_VERSION = "llm-why-v1"

SYSTEM = (
    "You explain stock research signals to a beginner in plain English. Use only the facts given. "
    "Write 2-3 short sentences: what happened, why it can matter for the share price, and the main risk. "
    "Do not give advice, do not tell anyone to buy or sell, do not predict prices, "
    "do not invent numbers, and do not change or restate the confidence score."
)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_ADVICE = re.compile(r"\b(you should|we recommend|i recommend|buy now|sell now|guaranteed)\b", re.I)


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    prompt_version: str = PROMPT_VERSION

    def as_dict(self) -> dict:
        return {"text": self.text, "model": self.model, "prompt_version": self.prompt_version,
                "label": "Written by a local AI model (Ollama). Explanation only: it does not affect the score."}


def facts(symbol: str, company: str | None, catalyst: str, headlines: Sequence[str], rule_reason: str,
          risk_notes: Sequence[str]) -> dict:
    return {"ticker": symbol, "company": company or symbol, "catalyst": catalyst.replace("_", " "),
            "headlines": list(headlines)[:5], "rule_explanation": rule_reason, "risks": list(risk_notes)[:5]}


def cache_key(model: str, f: dict) -> str:
    raw = json.dumps({"m": model, "v": PROMPT_VERSION, "f": f}, sort_keys=True).encode()
    return "llm:why:" + hashlib.sha256(raw).hexdigest()[:32]


def clean(text: str) -> str | None:
    """Strip model 'thinking', collapse whitespace, reject empty/advice-like output, cap the length."""
    t = " ".join(_THINK.sub("", text or "").split())
    if len(t) < 20 or _ADVICE.search(t):
        return None
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        t = cut[: cut.rfind(".") + 1] if "." in cut else cut.rstrip() + "…"
    return t


class OllamaExplainer:
    """Talks to Ollama's HTTP API (default http://localhost:11434). Never raises."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=2.0), transport=transport,
                                    trust_env=False)   # local server: never route through a proxy

    def status(self) -> str:
        """'ready', 'unreachable', or 'model_missing' (Ollama runs but the model is not pulled)."""
        try:
            r = self._client.get(f"{self.base_url}/api/tags", timeout=3.0)
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
        except Exception:
            return "unreachable"
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        return "ready" if wanted in names or self.model in names else "model_missing"

    def explain(self, f: dict) -> LLMResult | None:
        try:
            r = self._client.post(f"{self.base_url}/api/chat", json={
                "model": self.model, "stream": False, "think": False,
                "options": {"temperature": 0.2, "num_predict": 220},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": "Facts (JSON):\n" + json.dumps(f, ensure_ascii=False)}],
            })
            r.raise_for_status()
            text = clean((r.json().get("message") or {}).get("content", ""))
        except Exception:
            return None
        return LLMResult(text, self.model) if text else None

    def close(self) -> None:
        self._client.close()


def build_explainer(settings) -> OllamaExplainer | None:
    if not settings.llm_enabled:
        return None
    return OllamaExplainer(settings.ollama_url, settings.ollama_model, settings.llm_timeout_s)


def annotate_signals(signals: Sequence, explainer: OllamaExplainer | None, kv, *,
                     company_of: Callable[[str], str | None], headlines_of: Callable[[object], list[str]],
                     max_new: int = 10) -> dict:
    """Attach `features["llm_why"]` to displayed signals. Touches nothing else on the signal.

    Returns a status dict; never raises."""
    if explainer is None:
        return {"status": "off"}
    try:
        st = explainer.status()
        if st != "ready":
            return {"status": st, "annotated": 0}
        added = cached = failed = 0
        for sig in signals:
            f = facts(sig.symbol, company_of(sig.symbol), sig.catalyst_type, headlines_of(sig), sig.reason,
                      sig.risk_notes or [])
            key = cache_key(explainer.model, f)
            hit = kv.get(key)
            if hit is not None:
                result = json.loads(hit)
                cached += 1
            elif added + failed >= max_new:
                continue
            else:
                res = explainer.explain(f)
                if res is None:
                    failed += 1
                    continue
                result = res.as_dict()
                kv.set(key, json.dumps(result).encode(), CACHE_TTL_S)
                added += 1
            sig.features = {**(sig.features or {}), "llm_why": result}
        return {"status": "ready", "annotated": added + cached, "new": added, "cached": cached, "failed": failed}
    except Exception as e:   # the pipeline must never notice the LLM
        return {"status": f"error: {type(e).__name__}", "annotated": 0}
