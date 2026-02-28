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


# ??? Groq ?????????????????????????????????????????????????????????????????????
# Free tier: 14,400 requests/day, no credit card required
# Models: llama3-8b-8192 (fastest), llama3-70b-8192, mixtral-8x7b-32768
# Docs: console.groq.com

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "llama3-8b-8192"   # fastest, lowest latency

def groq_complete(
    prompt: str,
    system: str = "You are a helpful assistant. Be concise.",
    model: str = GROQ_DEFAULT_MODEL,
    max_tokens: int = 256,
    temperature: float = 0.3,
) -> dict:
    """
    Call Groq inference API.
    Env: GROQ_API_KEY
    Free: 14,400 req/day ? no rate limiting needed for 800 sessions.
    Expected latency: 200?800ms (custom LPU hardware).
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY not set")
    r = httpx.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            "max_tokens":  max_tokens,
            "temperature": temperature,
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def groq_extract_text(response: dict) -> str:
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ""


def groq_summarize(topic: str) -> dict:
    """
    Single-call Groq task: summarize a topic.
    Used as the measurable unit in batch sessions.
    """
    return groq_complete(
        prompt=f"In 2-3 sentences, summarize the current state of: {topic}",
        max_tokens=150,
    )


def groq_analyze(topic: str) -> dict:
    """Second call variant: analysis task (slightly longer prompt)."""
    return groq_complete(
        prompt=f"What are the 3 most important recent developments in: {topic}? "
               f"Reply in plain text, no markdown.",
        max_tokens=200,
    )


def groq_extract(topic: str) -> dict:
    """Third call variant: key facts extraction."""
    return groq_complete(
        prompt=f"List 3 key facts about: {topic}. One fact per line.",
        max_tokens=150,
    )


# ??? Google Gemini ????????????????????????????????????????????????????????????
# Free tier: unlimited requests, rate-limited (15 RPM on flash)
# Model: gemini-2.0-flash (fastest) or gemini-1.5-flash
# Docs: aistudio.google.com
# Note: 15 requests/minute limit on free tier = 4s between calls minimum

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"

# Rate limiter: 15 RPM = 4s between calls (conservative: 5s)
_last_gemini_call: float = 0.0
_GEMINI_MIN_INTERVAL: float = 4.2   # 15 RPM = 4s per call, add 0.2s buffer


def _gemini_rate_limit() -> None:
    global _last_gemini_call
    elapsed = time.monotonic() - _last_gemini_call
    if elapsed < _GEMINI_MIN_INTERVAL:
        wait = _GEMINI_MIN_INTERVAL - elapsed
        time.sleep(wait)
    _last_gemini_call = time.monotonic()


def gemini_complete(
    prompt: str,
    model: str = GEMINI_DEFAULT_MODEL,
    max_tokens: int = 256,
    temperature: float = 0.3,
) -> dict:
    """
    Call Google Gemini API (generateContent endpoint).
    Env: GEMINI_API_KEY
    Free: unlimited with rate limit (15 RPM for flash).
    Expected latency: 500?2000ms.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set")
    _gemini_rate_limit()
    url = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"
    r = httpx.post(
        url,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature":     temperature,
            },
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def gemini_extract_text(response: dict) -> str:
    try:
        return response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return ""


def gemini_summarize(topic: str) -> dict:
    """Single-call Gemini task: summarize a topic."""
    return gemini_complete(
        prompt=f"In 2-3 sentences, summarize the current state of: {topic}",
        max_tokens=150,
    )


def gemini_analyze(topic: str) -> dict:
    """Second call variant: analysis task."""
    return gemini_complete(
        prompt=f"What are the 3 most important recent developments in: {topic}? "
               f"Reply in plain text, no markdown.",
        max_tokens=200,
    )


def gemini_extract(topic: str) -> dict:
    """Third call variant: key facts extraction."""
    return gemini_complete(
        prompt=f"List 3 key facts about: {topic}. One fact per line.",
        max_tokens=150,
    )
