"""
examples/proving_ground.py

Proving ground for Legible ? runs real sessions against live APIs
and submits evidence to a local SubstraLink kernel.

This script is the first real data collection instrument.
Run it, collect sessions, analyze fault patterns.

Usage:
    python examples/proving_ground.py

Requirements:
    - SubstraLink kernel running on localhost:3000
    - pip install httpx pydantic

Environment:
    SUBSTRALINK_KERNEL_URL  (default: http://localhost:3000)
    SUBSTRALINK_API_KEY     (optional)
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ??? Kernel client (inline for self-contained example) ????????????????????????

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

KERNEL_URL = os.environ.get("SUBSTRALINK_KERNEL_URL", "http://localhost:3000")
API_KEY    = os.environ.get("SUBSTRALINK_API_KEY", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def kernel_commit(context_id: str, proposer_id: str, intent: str) -> dict:
    if not _HTTPX_AVAILABLE:
        raise RuntimeError("httpx not installed")
    r = httpx.post(
        f"{KERNEL_URL}/decisions/commit",
        json={"context_id": context_id, "proposer_id": proposer_id, "intent": intent},
        headers=_headers(), timeout=10
    )
    r.raise_for_status()
    return r.json()


def kernel_attest(decision_id: str, actor_id: str, approve: bool, reasoning: str) -> None:
    if not _HTTPX_AVAILABLE:
        return
    r = httpx.post(
        f"{KERNEL_URL}/decisions/{decision_id}/attest",
        json={"actor_id": actor_id, "approve": approve, "reasoning": reasoning},
        headers=_headers(), timeout=10
    )
    r.raise_for_status()


def kernel_resolve(decision_id: str, resolver_id: str, reason: str) -> dict:
    if not _HTTPX_AVAILABLE:
        return {}
    r = httpx.post(
        f"{KERNEL_URL}/decisions/{decision_id}/resolve",
        json={"resolver_id": resolver_id, "reason": reason},
        headers=_headers(), timeout=10
    )
    r.raise_for_status()
    return r.json()


def kernel_health() -> bool:
    if not _HTTPX_AVAILABLE:
        return False
    try:
        r = httpx.get(f"{KERNEL_URL}/ledger/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ??? Evaluator (inline ? mirrors evaluator.py exactly) ????????????????????????

import math

ACS_SHARED_THRESHOLD       = 0.65
MIN_CALLS_FOR_ATTRIBUTION  = 3
THIN_EVIDENCE_DISCOUNT     = 0.5
STRICTNESS_MIN             = 0.1
STRICTNESS_MAX             = 10.0


@dataclass
class Call:
    latency_ms: int
    correctness_passed: bool
    status_code: int = 200
    call_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


def evaluate(declared_var: int, latency_ms: int, strictness: float,
             provider_ratio: float, caller_ratio: float,
             calls: list[Call], caller_faults: int = 0) -> dict:
    if not calls:
        return {"outcome": "SlaPass", "provider_slash": 0, "caller_slash": 0,
                "confidence": 1.0, "total_calls": 0}

    total = len(calls)
    lat_v   = sum(1 for c in calls if c.latency_ms > latency_ms)
    corr_v  = sum(1 for c in calls if not c.correctness_passed)
    prov_v  = lat_v + corr_v
    total_f = prov_v + caller_faults

    if total_f == 0:
        return {"outcome": "SlaPass", "provider_slash": 0, "caller_slash": 0,
                "confidence": 1.0, "total_calls": total,
                "latency_violations": lat_v, "correctness_violations": corr_v}

    s         = max(STRICTNESS_MIN, min(STRICTNESS_MAX, strictness))
    base      = math.floor(declared_var * (prov_v / total) * s)
    pw        = prov_v / total_f
    cw        = caller_faults / total_f
    raw_conf  = abs(pw - cw)
    conf      = raw_conf * THIN_EVIDENCE_DISCOUNT if total < MIN_CALLS_FOR_ATTRIBUTION else raw_conf
    pr        = math.floor(base * provider_ratio * pw)
    cr        = math.floor(base * caller_ratio * cw)

    if conf < ACS_SHARED_THRESHOLD:
        outcome = f"SlaSharedSlash(provider={pr}, caller={cr})"
    elif pw >= cw:
        outcome = f"SlaSlashProvider({pr})" if cr == 0 else f"SlaSharedSlash(provider={pr}, caller={cr})"
    else:
        outcome = f"SlaSlashCaller({cr})" if pr == 0 else f"SlaSharedSlash(provider={pr}, caller={cr})"

    return {
        "outcome": outcome, "provider_slash": pr, "caller_slash": cr,
        "confidence": conf, "total_calls": total,
        "latency_violations": lat_v, "correctness_violations": corr_v,
        "caller_faults": caller_faults, "base_slash": base,
    }


# ??? Simulated API targets ?????????????????????????????????????????????????????
# Replace these with real API calls to collect real session data.
# Each function simulates a realistic latency distribution.

def simulate_search_api(query: str, inject_failure: bool = False) -> dict:
    """Simulates a search API (e.g. Serper, Tavily). ~80ms median, occasional spikes."""
    if inject_failure:
        time.sleep(0.35)  # Deliberate latency violation
        return {"results": [], "error": "timeout"}
    latency_sim = random.gauss(80, 25)
    time.sleep(max(0.01, latency_sim / 1000))
    return {"results": [{"title": f"Result for {query}", "url": "https://example.com"}]}


def simulate_llm_inference(prompt: str, inject_failure: bool = False) -> dict:
    """Simulates LLM inference (e.g. Together, Replicate). ~200ms median."""
    if inject_failure:
        time.sleep(0.1)
        return "not json"  # Correctness failure ? not valid JSON response
    latency_sim = random.gauss(200, 60)
    time.sleep(max(0.05, latency_sim / 1000))
    return {"response": f"Synthesized answer for: {prompt}", "tokens": random.randint(50, 200)}


def simulate_data_api(symbol: str, inject_failure: bool = False) -> dict:
    """Simulates a financial data API (e.g. Polygon). ~50ms median, very reliable."""
    if inject_failure:
        time.sleep(0.3)
        return {}
    latency_sim = random.gauss(50, 15)
    time.sleep(max(0.005, latency_sim / 1000))
    return {
        "symbol": symbol,
        "price": round(random.uniform(100, 500), 2),
        "volume": random.randint(10000, 1000000),
    }


# ??? Session runner ????????????????????????????????????????????????????????????

def run_session(
    caller_id: str,
    provider_id: str,
    declared_var: int,
    latency_ms: int,
    strictness: float,
    calls_fn,           # list of (fn, args, inject_failure)
    use_kernel: bool = True,
    session_label: str = "",
) -> dict:
    """
    Runs a complete Legible session:
      1. Commit decision to kernel
      2. Execute and track calls
      3. Evaluate locally
      4. Submit resolution to kernel
      5. Return evidence summary
    """
    session_id  = str(uuid.uuid4())
    decision_id = None

    intent_dict = {
        "type": "sla.v1",
        "caller_id": caller_id,
        "provider_id": provider_id,
        "declared_var": declared_var,
        "latency_ms": latency_ms,
        "strictness_multiplier": strictness,
        "provider_stake_ratio": 0.9,
        "caller_stake_ratio": 0.1,
        "dependency_depth": 1,
        "exposure_time_ms": 5000,
    }
    intent_json = json.dumps(intent_dict, sort_keys=True, separators=(",", ":"))

    # Commit to kernel
    if use_kernel:
        try:
            decision = kernel_commit(
                context_id=f"{caller_id}:{provider_id}",
                proposer_id=caller_id,
                intent=intent_json,
            )
            decision_id = decision.get("id", "unknown")
        except Exception as e:
            decision_id = f"kernel_error:{e}"

    # Execute calls and collect attestations
    tracked_calls = []
    for fn, args, inject in calls_fn:
        t0 = time.monotonic()
        try:
            response = fn(*args, inject_failure=inject)
            latency  = int((time.monotonic() - t0) * 1000)
            correct  = isinstance(response, dict) and len(response) > 0
            status   = 200
        except Exception as exc:
            latency  = int((time.monotonic() - t0) * 1000)
            response = {"error": str(exc)}
            correct  = False
            status   = 500

        call = Call(latency_ms=latency, correctness_passed=correct, status_code=status)
        tracked_calls.append(call)

        if use_kernel and decision_id and not decision_id.startswith("kernel_error"):
            try:
                kernel_attest(
                    decision_id=decision_id,
                    actor_id=provider_id,
                    approve=correct,
                    reasoning=json.dumps({
                        "call_id": call.call_id,
                        "latency_ms": latency,
                        "correctness_passed": correct,
                        "status_code": status,
                    })
                )
            except Exception:
                pass

    # Evaluate
    result = evaluate(
        declared_var=declared_var,
        latency_ms=latency_ms,
        strictness=strictness,
        provider_ratio=0.9,
        caller_ratio=0.1,
        calls=tracked_calls,
    )

    # Submit resolution to kernel
    if use_kernel and decision_id and not decision_id.startswith("kernel_error"):
        try:
            kernel_resolve(
                decision_id=decision_id,
                resolver_id="legible.evaluator",
                reason=str(result),
            )
        except Exception:
            pass

    return {
        "label":       session_label,
        "session_id":  session_id,
        "decision_id": decision_id,
        "caller":      caller_id,
        "provider":    provider_id,
        **result,
    }


# ??? Main proving ground ???????????????????????????????????????????????????????

def main():
    print()
    print("=" * 62)
    print("  Legible Proving Ground")
    print(f"  Kernel: {KERNEL_URL}")
    print("=" * 62)

    # Check kernel connectivity
    live = kernel_health()
    if live:
        print("  Kernel: CONNECTED ?")
    else:
        print("  Kernel: UNREACHABLE ? running in local-only mode")
        print(f"  (set SUBSTRALINK_KERNEL_URL or start kernel at {KERNEL_URL})")
    print()

    sessions = []

    # ?? Scenario 1: Clean search API workflow ?????????????????????????????????
    print("Running Scenario 1: Search API ? clean 5-call session")
    s = run_session(
        caller_id="research_agent",
        provider_id="search_api",
        declared_var=500,
        latency_ms=300,
        strictness=1.0,
        calls_fn=[(simulate_search_api, ["AI agents"], False)] * 5,
        use_kernel=live,
        session_label="search_clean",
    )
    sessions.append(s)
    print(f"  Outcome: {s['outcome']}  |  Calls: {s['total_calls']}  |  Provider slash: {s['provider_slash']}")

    # ?? Scenario 2: Search API with injected latency violations ???????????????
    print("Running Scenario 2: Search API ? 2 latency violations in 5 calls")
    calls = (
        [(simulate_search_api, ["query"], True)]  * 2 +   # injected failures
        [(simulate_search_api, ["query"], False)] * 3
    )
    s = run_session(
        caller_id="research_agent",
        provider_id="search_api",
        declared_var=500,
        latency_ms=300,
        strictness=1.5,
        calls_fn=calls,
        use_kernel=live,
        session_label="search_violations",
    )
    sessions.append(s)
    print(f"  Outcome: {s['outcome']}  |  Latency violations: {s.get('latency_violations', 0)}  |  Provider slash: {s['provider_slash']}")

    # ?? Scenario 3: LLM inference with correctness failure ????????????????????
    print("Running Scenario 3: LLM inference ? correctness failure")
    calls = (
        [(simulate_llm_inference, ["summarize"], True)]  +  # returns non-JSON
        [(simulate_llm_inference, ["summarize"], False)] * 4
    )
    s = run_session(
        caller_id="orchestrator_agent",
        provider_id="llm_inference",
        declared_var=2000,
        latency_ms=500,
        strictness=2.0,
        calls_fn=calls,
        use_kernel=live,
        session_label="llm_correctness_fail",
    )
    sessions.append(s)
    print(f"  Outcome: {s['outcome']}  |  Correctness violations: {s.get('correctness_violations', 0)}  |  Provider slash: {s['provider_slash']}")

    # ?? Scenario 4: Financial data API ? thin evidence ????????????????????????
    print("Running Scenario 4: Financial data API ? thin evidence (1 call)")
    s = run_session(
        caller_id="trading_agent",
        provider_id="market_data_api",
        declared_var=10000,
        latency_ms=100,
        strictness=3.0,
        calls_fn=[(simulate_data_api, ["AGNT"], True)],  # 1 call, injected
        use_kernel=live,
        session_label="market_data_thin_evidence",
    )
    sessions.append(s)
    print(f"  Outcome: {s['outcome']}  |  Confidence: {s['confidence']:.2f}  |  Provider slash: {s['provider_slash']}")

    # ?? Scenario 5: Clean financial data ? high VAR ???????????????????????????
    print("Running Scenario 5: Financial data ? clean high-VAR session")
    s = run_session(
        caller_id="trading_agent",
        provider_id="market_data_api",
        declared_var=10000,
        latency_ms=100,
        strictness=3.0,
        calls_fn=[(simulate_data_api, ["AGNT"], False)] * 8,
        use_kernel=live,
        session_label="market_data_clean",
    )
    sessions.append(s)
    print(f"  Outcome: {s['outcome']}  |  Calls: {s['total_calls']}  |  Provider slash: {s['provider_slash']}")

    # ?? Results summary ???????????????????????????????????????????????????????
    print()
    print("=" * 62)
    print("  Session Summary")
    print("=" * 62)
    print(f"  {'Label':<32} {'Outcome':<28} {'Conf':>5}")
    print(f"  {'-'*32} {'-'*28} {'-'*5}")
    for s in sessions:
        outcome_short = s['outcome'].split('(')[0][:28]
        print(f"  {s['label']:<32} {outcome_short:<28} {s['confidence']:>5.2f}")

    print()
    violations  = sum(1 for s in sessions if s['outcome'] != 'SlaPass')
    total_slash = sum(s['provider_slash'] for s in sessions)
    avg_conf    = sum(s['confidence'] for s in sessions) / len(sessions)
    print(f"  Sessions:        {len(sessions)}")
    print(f"  With violations: {violations}")
    print(f"  Total slashed:   {total_slash} shadow units")
    print(f"  Avg confidence:  {avg_conf:.2f}")
    if live:
        print(f"  All sessions anchored to SubstraLink kernel.")
    print()

    # Save evidence to JSON
    output_path = "proving_ground_sessions.json"
    with open(output_path, "w") as f:
        json.dump(sessions, f, indent=2)
    print(f"  Evidence saved to {output_path}")
    print()


if __name__ == "__main__":
    main()
