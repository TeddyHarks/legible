"""
examples/research_agent.py

Research agent with Legible SLA enforcement.

Fulfills a research task across three hops:
  1. Serper  ? web search + news          (available now)
  2. Massive ? financial market data      (available now, 5 calls/min free)
  3. Together AI ? LLM synthesis          (available after $5 credit)

Legible wraps every external call, recording latency, correctness,
and fault attribution for each provider. All sessions commit to
your local SubstraLink kernel.

Usage:
    set SERPER_API_KEY=your_key
    set MASSIVE_API_KEY=your_key
    set SUBSTRALINK_KERNEL_URL=http://127.0.0.1:3000

    python examples/research_agent.py
    python examples/research_agent.py "Fed rate decision semiconductor stocks"

Together AI (optional, skip until you add credit):
    set TOGETHER_API_KEY=your_key
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import hashlib
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from legible.evaluator import (
    SlaIntent as EvalIntent,
    SlaAttestationBundle,
    SlaProviderAttestation,
    evaluate_sla,
)
from legible.attestation import evaluate_correctness
from legible.providers import (
    serper_search,
    serper_news,
    massive_previous_close,
    massive_ticker_details,
    massive_news,
    together_complete,
    together_extract_text,
)


# ??? Kernel client ????????????????????????????????????????????????????????????

KERNEL_URL = os.environ.get("SUBSTRALINK_KERNEL_URL", "http://127.0.0.1:3000")


def _kh():
    h = {"Content-Type": "application/json"}
    k = os.environ.get("SUBSTRALINK_API_KEY", "")
    if k:
        h["Authorization"] = f"Bearer {k}"
    return h


def kernel_health():
    try:
        return httpx.get(f"{KERNEL_URL}/ledger/health", timeout=5).status_code == 200
    except Exception:
        return False


def kernel_commit(ctx, proposer, intent):
    r = httpx.post(f"{KERNEL_URL}/decisions/commit",
                   json={"context_id": ctx, "proposer_id": proposer, "intent": intent},
                   headers=_kh(), timeout=10)
    r.raise_for_status()
    return r.json()


def kernel_attest(did, actor, approve, reasoning):
    r = httpx.post(f"{KERNEL_URL}/decisions/{did}/attest",
                   json={"actor_id": actor, "approve": approve, "reasoning": reasoning},
                   headers=_kh(), timeout=10)
    r.raise_for_status()


def kernel_resolve(did, resolver, reason):
    r = httpx.post(f"{KERNEL_URL}/decisions/{did}/resolve",
                   json={"resolver_id": resolver, "reason": reason},
                   headers=_kh(), timeout=10)
    r.raise_for_status()
    return r.json()


# ??? Session tracker ?????????????????????????????????????????????????????????

class Session:
    """Lightweight Legible session ? wraps calls, commits evidence to kernel."""

    def __init__(self, caller_id, provider_id, declared_var,
                 latency_ms, strictness, use_kernel=True):
        self.caller_id    = caller_id
        self.provider_id  = provider_id
        self.declared_var = declared_var
        self.latency_ms   = latency_ms
        self.strictness   = strictness
        self.use_kernel   = use_kernel
        self.session_id   = str(uuid.uuid4())
        self.decision_id  = None
        self._calls       = []

        intent = json.dumps({
            "type": "sla.v1", "caller_id": caller_id,
            "provider_id": provider_id, "declared_var": declared_var,
            "latency_ms": latency_ms, "strictness_multiplier": strictness,
            "provider_stake_ratio": 0.9, "caller_stake_ratio": 0.1,
            "dependency_depth": 1, "exposure_time_ms": 10000,
        }, sort_keys=True, separators=(",", ":"))

        if use_kernel:
            try:
                d = kernel_commit(f"{caller_id}:{provider_id}", caller_id, intent)
                self.decision_id = d.get("id")
            except Exception as e:
                print(f"    [kernel] commit failed: {e}")

    def track(self, fn, *args, label="", **kwargs):
        call_id  = str(uuid.uuid4())[:8]
        req_hash = hashlib.sha256(
            json.dumps({"a": str(args), "k": str(kwargs)},
                       sort_keys=True).encode()).hexdigest()

        t0 = time.monotonic()
        try:
            result  = fn(*args, **kwargs)
            latency = int((time.monotonic() - t0) * 1000)
            correct = isinstance(result, dict) and bool(result)
            status  = 200
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            result  = {"error": str(exc)}
            correct = False
            status  = 500

        resp_hash = hashlib.sha256(
            json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()

        tag = label or fn.__name__
        ok  = "?" if correct and latency <= self.latency_ms else "?"
        print(f"    [{tag}] {latency}ms  "
              f"{'PASS' if correct else 'FAIL'}  {ok}")

        rec = {"call_id": call_id, "label": tag, "latency_ms": latency,
               "correctness_passed": correct, "status_code": status,
               "request_hash": req_hash, "response_hash": resp_hash,
               "timestamp": int(time.time() * 1000),
               "violation": latency > self.latency_ms or not correct}
        self._calls.append(rec)

        # Per-call evidence lives in Legible's evidence packet.
        # Kernel attestation is a governance primitive (one per actor).
        # We commit once and resolve once ? no per-call attests.

        return result

    def finalize(self):
        provider_calls = [
            SlaProviderAttestation(
                call_id=c["call_id"], timestamp=c["timestamp"],
                request_hash=c["request_hash"], response_hash=c["response_hash"],
                latency_ms=c["latency_ms"], status_code=c["status_code"],
                correctness_passed=c["correctness_passed"],
            ) for c in self._calls
        ]

        result = evaluate_sla(
            EvalIntent(declared_var=self.declared_var, latency_ms=self.latency_ms,
                       strictness_multiplier=self.strictness, dependency_depth=1,
                       exposure_time_ms=10000, provider_stake_ratio=0.9,
                       caller_stake_ratio=0.1),
            SlaAttestationBundle(provider_calls=provider_calls),
        )

        if self.use_kernel and self.decision_id:
            try:
                kernel_resolve(self.decision_id, "legible.evaluator",
                               result.reason_summary)
            except Exception as e:
                print(f"    [kernel] resolve failed: {e}")

        return {
            "session_id": self.session_id, "decision_id": self.decision_id,
            "provider": self.provider_id,
            "outcome": result.outcome.to_dict(),
            "provider_slash": result.provider_slash,
            "caller_slash": result.caller_slash,
            "violation_count": result.violation_count,
            "total_calls": result.total_calls,
            "latency_violation_count": result.latency_violation_count,
            "correctness_violation_count": result.correctness_violation_count,
            "confidence": result.attribution_confidence,
            "reason": result.reason_summary,
            "calls": self._calls,
        }


# ??? Ticker extractor ?????????????????????????????????????????????????????????

_TICKER_MAP = {
    "NVIDIA": "NVDA", "NVDA": "NVDA", "AMD": "AMD",
    "APPLE": "AAPL",  "AAPL": "AAPL",
    "MICROSOFT": "MSFT", "MSFT": "MSFT",
    "GOOGLE": "GOOGL", "GOOGL": "GOOGL", "ALPHABET": "GOOGL",
    "AMAZON": "AMZN",  "AMZN": "AMZN",
    "META": "META",    "TESLA": "TSLA", "TSLA": "TSLA",
    "INTEL": "INTC",   "INTC": "INTC",
    "SPY": "SPY",      "QQQ": "QQQ",
}


def _extract_tickers(*data_sources) -> list[str]:
    text  = " ".join(json.dumps(d, default=str).upper() for d in data_sources)
    found = []
    for name, ticker in _TICKER_MAP.items():
        if ticker and name in text and ticker not in found:
            found.append(ticker)
    return found[:2]


# ??? Research agent ???????????????????????????????????????????????????????????

def run(topic: str, use_kernel: bool = True) -> dict:
    print(f"\n{'?'*60}")
    print(f"  Legible Research Agent")
    print(f"  Topic:  \"{topic}\"")
    print(f"  Kernel: {KERNEL_URL}")
    print(f"{'?'*60}\n")

    sessions = []
    evidence  = {"topic": topic, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}

    has_together = bool(os.environ.get("TOGETHER_API_KEY"))

    # ?? Session 1: Serper ?????????????????????????????????????????????????????
    print("Session 1/3 ? serper_api")
    print("  SLA: 400ms latency ? valid_json ? VAR=200\n")

    s1 = Session("research_agent", "serper_api", 200, 400, 1.5, use_kernel)

    search = s1.track(serper_search, topic, 5,       label="web_search")
    news   = s1.track(serper_news,   topic, 3,       label="news_search")

    r1 = s1.finalize()
    sessions.append(r1)
    print(f"\n  ? {r1['outcome']['type']}  "
          f"slash={r1['provider_slash']}  conf={r1['confidence']:.2f}\n")

    # Extract tickers from search results
    tickers = _extract_tickers(search, news)
    if not tickers:
        tickers = ["SPY"]   # fallback ? always has data

    evidence["tickers_found"] = tickers
    evidence["search_results"] = len((search or {}).get("organic", []))
    evidence["news_results"]   = len((news or {}).get("news", []))

    # ?? Session 2: Massive ????????????????????????????????????????????????????
    print(f"Session 2/3 ? massive_api  (tickers: {tickers})")
    print("  SLA: 300ms latency ? valid_json ? VAR=100")
    print("  ? Free tier: 12.5s between calls\n")

    s2 = Session("research_agent", "massive_api", 100, 300, 2.0, use_kernel)

    market = {}
    for ticker in tickers:
        data = s2.track(massive_previous_close, ticker,
                        label=f"close_{ticker}")
        if isinstance(data, dict):
            market[ticker] = data

        detail = s2.track(massive_ticker_details, ticker,
                          label=f"details_{ticker}")
        if isinstance(detail, dict):
            market[f"{ticker}_detail"] = detail

    r2 = s2.finalize()
    sessions.append(r2)
    print(f"\n  ? {r2['outcome']['type']}  "
          f"slash={r2['provider_slash']}  conf={r2['confidence']:.2f}\n")

    evidence["market_data"] = {k: v for k, v in market.items()
                                if "_detail" not in k}

    # ?? Session 3: Together AI (optional) ????????????????????????????????????
    if has_together:
        print("Session 3/3 ? together_llm")
        print("  SLA: 8000ms latency ? valid_json ? VAR=500\n")

        snippets = []
        for item in (search or {}).get("organic", [])[:4]:
            snippets.append(f"- {item.get('title','')}: {item.get('snippet','')}")
        for item in (news or {}).get("news", [])[:2]:
            snippets.append(f"- [NEWS] {item.get('title','')}")

        market_lines = []
        for ticker in tickers:
            results = market.get(ticker, {}).get("results", [])
            if results:
                r = results[0]
                market_lines.append(
                    f"{ticker}: close=${r.get('c','?')} "
                    f"volume={r.get('v','?')}")

        prompt = f"""Research topic: {topic}

