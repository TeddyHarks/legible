"""
legible/providers.py

Real API call wrappers for Serper, Massive.com (formerly Polygon.io),
and Together AI.

Each wrapper is a plain function ? no Legible logic inside.
Legible wraps these via session.track_call().

Setup:
    pip install httpx

Environment variables:
    SERPER_API_KEY     ? serper.dev (free: 2,500 searches/month)
    MASSIVE_API_KEY    ? massive.com (free: 5 calls/minute)
    TOGETHER_API_KEY   ? together.ai (requires $5 credit minimum)
"""

from __future__ import annotations

import os
import time
import httpx


SERPER_BASE = "https://google.serper.dev"
MASSIVE_BASE = "https://api.massive.com"
TOGETHER_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_DEFAULT_MODEL = "mistralai/Mixtral-8x7B-Instruct-v0.1"

# Rate limiter: 5 calls/minute = 12s between calls on free tier
_last_massive_call: float = 0.0
_MASSIVE_MIN_INTERVAL: float = 12.5  # set to 0 on paid plan


def _massive_rate_limit() -> None:
    global _last_massive_call
    elapsed = time.monotonic() - _last_massive_call
    if elapsed < _MASSIVE_MIN_INTERVAL:
        wait = _MASSIVE_MIN_INTERVAL - elapsed
        print(f"    [massive] rate limit pause {wait:.1f}s (free tier)")
        time.sleep(wait)
    _last_massive_call = time.monotonic()


def _massive_headers() -> dict:
    api_key = os.environ.get("MASSIVE_API_KEY", "")
    if not api_key:
        raise EnvironmentError("MASSIVE_API_KEY not set")
    return {"Authorization": f"Bearer {api_key}"}


# ??? Serper ???????????????????????????????????????????????????????????????????

def serper_search(query: str, num_results: int = 5) -> dict:
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        raise EnvironmentError("SERPER_API_KEY not set")
    r = httpx.post(f"{SERPER_BASE}/search",
                   headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                   json={"q": query, "num": num_results}, timeout=10.0)
    r.raise_for_status()
    return r.json()


def serper_news(query: str, num_results: int = 5) -> dict:
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        raise EnvironmentError("SERPER_API_KEY not set")
    r = httpx.post(f"{SERPER_BASE}/news",
                   headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                   json={"q": query, "num": num_results}, timeout=10.0)
    r.raise_for_status()
    return r.json()


# ??? Massive.com (formerly Polygon.io) ???????????????????????????????????????

def massive_previous_close(symbol: str) -> dict:
    _massive_rate_limit()
    r = httpx.get(f"{MASSIVE_BASE}/v2/aggs/ticker/{symbol.upper()}/prev",
                  headers=_massive_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


def massive_ticker_details(symbol: str) -> dict:
    _massive_rate_limit()
    r = httpx.get(f"{MASSIVE_BASE}/v3/reference/tickers/{symbol.upper()}",
                  headers=_massive_headers(), timeout=10.0)
    r.raise_for_status()
    return r.json()


def massive_news(symbol: str, limit: int = 3) -> dict:
    _massive_rate_limit()
    r = httpx.get(f"{MASSIVE_BASE}/v2/reference/news",
                  headers=_massive_headers(),
                  params={"ticker": symbol.upper(), "limit": limit},
                  timeout=10.0)
    r.raise_for_status()
    return r.json()


def massive_aggregates(symbol: str, from_date: str,
                       to_date: str, timespan: str = "day") -> dict:
    _massive_rate_limit()
    r = httpx.get(
        f"{MASSIVE_BASE}/v2/aggs/ticker/{symbol.upper()}/range/1/{timespan}/{from_date}/{to_date}",
        headers=_massive_headers(),
        params={"adjusted": "true", "sort": "asc"},
        timeout=10.0)
    r.raise_for_status()
    return r.json()


# ??? Together AI ?????????????????????????????????????????????????????????????

def together_complete(prompt: str,
                      system: str = "You are a helpful assistant. Always respond with valid JSON.",
                      model: str = TOGETHER_DEFAULT_MODEL,
                      max_tokens: int = 512,
                      temperature: float = 0.3) -> dict:
    api_key = os.environ.get("TOGETHER_API_KEY", "")
    if not api_key:
        raise EnvironmentError("TOGETHER_API_KEY not set")
    r = httpx.post(TOGETHER_URL,
                   headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                   json={"model": model,
                         "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": prompt}],
                         "max_tokens": max_tokens, "temperature": temperature},
                   timeout=30.0)
    r.raise_for_status()
    return r.json()


def together_extract_text(response: dict) -> str:
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ""
