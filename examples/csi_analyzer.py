"""
examples/csi_analyzer.py

Reads existing batch session data and computes:
  1. Full CSI decomposition for Serper (800 sessions)
  2. Rolling CSI timeline ? how stability evolved over time
  3. Per-topic entropy analysis ? which topics drove instability
  4. Firewall state simulation ? what state would have triggered when
  5. Provider comparison scaffold (ready for Together + Massive data)

Usage:
    python examples/csi_analyzer.py
    python examples/csi_analyzer.py --input batch_results/sessions_20260227.jsonl
    python examples/csi_analyzer.py --window 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from legible.firewall import (
    CoordinationFirewall,
    SessionMetrics,
    RollingCSI,
    topic_entropy_score,
)
from legible.firewall.state_machine import FirewallState, csi_to_state, STATE_COLORS


# ??? Load sessions ????????????????????????????????????????????????????????????

def load_sessions(path: str) -> list[dict]:
    sessions = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sessions.append(json.loads(line))
    return sessions


def to_metrics(record: dict) -> SessionMetrics:
    return SessionMetrics.from_batch_record(record)


# ??? Analysis ?????????????????????????????????????????????????????????????????

def analyze_csi(sessions: list[dict], window_size: int = 100) -> dict:
    metrics = [to_metrics(s) for s in sessions]

    # Full window CSI
    engine = RollingCSI(window_size=window_size)
    engine.add_many(metrics)
    components = engine.compute()

    # Rolling timeline ? CSI every 25 sessions
    engine2    = RollingCSI(window_size=window_size)
    timeline   = engine2.rolling_csi_series(metrics, step=25)

    # Firewall simulation
    fw = CoordinationFirewall(provider_id="serper_api", window_size=window_size)
    transitions = fw.ingest_many(metrics)

    # Per-topic analysis
    topic_stats: dict = defaultdict(lambda: {"total": 0, "violations": 0,
                                              "latencies": [], "entropy": 0.0})
    for s in sessions:
        t = s.get("topic", "unknown")
        topic_stats[t]["total"]      += 1
        topic_stats[t]["violations"] += 0 if s.get("passed", True) else 1
        topic_stats[t]["latencies"].extend(s.get("latency_ms", []))
        topic_stats[t]["entropy"]     = topic_entropy_score(t)

    topic_summary = []
    for topic, data in topic_stats.items():
        vrate = data["violations"] / data["total"] if data["total"] > 0 else 0
        mlat  = sum(data["latencies"]) / len(data["latencies"]) if data["latencies"] else 0
        topic_summary.append({
            "topic":          topic,
            "total":          data["total"],
            "violations":     data["violations"],
            "violation_rate": round(vrate, 4),
            "mean_latency_ms":round(mlat),
            "entropy_score":  round(data["entropy"], 3),
        })
    topic_summary.sort(key=lambda x: x["violation_rate"], reverse=True)

    # State distribution from timeline
    state_counts = defaultdict(int)
    for _, csi in timeline:
        state_counts[csi_to_state(csi).value] += 1

    return {
        "provider":     "serper_api",
        "sessions":     len(sessions),
        "window_size":  window_size,
        "csi":          components.csi,
        "components": {
            "rf":  components.rf,
            "trf": components.trf,
            "ac":  components.ac,
            "ec":  components.ec,
        },
        "violations":       components.violation_count,
        "violation_rate":   components.violation_rate,
        "mean_latency_ms":  components.mean_latency_ms,
        "mean_confidence":  components.mean_confidence,
        "mean_entropy":     components.mean_entropy,
        "final_state":      fw.state.value,
        "transitions":      len(transitions),
        "transition_log":   [
            {
                "from": t.from_state.value, "to": t.to_state.value,
                "csi": t.csi, "trigger": t.trigger,
            } for t in transitions
        ],
        "rolling_timeline": [
            {"session": i, "csi": c, "state": csi_to_state(c).value}
            for i, c in timeline
        ],
        "state_distribution": dict(state_counts),
        "topic_analysis":   topic_summary,
        "generated_at":     datetime.now(timezone.utc).isoformat(),
    }


# ??? Print report ?????????????????????????????????????????????????????????????

def print_report(result: dict):
    c    = result["components"]
    col  = STATE_COLORS.get(FirewallState(result["final_state"]), "?")

    print(f"\n{'='*62}")
    print(f"  Legible Coordination Stability Index (CSI)")
    print(f"  Provider: {result['provider']}")
    print(f"{'='*62}\n")

    print(f"  CSI = {result['csi']:.4f}   [{col}]  "
          f"{result['final_state'].upper()}\n")

    print(f"  Components:")
    print(f"    RF  (Reliability,  w=0.40) = {c['rf']:.4f}  "
          f"[{result['violations']}/{result['sessions']} violations, "
          f"{result['violation_rate']:.1%}]")
    print(f"    TRF (Tail Risk,    w=0.25) = {c['trf']:.4f}")
    print(f"    AC  (Attribution,  w=0.20) = {c['ac']:.4f}  "
          f"[mean confidence={result['mean_confidence']:.4f}]")
    print(f"    EC  (Entropy,      w=0.15) = {c['ec']:.4f}  "
          f"[mean entropy={result['mean_entropy']:.4f}]")

    print(f"\n  Formula:")
    print(f"    CSI = (0.40 x {c['rf']:.4f}) + (0.25 x {c['trf']:.4f}) + "
          f"(0.20 x {c['ac']:.4f}) + (0.15 x {c['ec']:.4f})")
    print(f"        = {0.4*c['rf']:.4f} + {0.25*c['trf']:.4f} + "
          f"{0.2*c['ac']:.4f} + {0.15*c['ec']:.4f}")
    print(f"        = {result['csi']:.4f}")

    print(f"\n  Interpretation:")
    state_map = {
        "green":  "  Autonomous mode permitted. Full VAR. Standard quorum.",
        "yellow": "  Supervised mode. VAR -20%. Quorum +1. Expanded logging.",
        "orange": "  Throttled. VAR -50%. Dual redundancy. Human approval required.",
        "red":    "  CIRCUIT BREAKER. Execution frozen. Manual override required.",
    }
    print(f"  {state_map.get(result['final_state'], '')}")

    # Rolling timeline summary
    print(f"\n  Rolling CSI Timeline (window={result['window_size']}):")
    timeline = result["rolling_timeline"]
    if timeline:
        # Show key checkpoints
        checkpoints = timeline[::max(1, len(timeline)//12)]
        for p in checkpoints:
            bar   = int(p["csi"] * 20)
            color = STATE_COLORS.get(FirewallState(p["state"]), "?")
            print(f"    s{p['session']:>4}  [{color}]  "
                  f"{'|'*bar}{'.'*(20-bar)}  {p['csi']:.4f}")

    # Transitions
    if result["transition_log"]:
        print(f"\n  State Transitions: {result['transitions']}")
        for t in result["transition_log"]:
            fc = STATE_COLORS.get(FirewallState(t["from"]), "?")
            tc = STATE_COLORS.get(FirewallState(t["to"]), "?")
            print(f"    {fc} -> {tc}  CSI={t['csi']:.4f}  @{t['trigger']}")
    else:
        print(f"\n  State Transitions: 0  (stable throughout)")

    # State distribution
    print(f"\n  State Distribution:")
    dist = result["state_distribution"]
    total = sum(dist.values())
    for state in ["green", "yellow", "orange", "red"]:
        n   = dist.get(state, 0)
        pct = n / total * 100 if total > 0 else 0
        col = STATE_COLORS.get(FirewallState(state), "?")
        print(f"    [{col}]  {n:>3} checkpoints  ({pct:.0f}%)")

    # Top entropy topics
    print(f"\n  Top Topics by Violation Rate:")
    print(f"    {'Topic':<40} {'Viol%':>6}  {'AvgLat':>7}  {'Entropy':>7}")
    print(f"    {'?'*40} {'?'*6}  {'?'*7}  {'?'*7}")
    for t in result["topic_analysis"][:10]:
        if t["total"] < 2:
            continue
        print(f"    {t['topic'][:40]:<40} "
              f"{t['violation_rate']*100:>5.1f}%  "
              f"{t['mean_latency_ms']:>6}ms  "
              f"{t['entropy_score']:>7.3f}")

    print(f"\n{'='*62}")
    print(f"  Sessions: {result['sessions']}  |  "
          f"Window: {result['window_size']}  |  "
          f"CSI: {result['csi']:.4f}")
    print(f"{'='*62}\n")


# ??? Provider comparison scaffold ?????????????????????????????????????????????

def print_comparison_scaffold(results: list[dict]):
    """Print multi-provider CSI comparison table."""
    print(f"\n  Provider CSI Comparison")
    print(f"  {'?'*60}")
    print(f"  {'Provider':<20} {'CSI':>6}  {'State':<8}  "
          f"{'ViolRate':>8}  {'p50lat':>7}  {'AC':>6}")
    print(f"  {'?'*20} {'?'*6}  {'?'*8}  {'?'*8}  {'?'*7}  {'?'*6}")
    for r in sorted(results, key=lambda x: x["csi"], reverse=True):
        col = STATE_COLORS.get(FirewallState(r["final_state"]), "?")
        print(f"  {r['provider']:<20} {r['csi']:>6.4f}  "
              f"[{col}]  "
              f"{r['violation_rate']*100:>7.1f}%  "
              f"{r['mean_latency_ms']:>6.0f}ms  "
              f"{r['components']['ac']:>6.4f}")
    print(f"  {'?'*60}\n")


# ??? Main ?????????????????????????????????????????????????????????????????????

def main():
    parser = argparse.ArgumentParser(description="Legible CSI Analyzer")
    parser.add_argument("--input",  default=None,
                        help="Path to sessions JSONL file")
    parser.add_argument("--window", type=int, default=100,
                        help="Rolling CSI window size (default: 100)")
    parser.add_argument("--output", default=None,
                        help="Save CSI report JSON to this path")
    args = parser.parse_args()

    # Find input file
    if args.input:
        input_path = args.input
    else:
        # Find most recent sessions file
        candidates = sorted(Path("batch_results").glob("sessions_*.jsonl"),
                            reverse=True)
        if not candidates:
            print("\n  No session data found. Run batch_runner first:")
            print("  python examples/batch_runner.py --sessions 20\n")
            sys.exit(1)
        input_path = str(candidates[0])

    print(f"\n  Loading sessions from {input_path}...")
    sessions = load_sessions(input_path)
    print(f"  Loaded {len(sessions)} sessions")

    result = analyze_csi(sessions, window_size=args.window)
    print_report(result)

    # Save report
    out_path = args.output or "batch_results/csi_report.json"
    Path(out_path).parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  Full report saved to {out_path}")

    # Comparison scaffold ? ready for additional providers
    print("\n  Provider comparison (add Together + Massive after next runs):")
    print_comparison_scaffold([result])

    print("  Next: add Together AI + Massive sessions for multi-provider comparison\n")


if __name__ == "__main__":
    main()