Search findings:
{chr(10).join(snippets[:6]) or 'No results.'}

Market data:
{chr(10).join(market_lines) or 'No market data.'}

Respond ONLY with a JSON object (no markdown) with keys:
summary, key_insights (list), tickers_mentioned (list),
sentiment (bullish/bearish/neutral), confidence_level (high/medium/low),
follow_up_questions (list)"""

        s3 = Session("research_agent", "together_llm", 500, 8000, 1.0, use_kernel)
        llm_raw = s3.track(together_complete, prompt, label="synthesis")
        r3 = s3.finalize()
        sessions.append(r3)
        print(f"\n  ? {r3['outcome']['type']}  "
              f"slash={r3['provider_slash']}  conf={r3['confidence']:.2f}\n")

        # Parse synthesis
        synthesis = {}
        text = together_extract_text(llm_raw) if isinstance(llm_raw, dict) else ""
        if text:
            try:
                clean = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
                synthesis = json.loads(clean)
            except Exception:
                synthesis = {"raw": text, "parse_error": True}
        evidence["synthesis"] = synthesis
    else:
        print("Session 3/3 ? together_llm  [SKIPPED ? TOGETHER_API_KEY not set]")
        print("  Add $5 credit at together.ai, then set TOGETHER_API_KEY\n")

    # ?? Summary ???????????????????????????????????????????????????????????????
    total_slash = sum(s["provider_slash"] for s in sessions)
    violations  = sum(s["violation_count"] for s in sessions)
    avg_conf    = sum(s["confidence"] for s in sessions) / len(sessions)

    print(f"{'?'*60}")
    print(f"  Results")
    print(f"{'?'*60}")

    synthesis = evidence.get("synthesis", {})
    if synthesis and not synthesis.get("parse_error"):
        print(f"\n  Summary:   {synthesis.get('summary', '')}")
        for ins in synthesis.get("key_insights", [])[:3]:
            print(f"    ? {ins}")
        print(f"  Sentiment: {synthesis.get('sentiment','N/A').upper()}")
    else:
        # Show raw search findings instead
        organic = (search or {}).get("organic", [])
        if organic:
            print(f"\n  Top findings:")
            for item in organic[:3]:
                print(f"    ? {item.get('title','')}")

    print(f"\n{'?'*60}")
    print(f"  {'Provider':<20} {'Outcome':<22} {'Slash':>6}  {'Conf':>5}")
    print(f"  {'?'*20} {'?'*22} {'?'*6}  {'?'*5}")
    for s in sessions:
        print(f"  {s['provider']:<20} {s['outcome']['type']:<22} "
              f"{s['provider_slash']:>6}  {s['confidence']:>5.2f}")

    print(f"{'?'*60}")
    print(f"  Sessions:        {len(sessions)}")
    print(f"  Violations:      {violations}")
    print(f"  Total slashed:   {total_slash} shadow units")
    print(f"  Avg confidence:  {avg_conf:.2f}")
    if use_kernel:
        print(f"  Anchored to SubstraLink ?")

    # Save evidence
    fname = f"research_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d_%H%M%S')}.json"
    output = {"evidence": evidence, "sessions": sessions,
              "meta": {"topic": topic, "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                       "total_slash": total_slash, "avg_confidence": avg_conf}}
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Evidence ? {fname}\n")

    return output


# ??? Entry point ?????????????????????????????????????????????????????????????

if __name__ == "__main__":
    missing = [k for k in ["SERPER_API_KEY", "MASSIVE_API_KEY"]
               if not os.environ.get(k)]
    if missing:
        print(f"\n?  Missing required API keys: {', '.join(missing)}")
        print("   Set them in PowerShell:")
        for k in missing:
            print(f"   $env:{k} = 'your_key'")
        sys.exit(1)

    if not kernel_health():
        print(f"\n?  SubstraLink not reachable at {KERNEL_URL}")
        print("   Start: cargo run --features full")
        sys.exit(1)

    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 \
            else "AI semiconductor demand NVIDIA earnings"

    run(topic, use_kernel=True)
