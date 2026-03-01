"""
examples/gateway_replay.py

Feeds all historical sessions through the CoordinationEngine directly
(no HTTP overhead) to validate shadow routing logic on real data.

This is the fastest way to confirm the gateway works before running
it as a live service.

Usage:
    python examples/gateway_replay.py
    python examples/gateway_replay.py --batch-dir batch_results
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from legible.gateway.engine import CoordinationEngine
from legible.firewall.entropy import topic_entropy_score


# ── Load sessions ──────────────────────────────────────────────────────────────
def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def find_file(batch_dir: Path, provider_key: str) -> Path | None:
    for f in sorted(batch_dir.glob("*.jsonl")):
        if "analysis" in f.name:
            continue
        stem = f.stem
        if provider_key == "serper" and re.match(r'^sessions_\d+$', stem):
            return f
        if stem.startswith(provider_key):
            return f
    return None


def normalise(record: dict, provider_name: str) -> dict:
    topic    = record.get("topic", "")
    entropy  = topic_entropy_score(topic)
    sla      = record.get("sla_latency_ms") or record.get("sla_target_ms", 5000)
    p50      = record.get("latency_p50", 3500)
    p99      = record.get("latency_max", p50)
    conf     = float(record.get("confidence", 1.0) or 1.0)
    violated = record.get("outcome", "SlaPass") != "SlaPass"
    # batch_runner uses "passed"
    if "passed" in record:
        violated = not record["passed"]
    return {
        "provider":       provider_name,
        "violation":      violated,
        "latency_ms":     float(p50),
        "latency_p99_ms": float(p99),
        "sla_ms":         float(sla),
        "confidence":     conf,
        "entropy":        entropy,
        "topic":          topic,
        "session_id":     record.get("session_id", ""),
    }


# ── Main replay ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Gateway Replay — validate on historical data")
    parser.add_argument("--batch-dir", default="batch_results")
    parser.add_argument("--window",    type=int, default=50)
    parser.add_argument("--mode",      default="safe", choices=["safe","competitive","probabilistic"])
    parser.add_argument("--exclude",    nargs="*", default=[], help="Providers to hard-exclude (e.g. --exclude Groq)")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)

    # Load provider sessions
    provider_map = {"Serper": "serper", "Groq": "groq", "Cerebras": "cerebras"}
    sessions: dict[str, list[dict]] = {}

    for name, key in provider_map.items():
        f = find_file(batch_dir, key)
        if not f:
            print(f"  ✗ {name}: not found in {batch_dir}/")
            continue
        raw = load_jsonl(f)
        sessions[name] = [normalise(r, name) for r in raw]
        print(f"  ✓ {name:<12} {len(sessions[name])} sessions  ({f.name})")

    if len(sessions) < 2:
        print("\nNeed at least 2 providers to replay.")
        sys.exit(1)

    n = min(len(v) for v in sessions.values())

    excluded = [p.title() for p in (args.exclude or [])]
    active_providers = [p for p in sessions.keys() if p not in excluded]
    if excluded:
        print(f"  Excluded: {excluded}")

    print(f"\n  Aligned: {n} sessions · Window: {args.window}")
    print(f"  Mode:    {args.mode}\n")

    # Initialise engine
    engine = CoordinationEngine(
        providers        = active_providers,
        default_provider = "Serper",
        routing_mode     = args.mode,
    )

    # Counters
    total           = 0
    agreed          = 0
    routing_traffic = defaultdict(int)
    shadow_violations = 0    # violations that would have occurred under recommendation
    actual_violations = 0    # violations that actually occurred

    for i in range(n):
        # Get recommendation BEFORE ingesting this session's outcome
        # (router looks at history up to but not including session i)
        topic   = sessions["Serper"][i]["topic"]
        recommended, score, reason, _ = engine.recommend(topic=topic)

        # Now ingest outcomes for ALL providers at this timestep
        for name, data in sessions.items():
            sess = data[i]
            engine.report_session(
                provider        = name,
                violation       = sess["violation"],
                latency_ms      = sess["latency_ms"],
                latency_p99_ms  = sess["latency_p99_ms"],
                sla_ms          = sess["sla_ms"],
                confidence      = sess["confidence"],
                entropy         = sess["entropy"],
                topic           = sess["topic"],
                session_id      = sess["session_id"],
            )

        # Actual provider for this round = "Serper" in historical baseline
        actual_provider   = "Serper"
        actual_violation  = sessions["Serper"][i]["violation"]
        rec_violation     = sessions[recommended][i]["violation"]

        routing_traffic[recommended] += 1
        actual_violations   += int(actual_violation)
        shadow_violations   += int(rec_violation)
        if recommended == actual_provider:
            agreed += 1
        total += 1

        # Progress every 100
        if (i + 1) % 100 == 0 or i == n - 1:
            status = engine.status()
            states = {
                name: f"{d['state']:<7} CSI={d['csi']:.4f}"
                for name, d in status["providers"].items()
            }
            print(f"  Session {i+1:>4}/{n}  "
                  f"shadow_viol={shadow_violations/total*100:.1f}%  "
                  f"actual_viol={actual_violations/total*100:.1f}%  "
                  f"agree={agreed/total*100:.0f}%")
            for name, state_str in states.items():
                pct = routing_traffic[name] / total * 100
                print(f"    {name:<12} {state_str}  routed={pct:.1f}%")
            print()

    # ── Final report ───────────────────────────────────────────────────────────
    shadow_vr  = shadow_violations / total
    actual_vr  = actual_violations / total
    improvement = (actual_vr - shadow_vr) / actual_vr * 100 if actual_vr else 0

    print("=" * 62)
    print("  GATEWAY REPLAY RESULTS")
    print("=" * 62)
    print(f"  Sessions replayed:       {total}")
    print(f"  Agreement rate:          {agreed/total*100:.1f}%  "
          f"({agreed}/{total} sessions)")
    print()
    print(f"  Actual violation rate:   {actual_vr*100:.2f}%  (static Serper)")
    print(f"  Shadow violation rate:   {shadow_vr*100:.2f}%  (tri-adaptive)")
    sign = "+" if improvement > 0 else ""
    print(f"  Improvement:             {sign}{improvement:.1f}%")
    print()
    print("  Traffic distribution (shadow routing):")
    for name, count in sorted(routing_traffic.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"    {name:<14} {count:>4} sessions  ({pct:.1f}%)")
    print()

    final = engine.status()
    print("  Final provider states:")
    for name, d in final["providers"].items():
        warmup = "✓ warmed" if d["warmup_complete"] else "○ cold"
        print(f"    {name:<14} {d['state']:<8} CSI={d['csi']:.4f}  {warmup}  "
              f"{d['session_count']} sessions")
    print("=" * 62)
    print(f"\n  Gateway engine validated on {total} real sessions.")
    print(f"  Run the live service with:\n\n    python -m legible.gateway\n")


if __name__ == "__main__":
    main()