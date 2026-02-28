"""
examples/multi_runner.py

Multi-provider batch session runner.
Runs sessions against Groq and Gemini (both free, no credit card).

Produces per-provider CSI and a comparison table.

SLA targets (calibrated to real-world expectations):
    groq:   800ms   ? Groq uses custom LPU hardware, claims sub-second
    gemini: 3000ms  ? Flash model, free tier rate-limited

Usage:
    python examples/multi_runner.py --provider groq --sessions 100
    python examples/multi_runner.py --provider gemini --sessions 100
    python examples/multi_runner.py --provider groq --sessions 800
    python examples/multi_runner.py --compare   # compare all providers

Environment:
    GROQ_API_KEY    ? from console.groq.com (free, no card)
    GEMINI_API_KEY  ? from aistudio.google.com (free, no card)
    SUBSTRALINK_KERNEL_URL ? optional, default http://127.0.0.1:3000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from legible.evaluator import (
    SlaIntent as EvalIntent,
    SlaAttestationBundle,
    SlaProviderAttestation,
    evaluate_sla,
)
from legible.providers import (
    groq_summarize, groq_analyze, groq_extract,
    gemini_summarize, gemini_analyze, gemini_extract,
)
from legible.firewall import CoordinationFirewall, SessionMetrics


# ??? Topics (same as batch_runner for comparability) ?????????????????????????

TOPICS = [
    "NVIDIA AI chip demand 2025",
    "OpenAI GPT-5 release date",
    "AI agent frameworks comparison",
    "autonomous AI systems enterprise",
    "large language model inference costs",
    "AI semiconductor supply chain",
    "machine learning infrastructure trends",
    "AI regulation European Union",
    "foundation model training costs",
    "AI safety research Anthropic",
    "Federal Reserve interest rate decision",
    "S&P 500 earnings season outlook",
    "semiconductor stock market performance",
    "venture capital AI investment 2025",
    "tech IPO market conditions",
    "treasury yield curve inversion",
    "inflation data consumer price index",
    "earnings per share guidance",
    "NVIDIA quarterly earnings revenue",
    "AMD MI300 AI accelerator",
    "Microsoft Azure AI services growth",
    "Google DeepMind research breakthrough",
    "Amazon AWS infrastructure spending",
    "Meta AI Llama model performance",
    "Tesla autonomous driving update",
    "Apple silicon AI capabilities",
    "China AI investment policy",
    "quantum computing commercial applications",
    "edge AI deployment enterprise",
    "AI data center power consumption",
    "transformer architecture improvements",
    "robotics AI integration manufacturing",
    "natural language processing advances",
    "AI drug discovery clinical trials",
    "autonomous vehicle regulation update",
    "AI cybersecurity threat detection",
]

# ??? Provider config ??????????????????????????????????????????????????????????

PROVIDER_CONFIG = {
    "groq": {
        "id":       "groq_api",
        "sla_ms":   800,       # Groq claims <1s ? hold them to it
        "calls": [
            ("summarize", groq_summarize),
            ("analyze",   groq_analyze),
            ("extract",   groq_extract),
        ],
        "env_key":  "GROQ_API_KEY",
        "note":     "LPU inference, 14,400 req/day free",
    },
    "gemini": {
        "id":       "gemini_api",
        "sla_ms":   3000,      # Flash model, rate-limited free tier
        "calls": [
            ("summarize", gemini_summarize),
            ("analyze",   gemini_analyze),
            ("extract",   gemini_extract),
        ],
        "env_key":  "GEMINI_API_KEY",
        "note":     "Flash model, 15 RPM free tier",
    },
}


# ??? Kernel helpers ???????????????????????????????????????????????????????????

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
                   json={"context_id": ctx, "proposer_id": proposer,
                         "intent": intent},
                   headers=_kh(), timeout=10)
    r.raise_for_status()
    return r.json()


def kernel_resolve(did, resolver, reason):
    r = httpx.post(f"{KERNEL_URL}/decisions/{did}/resolve",
                   json={"resolver_id": resolver, "reason": reason},
                   headers=_kh(), timeout=10)
    r.raise_for_status()
    return r.json()


# ??? Single session ???????????????????????????????????????????????????????????

def run_session(
    provider_name: str,
    topic: str,
    session_num: int,
    total: int,
    use_kernel: bool = True,
    declared_var: int = 200,
    strictness: float = 1.5,
) -> dict:
    """
    Run one session against the given provider.
    3 calls per session ? clears MIN_CALLS_FOR_ATTRIBUTION.
    """
    cfg         = PROVIDER_CONFIG[provider_name]
    provider_id = cfg["id"]
    sla_ms      = cfg["sla_ms"]
    call_fns    = cfg["calls"]

    session_id  = str(uuid.uuid4())
    decision_id = None
    calls       = []

    intent_dict = {
        "type": "sla.v1", "caller_id": "multi_runner",
        "provider_id": provider_id, "declared_var": declared_var,
        "latency_ms": sla_ms, "strictness_multiplier": strictness,
        "provider_stake_ratio": 0.9, "caller_stake_ratio": 0.1,
        "dependency_depth": 1, "exposure_time_ms": 15000,
    }
    intent_json = json.dumps(intent_dict, sort_keys=True, separators=(",", ":"))

    if use_kernel:
        try:
            d = kernel_commit(
                f"multi_runner:{provider_id}:{session_id[:8]}",
                "multi_runner", intent_json)
            decision_id = d.get("id")
        except Exception:
            pass

    def tracked_call(fn, label=""):
        call_id  = str(uuid.uuid4())[:8]
        req_hash = hashlib.sha256(f"{label}:{topic}".encode()).hexdigest()
        t0 = time.monotonic()
        try:
            result  = fn(topic)
            latency = int((time.monotonic() - t0) * 1000)
            correct = isinstance(result, dict) and bool(result)
            status  = 200
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            result  = {"error": str(exc)}
            correct = False
            status  = 500
        resp_hash = hashlib.sha256(
            json.dumps(result, sort_keys=True, default=str).encode()
        ).hexdigest()
        calls.append({
            "call_id":            call_id,
            "label":              label,
            "latency_ms":         latency,
            "correctness_passed": correct,
            "status_code":        status,
            "request_hash":       req_hash,
            "response_hash":      resp_hash,
            "timestamp":          int(time.time() * 1000),
            "sla_latency_ms":     sla_ms,
            "latency_violation":  latency > sla_ms,
        })
        return result, latency, correct

    # 3 calls per session
    for label, fn in call_fns:
        tracked_call(fn, label=label)

    # Evaluate
    provider_calls = [
        SlaProviderAttestation(
            call_id=c["call_id"], timestamp=c["timestamp"],
            request_hash=c["request_hash"], response_hash=c["response_hash"],
            latency_ms=c["latency_ms"], status_code=c["status_code"],
            correctness_passed=c["correctness_passed"],
        ) for c in calls
    ]
    result = evaluate_sla(
        EvalIntent(
            declared_var=declared_var, latency_ms=sla_ms,
            strictness_multiplier=strictness, dependency_depth=1,
            exposure_time_ms=15000, provider_stake_ratio=0.9,
            caller_stake_ratio=0.1,
        ),
        SlaAttestationBundle(provider_calls=provider_calls),
    )

    if use_kernel and decision_id:
        try:
            kernel_resolve(decision_id, "legible.multi_runner",
                           result.reason_summary)
        except Exception:
            pass

    outcome_type = result.outcome.to_dict()["type"]
    latencies    = [c["latency_ms"] for c in calls]
    passed       = outcome_type == "SlaPass"

    record = {
        "provider":           provider_id,
        "provider_name":      provider_name,
        "session_num":        session_num,
        "session_id":         session_id,
        "decision_id":        decision_id,
        "topic":              topic,
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "outcome":            outcome_type,
        "passed":             passed,
        "provider_slash":     result.provider_slash,
        "caller_slash":       result.caller_slash,
        "violation_count":    result.violation_count,
        "total_calls":        result.total_calls,
        "latency_violation_count": result.latency_violation_count,
        "confidence":         result.attribution_confidence,
        "latency_ms":         latencies,
        "latency_p50":        sorted(latencies)[len(latencies) // 2],
        "latency_max":        max(latencies),
        "latency_min":        min(latencies),
        "sla_target_ms":      sla_ms,
        "calls":              calls,
    }
    return record


# ??? Batch run ????????????????????????????????????????????????????????????????

def run_batch(provider_name: str, n_sessions: int, args):
    cfg        = PROVIDER_CONFIG[provider_name]
    env_key    = cfg["env_key"]
    provider_id = cfg["id"]
    sla_ms     = cfg["sla_ms"]

    if not os.environ.get(env_key):
        print(f"\n  {env_key} not set. Export it first:")
        print(f'  $env:{env_key} = "your_key"\n')
        return

    use_kernel = kernel_health()
    today      = datetime.now().strftime("%Y%m%d")
    out_dir    = Path("batch_results")
    out_dir.mkdir(exist_ok=True)
    sessions_file = out_dir / f"{provider_name}_sessions_{today}.jsonl"

    # Load existing
    all_sessions = []
    if sessions_file.exists():
        with open(sessions_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_sessions.append(json.loads(line))

    remaining = n_sessions - len(all_sessions)
    if remaining <= 0:
        print(f"\n  Already have {len(all_sessions)} sessions for {provider_name}.")
        print(f"  Use --analyze to see results.\n")
        return

    # Header
    w = 62
    print(f"\n{'='*w}")
    print(f"  Legible Multi-Provider Runner ? {provider_name.upper()}")
    print(f"{'='*w}")
    print(f"  Provider:  {provider_id}")
    print(f"  Sessions:  {n_sessions}  ({remaining} remaining)")
    print(f"  SLA:       {sla_ms}ms")
    print(f"  Kernel:    {'yes - ' + KERNEL_URL if use_kernel else 'no'}")
    print(f"  Output:    {sessions_file}")
    print(f"  Note:      {cfg['note']}")
    print(f"{'='*w}\n")
    print(f"  Running {remaining} sessions...\n")

    # Firewall
    fw      = CoordinationFirewall(provider_id=provider_id, window_size=100)
    t_start = time.monotonic()
    errors  = 0

    sessions_so_far = len(all_sessions)

    with open(sessions_file, "a", encoding="utf-8") as out_f:
        for i in range(remaining):
            session_num = sessions_so_far + i + 1
            topic       = TOPICS[session_num % len(TOPICS)]

            try:
                record = run_session(
                    provider_name, topic, session_num, n_sessions,
                    use_kernel=use_kernel,
                )
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()
                all_sessions.append(record)

                # Update firewall
                from legible.firewall.entropy import topic_entropy_score
                sm = SessionMetrics(
                    violated      = not record["passed"],
                    latency_ms    = record["latency_p50"],
                    latency_p99_ms= record["latency_max"],
                    sla_ms        = record["sla_target_ms"],
                    confidence    = record["confidence"],
                    topic_entropy = topic_entropy_score(topic),
                    provider_id   = provider_id,
                    session_id    = record["session_id"],
                )
                fw.ingest(sm)

                icon    = "+" if record["passed"] else "!"
                p50     = record["latency_p50"]
                slash   = record["provider_slash"]
                conf    = record["confidence"]
                outcome = record["outcome"]
                print(f"  [{session_num:>4}/{n_sessions}] {icon} "
                      f"{outcome:<20} p50={p50:>5}ms  "
                      f"slash={slash:>4}  conf={conf:.2f}  "
                      f'"{topic[:35]}"')

            except Exception as exc:
                errors += 1
                print(f"  [{session_num:>4}/{n_sessions}] ERROR: {exc}")

            # Checkpoint every 25
            if session_num % 25 == 0:
                elapsed  = time.monotonic() - t_start
                rate     = session_num / (elapsed / 60) if elapsed > 0 else 0
                recent   = all_sessions[-25:]
                vrate    = sum(1 for s in recent if not s["passed"]) / len(recent) * 100
                recent_p50 = sorted([s["latency_p50"] for s in recent])[len(recent)//2]
                print(f"\n  -- Checkpoint {session_num}/{n_sessions} --")
                print(f"     CSI:              {fw.csi:.4f}  [{fw.state.value.upper()}]")
                print(f"     Violation rate:   {vrate:.1f}% (last 25)")
                print(f"     Median latency:   {recent_p50}ms")
                print(f"     Rate:             {rate:.1f} sessions/min\n")

            # Jitter between sessions
            time.sleep(random.uniform(0.1, 0.3))

    # Final summary
    elapsed_min = (time.monotonic() - t_start) / 60
    completed   = [s for s in all_sessions if "outcome" in s]
    violations  = [s for s in completed if not s.get("passed", True)]
    vrate       = len(violations) / len(completed) * 100 if completed else 0
    all_lats    = [lat for s in completed for lat in s.get("latency_ms", [])]
    all_lats.sort()

    def pct(lst, p):
        if not lst:
            return 0
        return lst[int(len(lst) * p / 100)]

    print(f"\n{'='*w}")
    print(f"  Complete ? {provider_name.upper()}")
    print(f"{'='*w}")
    print(f"  Sessions:  {len(completed)}")
    print(f"  Errors:    {errors}")
    print(f"  Time:      {elapsed_min:.1f} min")
    print(f"\n  CSI:       {fw.csi:.4f}  [{fw.state.value.upper()}]")
    print(f"  Violations: {len(violations)} ({vrate:.1f}%)")
    print(f"\n  Latency:")
    print(f"    p50  {pct(all_lats,50):>6}ms")
    print(f"    p75  {pct(all_lats,75):>6}ms")
    print(f"    p90  {pct(all_lats,90):>6}ms")
    print(f"    p95  {pct(all_lats,95):>6}ms")
    print(f"    p99  {pct(all_lats,99):>6}ms")
    print(f"\n  Outcomes:")
    from collections import Counter
    for outcome, count in Counter(s["outcome"] for s in completed).most_common():
        print(f"    {outcome:<22} {count:>4}  ({count/len(completed)*100:.1f}%)")
    print(f"{'='*w}\n")

    # Save analysis
    analysis = {
        "provider":        provider_id,
        "provider_name":   provider_name,
        "sessions":        len(completed),
        "sla_ms":          sla_ms,
        "csi":             fw.csi,
        "firewall_state":  fw.state.value,
        "violation_rate":  round(vrate / 100, 4),
        "violations":      len(violations),
        "latency_p50":     pct(all_lats, 50),
        "latency_p90":     pct(all_lats, 90),
        "latency_p95":     pct(all_lats, 95),
        "latency_p99":     pct(all_lats, 99),
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }
    analysis_file = out_dir / f"{provider_name}_analysis_{today}.json"
    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)
    print(f"  Analysis saved to {analysis_file}")


# ??? Comparison table ?????????????????????????????????????????????????????????

def compare_all():
    """Load all analysis files and print comparison table."""
    out_dir = Path("batch_results")
    results = []

    # Load provider analyses
    for name in ["serper", "groq", "gemini", "together", "massive"]:
        # find most recent
        files = sorted(out_dir.glob(f"{name}_analysis_*.json"), reverse=True)
        if not files:
            # try the original batch_runner format for serper
            files = sorted(out_dir.glob(f"analysis_*.json"), reverse=True)
            if files and name == "serper":
                with open(files[0]) as f:
                    data = json.load(f)
                    results.append({
                        "name":       "serper_api",
                        "csi":        data.get("csi", 0.9725),
                        "state":      data.get("firewall_state", "green"),
                        "vrate":      data.get("violation_rate", 0.061),
                        "p50":        data.get("latency_p50", 3521),
                        "p99":        data.get("latency_p99", 5539),
                        "sla_ms":     data.get("sla_target_ms", 5000),
                        "sessions":   data.get("sessions", 800),
                    })
            continue
        with open(files[0]) as f:
            data = json.load(f)
        results.append({
            "name":     data.get("provider_name", name) + "_api",
            "csi":      data.get("csi", 0),
            "state":    data.get("firewall_state", "unknown"),
            "vrate":    data.get("violation_rate", 0),
            "p50":      data.get("latency_p50", 0),
            "p99":      data.get("latency_p99", 0),
            "sla_ms":   data.get("sla_ms", 0),
            "sessions": data.get("sessions", 0),
        })

    if not results:
        print("\n  No analysis files found. Run sessions first:\n")
        print("  python examples/multi_runner.py --provider groq --sessions 100")
        print("  python examples/multi_runner.py --provider gemini --sessions 100\n")
        return

    results.sort(key=lambda x: x["csi"], reverse=True)

    w = 72
    print(f"\n{'='*w}")
    print(f"  Legible Provider CSI Comparison")
    print(f"{'='*w}")
    print(f"  {'Provider':<18} {'CSI':>6}  {'State':<8}  "
          f"{'ViolRate':>8}  {'p50':>7}  {'p99':>7}  {'SLA':>7}  {'N':>5}")
    print(f"  {'?'*18} {'?'*6}  {'?'*8}  {'?'*8}  {'?'*7}  {'?'*7}  {'?'*7}  {'?'*5}")
    for r in results:
        state_col = {
            "green": "GREEN ", "yellow": "YELLOW",
            "orange": "ORANGE", "red": "RED   "
        }.get(r["state"], r["state"].upper()[:6])
        print(f"  {r['name']:<18} {r['csi']:>6.4f}  "
              f"[{state_col}]  "
              f"{r['vrate']*100:>7.1f}%  "
              f"{r['p50']:>6}ms  "
              f"{r['p99']:>6}ms  "
              f"{r['sla_ms']:>5}ms  "
              f"{r['sessions']:>5}")
    print(f"{'='*w}\n")


# ??? Main ?????????????????????????????????????????????????????????????????????

import random

def main():
    parser = argparse.ArgumentParser(description="Legible Multi-Provider Runner")
    parser.add_argument("--provider", choices=["groq", "gemini"],
                        help="Which provider to run")
    parser.add_argument("--sessions", type=int, default=100,
                        help="Number of sessions (default: 100)")
    parser.add_argument("--compare", action="store_true",
                        help="Print comparison table of all providers")
    args = parser.parse_args()

    if args.compare:
        compare_all()
        return

    if not args.provider:
        parser.print_help()
        print("\n  Examples:")
        print("    python examples/multi_runner.py --provider groq --sessions 100")
        print("    python examples/multi_runner.py --provider gemini --sessions 100")
        print("    python examples/multi_runner.py --compare\n")
        return

    run_batch(args.provider, args.sessions, args)


if __name__ == "__main__":
    main()
