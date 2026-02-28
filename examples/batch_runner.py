"""
examples/batch_runner.py

Runs N research sessions against Serper and Massive, collects fault
statistics, and produces the dataset for cold outreach analysis.

Target: 1000 Serper sessions (2496 credits available)
Each session uses 2 Serper credits (web_search + news_search)
1000 sessions = 2000 credits ? safely within budget.

Usage:
    python examples/batch_runner.py              # 20 sessions (test run)
    python examples/batch_runner.py --sessions 100
    python examples/batch_runner.py --sessions 1000 --serper-only
    python examples/batch_runner.py --analyze    # analyze existing data

Output:
    batch_results/sessions_YYYYMMDD.jsonl   ? one session per line
    batch_results/analysis_YYYYMMDD.json    ? fault statistics
    batch_results/outreach_data.json        ? cold outreach ready summary
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
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
from legible.providers import serper_search, serper_news

# ??? Topic bank ? varied so latency patterns are representative ???????????????
# Mix of fast queries (short, common) and slow queries (complex, rare)

TOPICS = [
    # AI / Tech
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
    # Finance
    "Federal Reserve interest rate decision",
    "S&P 500 earnings season outlook",
    "semiconductor stock market performance",
    "venture capital AI investment 2025",
    "tech IPO market conditions",
    "treasury yield curve inversion",
    "inflation data consumer price index",
    "earnings per share guidance",
    # Specific companies
    "NVIDIA quarterly earnings revenue",
    "AMD MI300 AI accelerator",
    "Microsoft Azure AI services growth",
    "Google DeepMind research breakthrough",
    "Amazon AWS infrastructure spending",
    "Meta AI Llama model performance",
    "Tesla autonomous driving update",
    "Apple silicon AI capabilities",
    # Macro / emerging
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


def kernel_resolve(did, resolver, reason):
    r = httpx.post(f"{KERNEL_URL}/decisions/{did}/resolve",
                   json={"resolver_id": resolver, "reason": reason},
                   headers=_kh(), timeout=10)
    r.raise_for_status()
    return r.json()


# ??? Single session runner ????????????????????????????????????????????????????

def run_serper_session(
    topic: str,
    session_num: int,
    total: int,
    use_kernel: bool = True,
    latency_sla_ms: int = 5000,
    declared_var: int = 200,
    strictness: float = 1.5,
) -> dict:
    """
    Run one Serper session: web_search + news_search.
    Returns session record with full evidence.
    Uses 2 Serper API credits.
    """
    import uuid, hashlib

    session_id  = str(uuid.uuid4())
    decision_id = None
    calls       = []

    intent_dict = {
        "type": "sla.v1", "caller_id": "batch_runner",
        "provider_id": "serper_api", "declared_var": declared_var,
        "latency_ms": latency_sla_ms, "strictness_multiplier": strictness,
        "provider_stake_ratio": 0.9, "caller_stake_ratio": 0.1,
        "dependency_depth": 1, "exposure_time_ms": 15000,
    }
    intent_json = json.dumps(intent_dict, sort_keys=True, separators=(",", ":"))

    if use_kernel:
        try:
            d = kernel_commit(f"batch_runner:serper_api:{session_id[:8]}",
                              "batch_runner", intent_json)
            decision_id = d.get("id")
        except Exception:
            pass

    def tracked_call(fn, *args, label=""):
        call_id  = str(uuid.uuid4())[:8]
        req_hash = hashlib.sha256(str(args).encode()).hexdigest()
        t0       = time.monotonic()
        try:
            result  = fn(*args)
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
        calls.append({
            "call_id": call_id, "label": label,
            "latency_ms": latency, "correctness_passed": correct,
            "status_code": status, "request_hash": req_hash,
            "response_hash": resp_hash,
            "timestamp": int(time.time() * 1000),
            "sla_latency_ms": latency_sla_ms,
            "latency_violation": latency > latency_sla_ms,
        })
        return result, latency, correct

    # Execute calls ? 3 calls to clear MIN_CALLS_FOR_ATTRIBUTION (3)
    # so violations get full confidence attribution, not thin-evidence SharedSlash
    _, l1, c1 = tracked_call(serper_search, topic, 5,       label="web_search")
    _, l2, c2 = tracked_call(serper_news,   topic, 3,       label="news_search")
    # Third call: related query for attribution confidence
    related = topic.split()[0] + " latest news"
    _, l3, c3 = tracked_call(serper_search, related, 3,     label="related_search")

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
        EvalIntent(declared_var=declared_var, latency_ms=latency_sla_ms,
                   strictness_multiplier=strictness, dependency_depth=1,
                   exposure_time_ms=15000, provider_stake_ratio=0.9,
                   caller_stake_ratio=0.1),
        SlaAttestationBundle(provider_calls=provider_calls),
    )

    if use_kernel and decision_id:
        try:
            kernel_resolve(decision_id, "legible.batch_runner",
                           result.reason_summary)
        except Exception:
            pass

    outcome_type = result.outcome.to_dict()["type"]
    violations   = result.violation_count
    latencies    = [c["latency_ms"] for c in calls]

    record = {
        "session_num":    session_num,
        "session_id":     session_id,
        "decision_id":    decision_id,
        "topic":          topic,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "outcome":        outcome_type,
        "provider_slash": result.provider_slash,
        "caller_slash":   result.caller_slash,
        "violation_count": violations,
        "total_calls":    result.total_calls,
        "latency_violation_count": result.latency_violation_count,
        "correctness_violation_count": result.correctness_violation_count,
        "confidence":     result.attribution_confidence,
        "latency_ms":     latencies,
        "latency_p50":    sorted(latencies)[len(latencies)//2],
        "latency_max":    max(latencies),
        "latency_min":    min(latencies),
        "sla_target_ms":  latency_sla_ms,
        "passed":         outcome_type == "SlaPass",
    }

    # Progress line
    status_icon = "?" if record["passed"] else "?"
    print(f"  [{session_num:>4}/{total}] {status_icon} "
          f"{outcome_type:<20} "
          f"p50={record['latency_p50']:>5}ms  "
          f"slash={record['provider_slash']:>4}  "
          f"conf={record['confidence']:.2f}  "
          f"\"{topic[:35]}\"")

    return record


# ??? Analysis ?????????????????????????????????????????????????????????????????

def analyze(sessions: list[dict]) -> dict:
    """Compute fault statistics from session records."""
    if not sessions:
        return {}

    n = len(sessions)
    passed   = [s for s in sessions if s["passed"]]
    violated = [s for s in sessions if not s["passed"]]

    latencies_all = [l for s in sessions for l in s["latency_ms"]]
    latencies_all.sort()

    def pct(lst, p):
        if not lst:
            return 0
        i = int(len(lst) * p / 100)
        return lst[min(i, len(lst)-1)]

    outcome_counts = {}
    for s in sessions:
        outcome_counts[s["outcome"]] = outcome_counts.get(s["outcome"], 0) + 1

    slash_amounts = [s["provider_slash"] for s in sessions if s["provider_slash"] > 0]
    confidences   = [s["confidence"] for s in sessions]

    # Violation rate by topic category
    by_topic = {}
    for s in sessions:
        t = s["topic"]
        if t not in by_topic:
            by_topic[t] = {"total": 0, "violations": 0, "latencies": []}
        by_topic[t]["total"] += 1
        by_topic[t]["violations"] += 1 if not s["passed"] else 0
        by_topic[t]["latencies"].extend(s["latency_ms"])

    worst_topics = sorted(
        [(t, d["violations"]/d["total"], sum(d["latencies"])/len(d["latencies"]))
         for t, d in by_topic.items() if d["total"] >= 2],
        key=lambda x: x[1], reverse=True
    )[:10]

    # Confidence distribution
    conf_zones = {
        "clear_0.85_1.00":    sum(1 for c in confidences if c >= 0.85),
        "probable_0.65_0.85": sum(1 for c in confidences if 0.65 <= c < 0.85),
        "ambiguous_0.00_0.65":sum(1 for c in confidences if c < 0.65),
    }

    return {
        "summary": {
            "total_sessions":       n,
            "total_credits_used":   n * 2,
            "pass_count":           len(passed),
            "violation_count":      len(violated),
            "violation_rate_pct":   round(len(violated) / n * 100, 2),
            "pass_rate_pct":        round(len(passed) / n * 100, 2),
        },
        "latency": {
            "p50_ms":  pct(latencies_all, 50),
            "p75_ms":  pct(latencies_all, 75),
            "p90_ms":  pct(latencies_all, 90),
            "p95_ms":  pct(latencies_all, 95),
            "p99_ms":  pct(latencies_all, 99),
            "min_ms":  latencies_all[0] if latencies_all else 0,
            "max_ms":  latencies_all[-1] if latencies_all else 0,
            "mean_ms": round(sum(latencies_all)/len(latencies_all)) if latencies_all else 0,
        },
        "outcomes": outcome_counts,
        "slash": {
            "total_shadow_units": sum(s["provider_slash"] for s in sessions),
            "sessions_with_slash": len(slash_amounts),
            "mean_slash": round(sum(slash_amounts)/len(slash_amounts)) if slash_amounts else 0,
            "max_slash":  max(slash_amounts) if slash_amounts else 0,
        },
        "confidence": {
            "mean": round(sum(confidences)/len(confidences), 3),
            "distribution": conf_zones,
        },
        "worst_topics": [
            {"topic": t, "violation_rate": round(r*100, 1), "mean_latency_ms": round(l)}
            for t, r, l in worst_topics
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sla_target_ms": sessions[0].get("sla_target_ms") if sessions else None,
    }


def build_outreach_summary(analysis: dict, provider: str = "Serper") -> dict:
    """
    Distills analysis into the cold outreach data packet.
    This is what you send to the API provider.
    """
    s = analysis["summary"]
    l = analysis["latency"]
    sl = analysis["slash"]
    c = analysis["confidence"]

    return {
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline": (
            f"Across {s['total_sessions']} agent sessions, {provider} API "
            f"violated our latency SLA in {s['violation_rate_pct']}% of calls. "
            f"Median latency from inside an agent workflow: {l['p50_ms']}ms. "
            f"P95: {l['p95_ms']}ms."
        ),
        "key_metrics": {
            "sessions_analyzed":  s["total_sessions"],
            "violation_rate":     f"{s['violation_rate_pct']}%",
            "latency_p50_ms":     l["p50_ms"],
            "latency_p95_ms":     l["p95_ms"],
            "latency_p99_ms":     l["p99_ms"],
            "total_shadow_slash": sl["total_shadow_units"],
            "avg_confidence":     c["mean"],
        },
        "what_this_data_is": (
            "Latency measured from inside an autonomous agent workflow ? "
            "not from a health check or synthetic monitor. "
            "Each session represents a real task fulfillment attempt "
            "where your API was a dependency in a multi-hop coordination chain."
        ),
        "what_you_dont_have": (
            "Your own dashboards show availability and status codes. "
            "They don't show latency from inside agent workflows, "
            "fault attribution across multi-hop chains, or "
            "what the economic consequence of your SLA profile is "
            "for the agents depending on you."
        ),
        "offer": (
            "We can share the full evidence packet: "
            "per-session latency distributions, violation patterns by query type, "
            "and cryptographic session records. "
            "No ask. Just data."
        ),
    }


# ??? Main ?????????????????????????????????????????????????????????????????????

def main():
    parser = argparse.ArgumentParser(description="Legible batch session runner")
    parser.add_argument("--sessions",    type=int, default=20,
                        help="Number of sessions to run (default: 20)")
    parser.add_argument("--serper-only", action="store_true",
                        help="Skip Massive API (faster, uses only Serper)")
    parser.add_argument("--sla-ms",      type=int, default=5000,
                        help="Serper latency SLA target in ms (default: 5000)")
    parser.add_argument("--analyze",     action="store_true",
                        help="Analyze existing session data without running new ones")
    parser.add_argument("--no-kernel",   action="store_true",
                        help="Skip kernel commits (faster for testing)")
    args = parser.parse_args()

    output_dir = Path("batch_results")
    output_dir.mkdir(exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    sessions_file = output_dir / f"sessions_{today}.jsonl"
    analysis_file = output_dir / f"analysis_{today}.json"
    outreach_file = output_dir / "outreach_data.json"

    # ?? Analyze existing data ?????????????????????????????????????????????????
    if args.analyze:
        all_sessions = []
        for f in sorted(output_dir.glob("sessions_*.jsonl")):
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        all_sessions.append(json.loads(line))

        if not all_sessions:
            print("No session data found in batch_results/")
            return

        print(f"\nAnalyzing {len(all_sessions)} sessions...\n")
        analysis = analyze(all_sessions)
        _print_analysis(analysis)

        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2)

        outreach = build_outreach_summary(analysis, "Serper")
        with open(outreach_file, "w") as f:
            json.dump(outreach, f, indent=2)

        print(f"\n  Analysis ? {analysis_file}")
        print(f"  Outreach data ? {outreach_file}\n")
        return

    # ?? Pre-flight checks ?????????????????????????????????????????????????????
    if not os.environ.get("SERPER_API_KEY"):
        print("\n?  SERPER_API_KEY not set")
        sys.exit(1)

    use_kernel = not args.no_kernel
    if use_kernel and not kernel_health():
        print(f"\n?  SubstraLink not reachable at {KERNEL_URL}")
        print("   Start kernel or use --no-kernel flag")
        sys.exit(1)

    n          = args.sessions
    credits_needed = n * 3
    print(f"\n{'?'*62}")
    print(f"  Legible Batch Runner")
    print(f"{'?'*62}")
    print(f"  Sessions:      {n}")
    print(f"  Serper credits:{credits_needed} (3 per session)")
    print(f"  SLA target:    {args.sla_ms}ms")
    print(f"  Kernel:        {'yes ? ' + KERNEL_URL if use_kernel else 'no'}")
    print(f"  Output:        {sessions_file}")
    print(f"{'?'*62}\n")

    if credits_needed > 2400:
        print(f"  {credits_needed} credits needed, budget is ~2496.")
        print(f"  Max safe sessions: 800 (2400 credits at 3 per session)")
        sys.exit(1)

    # ?? Run sessions ??????????????????????????????????????????????????????????
    all_sessions = []
    start_time   = time.monotonic()
    errors       = 0

    # Load existing sessions from today if resuming
    if sessions_file.exists():
        with open(sessions_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    all_sessions.append(json.loads(line))
        if all_sessions:
            print(f"  Resuming: {len(all_sessions)} sessions already recorded today\n")

    remaining = n - len(all_sessions)
    if remaining <= 0:
        print(f"  Already have {len(all_sessions)} sessions. Run --analyze to see results.")
        return

    print(f"  Running {remaining} sessions...\n")

    sessions_so_far = len(all_sessions)
    with open(sessions_file, "a") as out_f:
        for i in range(remaining):
            session_num = sessions_so_far + i + 1
            topic       = TOPICS[session_num % len(TOPICS)]

            # Small jitter between sessions (0.5-2s) ? polite to the API
            if i > 0:
                time.sleep(random.uniform(0.5, 2.0))

            try:
                record = run_serper_session(
                    topic=topic,
                    session_num=session_num,
                    total=n,
                    use_kernel=use_kernel,
                    latency_sla_ms=args.sla_ms,
                )
                all_sessions.append(record)
                out_f.write(json.dumps(record) + "\n")
                out_f.flush()

            except Exception as e:
                errors += 1
                print(f"  [{session_num:>4}/{n}] ERROR: {e}")
                if errors > 10:
                    print("\n  Too many errors. Stopping.")
                    break

            # Print running stats every 25 sessions
            if session_num % 25 == 0 and len(all_sessions) >= 5:
                recent    = all_sessions[-25:]
                viol_rate = sum(1 for s in recent if not s["passed"]) / len(recent) * 100
                avg_lat   = sum(s["latency_p50"] for s in recent) / len(recent)
                elapsed   = time.monotonic() - start_time
                rate      = session_num / elapsed * 60
                print(f"\n  ?? Checkpoint {session_num}/{n} ??")
                print(f"     Recent violation rate: {viol_rate:.1f}%")
                print(f"     Recent p50 latency:    {avg_lat:.0f}ms")
                print(f"     Rate: {rate:.1f} sessions/min")
                print(f"     Serper credits used: ~{session_num * 3}")
                print()

    # ?? Final analysis ????????????????????????????????????????????????????????
    elapsed = time.monotonic() - start_time
    print(f"\n{'?'*62}")
    print(f"  Batch Complete")
    print(f"{'?'*62}")
    print(f"  Sessions run:    {len(all_sessions)}")
    print(f"  Errors:          {errors}")
    print(f"  Time:            {elapsed/60:.1f} min")
    print(f"  Credits used:    ~{len(all_sessions) * 3}")
    print()

    if len(all_sessions) >= 5:
        analysis = analyze(all_sessions)
        _print_analysis(analysis)

        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2)

        outreach = build_outreach_summary(analysis, "Serper")
        with open(outreach_file, "w") as f:
            json.dump(outreach, f, indent=2)

        print(f"\n  Sessions  ? {sessions_file}")
        print(f"  Analysis  ? {analysis_file}")
        print(f"  Outreach  ? {outreach_file}\n")

    print(f"  Next: python examples/batch_runner.py --analyze")
    print()


def _print_analysis(a: dict):
    s  = a["summary"]
    l  = a["latency"]
    sl = a["slash"]
    c  = a["confidence"]

    print(f"  {'?'*56}")
    print(f"  Sessions:        {s['total_sessions']}")
    print(f"  Violation rate:  {s['violation_rate_pct']}%  "
          f"({s['violation_count']} violated / {s['pass_count']} passed)")
    print()
    print(f"  Latency (all calls):")
    print(f"    p50  {l['p50_ms']:>6}ms")
    print(f"    p75  {l['p75_ms']:>6}ms")
    print(f"    p90  {l['p90_ms']:>6}ms")
    print(f"    p95  {l['p95_ms']:>6}ms")
    print(f"    p99  {l['p99_ms']:>6}ms")
    print()
    print(f"  Outcomes:")
    for outcome, count in sorted(a["outcomes"].items()):
        pct = count / s["total_sessions"] * 100
        print(f"    {outcome:<25} {count:>4}  ({pct:.1f}%)")
    print()
    print(f"  Slash:           {sl['total_shadow_units']:,} total shadow units")
    print(f"  Avg confidence:  {c['mean']:.3f}")
    conf_d = c["distribution"]
    print(f"  Confidence zones:")
    print(f"    clear  (?0.85): {conf_d['clear_0.85_1.00']:>4}")
    print(f"    probable (0.65-0.85): {conf_d['probable_0.65_0.85']:>4}")
    print(f"    ambiguous (<0.65): {conf_d['ambiguous_0.00_0.65']:>4}")
    if a.get("worst_topics"):
        print(f"\n  Highest violation topics:")
        for t in a["worst_topics"][:5]:
            print(f"    {t['violation_rate']:>5.1f}%  {t['mean_latency_ms']:>6}ms  {t['topic']}")
    print(f"  {'?'*56}")


if __name__ == "__main__":
    main()
