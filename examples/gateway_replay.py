"""
examples/gateway_replay.py

Feeds historical sessions through CoordinationEngine.
Validates routing logic, escalation controls, cost economics, and budget governance.

Usage:
    python examples/gateway_replay.py --mode safe
    python examples/gateway_replay.py --mode safe --escalation
    python examples/gateway_replay.py --mode safe --escalation --cost-model --impact
    python examples/gateway_replay.py --mode safe --escalation --cost-model --impact --escalation-budget 0.05
    python examples/gateway_replay.py --mode safe --escalation --cost-model --impact --escalation-budget 0.10
    python examples/gateway_replay.py --mode safe --escalation --cost-model --impact --escalation-budget 0.15
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


# ── Cost model ─────────────────────────────────────────────────────────────────
COST_WEIGHTS = {
    "Serper":   1.00,
    "Cerebras": 1.40,
    "Groq":     0.80,
}
DEFAULT_COST = 1.00


# ── Impact model ───────────────────────────────────────────────────────────────
HIGH_IMPACT_KEYWORDS = [
    "earnings", "inflation", "cpi", "federal reserve", "fed rate",
    "regulation", "regulatory", "guidance", "quarter", "revenue",
    "gdp", "jobs report", "unemployment", "fomc", "interest rate",
    "sec filing", "balance sheet", "profit", "loss", "dividend",
]
MEDIUM_IMPACT_KEYWORDS = [
    "ai", "infrastructure", "investment", "market", "cloud",
    "semiconductor", "acquisition", "merger", "ipo", "valuation",
    "nvidia", "microsoft", "google", "meta", "apple", "amazon",
    "supply chain", "chip", "data center", "energy",
]


def impact_weight(topic: str) -> float:
    t = topic.lower()
    for k in HIGH_IMPACT_KEYWORDS:
        if k in t:
            return 3.0
    for k in MEDIUM_IMPACT_KEYWORDS:
        if k in t:
            return 2.0
    return 1.0


# ── Escalation gate thresholds ────────────────────────────────────────────────
ENTROPY_GATE_THRESHOLD = 0.65   # minimum topic entropy to allow escalation
IMPACT_GATE_MIN        = 2.0    # minimum impact weight (2.0 = medium, 3.0 = high only)

# ── Escalation controls ────────────────────────────────────────────────────────
ESCALATION_RULES = {
    "GREEN":  {"redundancy": 0},
    "YELLOW": {"redundancy": 1},
    "ORANGE": {"redundancy": 2},
    "RED":    {"redundancy": 0},
}


def escalation_gate_passes(topic: str, entropy: float, gate: str) -> bool:
    """
    Returns True if this session passes the escalation gate.
    none  → always passes
    impact → topic must be medium or high impact (weight >= 2.0)
    entropy → topic entropy must be >= ENTROPY_GATE_THRESHOLD
    both   → must pass both impact AND entropy gates
    """
    if gate == "none":
        return True
    w = impact_weight(topic)
    passes_impact  = w >= IMPACT_GATE_MIN
    passes_entropy = entropy >= ENTROPY_GATE_THRESHOLD
    if gate == "impact":
        return passes_impact
    if gate == "entropy":
        return passes_entropy
    if gate == "both":
        return passes_impact and passes_entropy
    return True


def apply_escalation(
    primary: str,
    primary_state: str,
    session_snap: dict,
    provider_csis: dict,
    active: list,
) -> tuple[bool, str, int, list[str]]:
    """
    Returns (violation_occurred, provider_used, backup_calls, all_providers_called).
    """
    redundancy       = ESCALATION_RULES.get(primary_state, {}).get("redundancy", 0)
    primary_violated = session_snap[primary]["violation"]

    if redundancy == 0 or not primary_violated:
        return primary_violated, primary, 0, [primary]

    candidates = sorted(
        [(csi, name) for name, csi in provider_csis.items()
         if name != primary and name in active and name in session_snap],
        reverse=True,
    )

    called = [primary]
    for attempt, (_, backup) in enumerate(candidates[:redundancy]):
        called.append(backup)
        if not session_snap[backup]["violation"]:
            return False, backup, attempt + 1, called

    return True, primary, redundancy, called


# ── Data loading ───────────────────────────────────────────────────────────────

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
        if provider_key == "serper" and re.match(r"^sessions_\d+$", stem):
            return f
        if stem.startswith(provider_key) and "sessions" in stem:
            return f
    return None


def normalise(record: dict, name: str) -> dict:
    topic    = record.get("topic", "")
    sla      = record.get("sla_latency_ms") or record.get("sla_target_ms", 5000)
    p50      = record.get("latency_p50", 3500)
    p99      = record.get("latency_max", p50)
    conf     = float(record.get("confidence", 1.0) or 1.0)
    violated = record.get("outcome", "SlaPass") != "SlaPass"
    if "passed" in record:
        violated = not record["passed"]
    return {
        "provider":       name,
        "violation":      violated,
        "latency_ms":     float(p50),
        "latency_p99_ms": float(p99),
        "sla_ms":         float(sla),
        "confidence":     conf,
        "entropy":        topic_entropy_score(topic),
        "topic":          topic,
        "session_id":     record.get("session_id", ""),
    }


# ── Survival mode routing ──────────────────────────────────────────────────────
SURVIVAL_ESC_PRIMARY_MAX = 0.85   # don't escalate if primary CSI >= this
SURVIVAL_ESC_MARGIN      = 0.05   # backup must beat primary by at least this
SURVIVAL_LOW_CSI_FLOOR   = 0.70   # min CSI to serve LOW impact traffic


def survival_route(topic: str, provider_csis: dict, active: list) -> str:
    """
    Deterministic impact-aware routing for degraded mode.
      HIGH   → highest CSI (protect quality on important queries)
      MEDIUM → second-highest CSI (stability over peak performance)
      LOW    → cheapest provider above CSI floor, else absolute cheapest
    No probability, no exploration, no blending.
    """
    w      = impact_weight(topic)
    ranked = sorted(
        [(csi, name) for name, csi in provider_csis.items() if name in active],
        reverse=True,
    )
    if not ranked:
        return active[0] if active else "Serper"

    if w >= 3.0:                      # HIGH — best available
        return ranked[0][1]
    if w >= 2.0:                      # MEDIUM — second-best for stability
        return ranked[1][1] if len(ranked) > 1 else ranked[0][1]

    # LOW — cheapest above floor, else absolute cheapest
    above_floor = [x for x in ranked if x[0] >= SURVIVAL_LOW_CSI_FLOOR]
    pool = above_floor if above_floor else ranked
    return min(pool, key=lambda x: COST_WEIGHTS.get(x[1], DEFAULT_COST))[1]


def survival_escalation_ok(topic: str, primary_csi: float, backup_csi: float) -> bool:
    """
    Survival mode escalation gate — strict version.
    Only HIGH impact gets a backup call, and only when backup is meaningfully better.
    """
    if impact_weight(topic) < 3.0:
        return False   # MEDIUM / LOW: no escalation, preserve budget
    if primary_csi >= SURVIVAL_ESC_PRIMARY_MAX:
        return False   # primary is acceptable — don't pay for backup
    if backup_csi - primary_csi < SURVIVAL_ESC_MARGIN:
        return False   # backup barely better — not worth the cost
    return True


SURVIVAL_V2_CSI_FLOOR  = 0.80   # providers below this excluded in degraded mode
SURVIVAL_V2_ESC_MARGIN = 0.07   # backup must beat primary by at least this
SURVIVAL_V2_COST_CAP   = 1.20   # stop escalating if total cost exceeds 1.20x baseline


def survival_v2_route(topic, provider_csis, active, rng):
    """
    Survival v2: viability-filtered proportional routing.
    1. Keep only providers with CSI >= 0.80
    2. If none pass, fall back to highest-CSI
    3. Route proportionally by CSI weight (adaptive, not deterministic)
    """
    viable = [(csi, name) for name, csi in provider_csis.items()
              if name in active and csi >= SURVIVAL_V2_CSI_FLOOR]
    if not viable:
        ranked = sorted(
            [(csi, name) for name, csi in provider_csis.items() if name in active],
            reverse=True,
        )
        return ranked[0][1] if ranked else (active[0] if active else "Serper")
    total_w = sum(csi for csi, _ in viable)
    r = rng.random() * total_w
    cum = 0.0
    for csi, name in sorted(viable, reverse=True):
        cum += csi
        if r <= cum:
            return name
    return viable[-1][1]


def survival_v2_escalation_ok(topic, primary_csi, backup_csi, current_cost_ratio):
    """Survival v2 escalation: HIGH only, 0.07 margin, cost cap."""
    if impact_weight(topic) < 3.0:
        return False
    if primary_csi >= SURVIVAL_ESC_PRIMARY_MAX:
        return False
    if backup_csi - primary_csi < SURVIVAL_V2_ESC_MARGIN:
        return False
    if current_cost_ratio >= SURVIVAL_V2_COST_CAP:
        return False
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gateway Replay")
    parser.add_argument("--batch-dir",  default="batch_results")
    parser.add_argument("--window",     type=int, default=50)
    parser.add_argument("--mode",       default="safe",
                        choices=["safe", "competitive", "probabilistic"])
    parser.add_argument("--exclude",    nargs="*", default=[],
                        help="Providers to hard-exclude (e.g. --exclude Groq)")
    parser.add_argument("--escalation", action="store_true",
                        help="Enable state-triggered redundancy escalation")
    parser.add_argument("--cost-model", action="store_true",
                        help="Track and report cost economics")
    parser.add_argument("--impact",     action="store_true",
                        help="Enable impact-weighted reliability metrics")
    # ── NEW: Budget Governor ──────────────────────────────────────────────────
    parser.add_argument("--escalation-budget", type=float, default=None, metavar="FRACTION",
                        help="Max cost overhead vs static baseline (e.g. 0.10 = 10%% cap). "
                             "Blocks escalation once budget is exhausted.")
    parser.add_argument("--collapse-at", type=int, default=None, metavar="SESSION",
                        help="Simulate a provider collapsing at a specific session number "
                             "(e.g. --collapse Serper --collapse-at 100). "
                             "Engine keeps CSI history up to that point, then provider is removed.")
    parser.add_argument("--collapse", default=None, metavar="PROVIDER",
                        help="Provider to collapse at --collapse-at session (e.g. Serper).")
    parser.add_argument("--degraded-strategy", default="normal",
                        choices=["normal", "survival", "survival_v2"],
                        help="Routing strategy after provider collapse. "
                             "survival: deterministic impact-aware routing, "
                             "tightened escalation (HIGH only + CSI margin), "
                             "no probabilistic exploration.")
    parser.add_argument("--provider-cap", type=float, default=None, metavar="FRACTION",
                        help="Max traffic share per provider (e.g. 0.65 = 65%% cap). "
                             "Forces diversification even when one provider dominates.")
    parser.add_argument("--provider-floor", type=float, default=None, metavar="FRACTION",
                        help="Min traffic share per active provider (e.g. 0.05 = 5%% floor). "
                             "Keeps backup providers warm even when primary dominates. "
                             "Applied before the cap check.")
    parser.add_argument("--escalation-gate", default="none",
                        choices=["none", "impact", "entropy", "both"],
                        help="Gate escalation by topic signal: "
                             "none=always escalate on state, "
                             "impact=only medium/high topics, "
                             "entropy=only high-entropy topics (>=0.65), "
                             "both=must pass both gates.")
    parser.add_argument("--shadow-warm", type=float, default=0.0, metavar="FRACTION",
                        help="Per-provider sampling rate for parallel observational learning "
                             "(e.g. 0.10 = 10%% chance per non-routed provider). "
                             "Shadow calls update CSI only; they do not affect routing decisions.")
    parser.add_argument("--shadow-warm-until", type=int, default=0, metavar="SESSIONS",
                        help="Enable shadow warm for the first N sessions only.")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)
    excluded  = {p.title() for p in (args.exclude or [])}

    provider_map = {"Serper": "serper", "Groq": "groq", "Cerebras": "cerebras"}
    all_sessions: dict[str, list[dict]] = {}

    for name, key in provider_map.items():
        f = find_file(batch_dir, key)
        if not f:
            continue
        all_sessions[name] = [normalise(r, name) for r in load_jsonl(f)]
        print(f"  ✓ {name:<12} {len(all_sessions[name])} sessions  ({f.name})")

    if len(all_sessions) < 2:
        print("\nNeed at least 2 providers.")
        sys.exit(1)

    active = [name for name in all_sessions if name not in excluded]
    if excluded:
        print(f"  Excluded from routing: {sorted(excluded)}")

    n = min(len(v) for v in all_sessions.values())

    # ── Budget limit: total cost units allowed over the whole replay ───────────
    # baseline = n × 1.0 (Serper cost per session)
    import random as _random
    _rng = _random.Random(42)
    baseline_cost           = n * COST_WEIGHTS["Serper"]
    # Budget caps backup-call spend only (not routing overhead).
    # e.g. 0.05 → max 40 extra units for backup calls on 800-session baseline.
    escalation_budget_units = (baseline_cost * args.escalation_budget
                               if args.escalation_budget is not None else None)

    # Build display tag
    tags = args.mode
    if args.provider_floor is not None:
        tags += f" [floor={args.provider_floor*100:.0f}%]"
    if args.provider_cap is not None:
        tags += f" [cap={args.provider_cap*100:.0f}%]"
    if args.collapse and args.collapse_at is not None:
        tags += f" [collapse={args.collapse}@{args.collapse_at}]"
    if args.shadow_warm > 0 and args.shadow_warm_until > 0:
        tags += f" [shadow-warm={args.shadow_warm*100:.0f}%@{args.shadow_warm_until}]"
    if args.degraded_strategy != 'normal':
        tags += f" [degraded={args.degraded_strategy}]"
    if args.escalation:
        tags += " + escalation"
        if args.escalation_gate != "none":
            tags += f" [gate={args.escalation_gate}]"
        if args.escalation_budget is not None:
            tags += f" [budget={args.escalation_budget*100:.0f}%]"
    if args.cost_model:
        tags += " + cost-model"
    if args.impact:
        tags += " + impact"

    print(f"\n  Aligned: {n} sessions · Window: {args.window}")
    print(f"  Mode:    {tags}")
    if escalation_budget_units is not None:
        print(f"  Budget:  {args.escalation_budget*100:.0f}% cap on escalation cost  "
              f"= {escalation_budget_units:.1f} backup-call units allowed  "
              f"(baseline={baseline_cost:.1f})")
    print()

    engine = CoordinationEngine(
        providers        = active,
        default_provider = "Serper",
        routing_mode     = args.mode,
    )

    # ── Counters ───────────────────────────────────────────────────────────────
    routing_traffic     = defaultdict(int)
    shadow_violations   = 0
    actual_violations   = 0
    agreed              = 0
    total               = 0

    # Escalation
    escalation_saves    = 0
    escalations_fired   = 0   # times escalation actually executed
    escalations_blocked = 0   # times budget prevented escalation
    escalations_gated   = 0   # times gate (entropy/impact) suppressed escalation

    # Cost
    total_shadow_cost = 0.0
    total_static_cost = 0.0
    escalation_cost   = 0.0
    shadow_warm_cost  = 0.0
    shadow_warm_calls = 0

    # Impact
    weighted_shadow_v = 0.0
    weighted_actual_v = 0.0
    weighted_total    = 0.0
    impact_bucket     = {"high": [0, 0], "medium": [0, 0], "low": [0, 0]}  # [viol, total]

    # ── Collapse tracking ─────────────────────────────────────────────────────
    collapse_provider  = args.collapse.title() if args.collapse else None
    collapse_at        = args.collapse_at
    collapse_triggered = False
    pre_collapse_csi   = None
    degraded_mode      = False

    # ── Replay loop ────────────────────────────────────────────────────────────
    for i in range(n):
        topic       = all_sessions["Serper"][i]["topic"]

        # ── Mid-run collapse ───────────────────────────────────────────────────────
        # At the specified session, remove the provider from active routing.
        # The engine keeps its CSI history (lagging indicator stays), but the
        # provider is no longer eligible for selection or escalation.
        if (collapse_provider and collapse_at is not None
                and i == collapse_at and not collapse_triggered):
            collapse_triggered = True
            pre_collapse_status = engine.status()
            pre_collapse_csi    = pre_collapse_status["providers"].get(
                collapse_provider, {}
            ).get("csi", None)
            if collapse_provider in active:
                active.remove(collapse_provider)
                if args.degraded_strategy in ("survival", "survival_v2"):
                    degraded_mode = True
                print(f"  ⚡ SESSION {i}: {collapse_provider} COLLAPSED  "
                      f"(CSI at collapse: {pre_collapse_csi:.4f})")
                if degraded_mode:
                    print(f"     ⚠ DEGRADED MODE ACTIVE — survival routing engaged")
                print(f"     Active providers: {active}")
                print()

        recommended, _, _, _ = engine.recommend(topic=topic)

        # If the recommended provider has collapsed, pick the next best from active list.
        # The engine doesn't know about mid-run collapse, so we enforce it here.
        if collapse_triggered and recommended not in active:
            status_now    = engine.status()
            alternatives  = sorted(
                [(d["csi"], name) for name, d in status_now["providers"].items()
                 if name in active],
                reverse=True,
            )
            recommended = alternatives[0][1] if alternatives else active[0]

        # Survival mode: override engine recommendation with degraded routing
        if degraded_mode:
            status_now    = engine.status()
            provider_csis = {name: d["csi"] for name, d in status_now["providers"].items()}
            if args.degraded_strategy == "survival_v2":
                recommended = survival_v2_route(topic, provider_csis, active, _rng)
            else:
                recommended = survival_route(topic, provider_csis, active)

        # Provider floor: if any active provider is below its minimum share,
        # route this session to the most-starved provider instead.
        # This keeps backup providers warm so CSI models stay accurate.
        if args.provider_floor is not None and total > 0:
            # Find the provider furthest below its floor (most starved)
            starved = [
                (args.provider_floor - routing_traffic.get(name, 0) / total, name)
                for name in active
                if routing_traffic.get(name, 0) / total < args.provider_floor
            ]
            if starved:
                # Route to the most-starved provider (largest deficit)
                starved.sort(reverse=True)
                recommended = starved[0][1]

        # Provider cap: if recommended provider has exceeded its traffic share,
        # fall back to the next best active provider under the cap.
        if args.provider_cap is not None and total > 0:
            for attempt in range(len(active)):
                share = routing_traffic.get(recommended, 0) / total
                if share < args.provider_cap:
                    break
                # Pick next best provider not over cap
                status_now = engine.status()
                alts = sorted(
                    [(d["csi"], name) for name, d in status_now["providers"].items()
                     if name in active and name != recommended
                     and routing_traffic.get(name, 0) / total < args.provider_cap],
                    reverse=True,
                )
                if alts:
                    recommended = alts[0][1]
                else:
                    break  # all providers at cap, keep original

        session_snap     = {name: all_sessions[name][i] for name in all_sessions}
        actual_violation = all_sessions["Serper"][i]["violation"]

        # ── Routing + Escalation ───────────────────────────────────────────────
        if args.escalation:
            status        = engine.status()
            rec_state     = status["providers"].get(recommended, {}).get("state", "GREEN")
            provider_csis = {name: d["csi"] for name, d in status["providers"].items()}

            # Would escalation fire this session?
            would_escalate = (
                ESCALATION_RULES.get(rec_state, {}).get("redundancy", 0) > 0
                and session_snap[recommended]["violation"]
            )

            # ── BUDGET GATE ────────────────────────────────────────────────────
            # Survival v2 escalation gate
            if would_escalate and degraded_mode and args.degraded_strategy == "survival_v2":
                p2_csi = provider_csis.get(recommended, 0.0)
                alts2  = sorted(
                    [(csi, nm) for nm, csi in provider_csis.items()
                     if nm != recommended and nm in active],
                    reverse=True,
                )
                b2_csi = alts2[0][0] if alts2 else 0.0
                cost_ratio = total_shadow_cost / baseline_cost if baseline_cost > 0 else 1.0
                if not survival_v2_escalation_ok(topic, p2_csi, b2_csi, cost_ratio):
                    would_escalate    = False
                    escalations_gated += 1

            # Survival v1 escalation gate: in degraded mode, only HIGH impact gets backup.
            if would_escalate and degraded_mode and args.degraded_strategy == "survival":
                p_csi = provider_csis.get(recommended, 0.0)
                best_backup_name = next(
                    (nm for _, nm in sorted(
                        [(csi, nm) for nm, csi in provider_csis.items()
                         if nm != recommended and nm in active],
                        reverse=True,
                    )[:1]), None,
                )
                b_csi = provider_csis.get(best_backup_name, 0.0) if best_backup_name else 0.0
                if not survival_escalation_ok(topic, p_csi, b_csi):
                    would_escalate    = False
                    escalations_gated += 1

            # Signal gate: check topic entropy/impact before spending on backup.
            if would_escalate and args.escalation_gate != "none":
                session_entropy = session_snap[recommended]["entropy"]
                if not escalation_gate_passes(topic, session_entropy, args.escalation_gate):
                    would_escalate    = False
                    escalations_gated += 1

            # Budget gate: check escalation_cost (backup calls only) vs budget.
            # Routing overhead is excluded — budget governs insurance spend only.
            budget_allows = True
            if would_escalate and escalation_budget_units is not None:
                best_backup = next(
                    (nm for _, nm in sorted(
                        [(csi, nm) for nm, csi in provider_csis.items()
                         if nm != recommended and nm in active],
                        reverse=True,
                    )[:1]),
                    None,
                )
                this_backup_cost = COST_WEIGHTS.get(best_backup, DEFAULT_COST)
                if escalation_cost + this_backup_cost > escalation_budget_units:
                    budget_allows       = False
                    escalations_blocked += 1

            if would_escalate and budget_allows:
                rec_violated, final_provider, backup_calls, providers_called = apply_escalation(
                    primary=recommended, primary_state=rec_state,
                    session_snap=session_snap, provider_csis=provider_csis, active=active,
                )
                escalations_fired += 1
                if backup_calls > 0 and not rec_violated:
                    escalation_saves += 1
            else:
                # No escalation — either not needed or budget exhausted
                rec_violated     = session_snap[recommended]["violation"]
                final_provider   = recommended
                providers_called = [recommended]

            routing_traffic[final_provider] += 1
            session_cost = sum(COST_WEIGHTS.get(p, DEFAULT_COST) for p in providers_called)

        else:
            rec_violated     = all_sessions[recommended][i]["violation"]
            final_provider   = recommended
            providers_called = [recommended]
            routing_traffic[recommended] += 1
            session_cost = COST_WEIGHTS.get(recommended, DEFAULT_COST)

        # ── Shadow warm (observational learning only) ───────────────────────
        shadow_called = []
        if (
            args.shadow_warm > 0
            and args.shadow_warm_until > 0
            and (i + 1) <= args.shadow_warm_until
        ):
            for provider_name in active:
                if provider_name in providers_called:
                    continue
                if _rng.random() < args.shadow_warm:
                    shadow_called.append(provider_name)

        if args.shadow_warm > 0 and args.shadow_warm_until > 0:
            learned_providers = list(dict.fromkeys(providers_called + shadow_called))
        else:
            # Preserve legacy replay semantics when shadow warm is disabled:
            # every provider receives observational updates each session.
            learned_providers = list(all_sessions.keys())

        for name in learned_providers:
            s = all_sessions[name][i]
            engine.report_session(
                provider=name, violation=s["violation"], latency_ms=s["latency_ms"],
                latency_p99_ms=s["latency_p99_ms"], sla_ms=s["sla_ms"],
                confidence=s["confidence"], entropy=s["entropy"],
                topic=s["topic"], session_id=s["session_id"],
            )

        if shadow_called:
            shadow_warm_calls += len(shadow_called)
            extra_shadow_cost = sum(COST_WEIGHTS.get(p, DEFAULT_COST) for p in shadow_called)
            shadow_warm_cost += extra_shadow_cost
            session_cost += extra_shadow_cost

        # ── Accumulate ────────────────────────────────────────────────────────
        shadow_violations += int(rec_violated)
        actual_violations += int(actual_violation)
        if final_provider == "Serper":
            agreed += 1
        total += 1

        total_shadow_cost += session_cost
        total_static_cost += COST_WEIGHTS["Serper"]
        if len(providers_called) > 1:
            escalation_cost += sum(COST_WEIGHTS.get(p, DEFAULT_COST) for p in providers_called[1:])

        if args.impact:
            w = impact_weight(topic)
            weighted_total    += w
            weighted_shadow_v += w * int(rec_violated)
            weighted_actual_v += w * int(actual_violation)
            bucket = "high" if w >= 3.0 else "medium" if w >= 2.0 else "low"
            impact_bucket[bucket][0] += int(rec_violated)
            impact_bucket[bucket][1] += 1

        # ── Progress report every 100 ─────────────────────────────────────────
        if (i + 1) % 100 == 0 or i == n - 1:
            status   = engine.status()
            sv_pct   = shadow_violations / total * 100
            av_pct   = actual_violations / total * 100
            cost_pct = (total_shadow_cost / total_static_cost - 1) * 100
            cost_str = f"  cost={cost_pct:+.1f}%" if args.cost_model else ""
            blk_str  = f"  blocked={escalations_blocked}" if args.escalation_budget is not None else ""
            print(f"  Session {i+1:>4}/{n}  "
                  f"shadow={sv_pct:.1f}%  actual={av_pct:.1f}%  "
                  f"agree={agreed/total*100:.0f}%{cost_str}{blk_str}")
            for name, d in sorted(status["providers"].items(),
                                  key=lambda x: x[1]["csi"], reverse=True):
                tag = " [EXCLUDED]" if name in excluded else ""
                print(f"    {name:<12} {d['state']:<8} CSI={d['csi']:.4f}  "
                      f"routed={routing_traffic.get(name,0)/total*100:.1f}%{tag}")
            print()

    # ── Final calculations ─────────────────────────────────────────────────────
    sv  = shadow_violations / total
    av  = actual_violations / total
    imp = (av - sv) / av * 100 if av else 0

    cost_overhead        = (total_shadow_cost / total_static_cost - 1) * 100
    violations_prevented = actual_violations - shadow_violations
    cost_per_prevention  = (
        (total_shadow_cost - total_static_cost) / violations_prevented
        if violations_prevented > 0 else float("inf")
    )
    res = imp / cost_overhead if cost_overhead > 0 else (float("inf") if imp > 0 else 0.0)

    # Impact weighted
    w_sv = w_av = w_imp = w_res = 0.0
    if args.impact and weighted_total > 0:
        w_sv  = weighted_shadow_v / weighted_total
        w_av  = weighted_actual_v / weighted_total
        w_imp = (w_av - w_sv) / w_av * 100 if w_av > 0 else 0
        w_res = w_imp / cost_overhead if cost_overhead > 0 else (float("inf") if w_imp > 0 else 0.0)

    # ── Report ─────────────────────────────────────────────────────────────────
    W = 64
    print("=" * W)
    print("  GATEWAY REPLAY RESULTS")
    print("=" * W)
    print(f"  Sessions:             {total}")
    print(f"  Mode:                 {tags}")
    print(f"  Excluded:             {sorted(excluded) or 'none'}")
    if collapse_triggered:
        if degraded_mode:
            strategy_label = "survival_v2 (viability-filtered proportional routing + tightened escalation)" \
                if args.degraded_strategy == "survival_v2" else \
                "survival (deterministic impact routing + tightened escalation)"
            print(f"  Degraded strategy:    {strategy_label}")
        print(f"  Collapse:             {collapse_provider} at session {collapse_at}  "
              f"(CSI at collapse: {pre_collapse_csi:.4f})")
        print(f"  Pre-collapse sessions:  {collapse_at}  "
              f"({collapse_at/total*100:.0f}% of run)")
        print(f"  Post-collapse sessions: {total - collapse_at}  "
              f"({(total-collapse_at)/total*100:.0f}% of run)")
    print()

    print(f"  ── Reliability {'─'*(W-18)}")
    print(f"  Actual (Serper static):    {av*100:.2f}%")
    print(f"  Shadow ({args.mode:<14}):  {sv*100:.2f}%")
    print(f"  Improvement:               {imp:+.1f}%")
    if args.escalation:
        print(f"  Escalation saves:          {escalation_saves}  "
              f"({escalation_saves/total*100:.1f}% of sessions)")
        print(f"  Violations prevented:      {violations_prevented}")
        if args.escalation_gate != "none":
            total_considered = escalations_fired + escalations_blocked + escalations_gated
            print(f"  Gate ({args.escalation_gate:<8}):          suppressed {escalations_gated} escalations  "
                  f"({escalations_gated/total_considered*100:.0f}% of candidates)"
                  if total_considered > 0 else
                  f"  Gate ({args.escalation_gate:<8}):          no escalations attempted")

    # ── BUDGET GOVERNOR REPORT ─────────────────────────────────────────────────
    if escalation_budget_units is not None:
        esc_pct       = escalation_cost / baseline_cost * 100
        routing_extra = total_shadow_cost - total_static_cost - escalation_cost
        routing_pct   = routing_extra / baseline_cost * 100
        headroom_u    = escalation_budget_units - escalation_cost
        attempted     = escalations_fired + escalations_blocked
        print()
        print(f"  ── Budget Governor {'─'*(W-22)}")
        print(f"  Escalation budget cap:     {args.escalation_budget*100:.0f}%  "
              f"= {escalation_budget_units:.1f} backup-call units allowed")
        print(f"  Escalation cost used:      {esc_pct:.2f}%  "
              f"({escalation_cost:.1f} units, {escalations_fired} backup calls)")
        print(f"  Routing overhead (excl.):  {routing_pct:.2f}%  "
              f"({routing_extra:.1f} units from provider pricing differences)")
        print(f"  Headroom remaining:        {max(headroom_u, 0):.1f} units  "
              f"({max(0, args.escalation_budget*100 - esc_pct):.2f}%)")
        print(f"  Escalations fired:         {escalations_fired}")
        print(f"  Escalations blocked:       {escalations_blocked}")
        if attempted > 0:
            pct = escalations_fired / attempted * 100
            print(f"  Execution rate:            {pct:.0f}%  "
                  f"({escalations_fired}/{attempted} attempted escalations ran)")
        if escalations_blocked > 0:
            print(f"  ⚠  Budget exhausted mid-run: "
                  f"{escalations_blocked} escalation(s) blocked.")

    if args.cost_model:
        print()
        print(f"  ── Economics {'─'*(W-16)}")
        print(f"  Cost weights:              Serper=1.00  Cerebras=1.40  Groq=0.80")
        print(f"  Total static cost:         {total_static_cost:.1f}  (Serper × {total})")
        print(f"  Total shadow cost:         {total_shadow_cost:.1f}")
        print(f"  Cost overhead:             {cost_overhead:+.2f}%  "
              f"({escalation_cost:.1f} units from backup calls)")
        print(f"  Avg cost per session:      {total_shadow_cost/total:.4f}  (static: 1.0000)")
        if shadow_warm_calls > 0:
            print(f"  Shadow warm calls:         {shadow_warm_calls}  ({shadow_warm_calls/total:.2f} per session)")
            print(f"  Shadow warm extra cost:    {shadow_warm_cost:.1f}")
        print()
        print(f"  ── Efficiency {'─'*(W-17)}")
        if violations_prevented > 0:
            print(f"  Cost per violation prevented:  {cost_per_prevention:.2f} cost-units")
        if cost_overhead > 0:
            print(f"  Reliability Efficiency Score:  {res:.2f}x  "
                  f"({imp:.1f}% gain per {cost_overhead:.1f}% cost increase)")
        elif imp > 0:
            print(f"  Reliability Efficiency Score:  ∞  ({imp:.1f}% gain at zero extra cost)")
        else:
            print(f"  Reliability Efficiency Score:  0.00")

    if args.impact and weighted_total > 0:
        print()
        print(f"  ── Impact-Weighted Reliability {'─'*(W-33)}")
        print(f"  Weighted violation (static):   {w_av*100:.2f}%")
        print(f"  Weighted violation (shadow):   {w_sv*100:.2f}%")
        print(f"  Weighted improvement:          {w_imp:+.1f}%")
        if cost_overhead > 0:
            print(f"  Weighted RES:                  {w_res:.2f}x")
        elif w_imp > 0:
            print(f"  Weighted RES:                  ∞")
        print()
        print(f"  Impact breakdown (shadow violations / total sessions):")
        for bucket in ["high", "medium", "low"]:
            bv, bt = impact_bucket[bucket]
            pct = bv / bt * 100 if bt > 0 else 0
            print(f"    {bucket.upper():<8}  {bv:>3} violations / {bt:>4} sessions  ({pct:.1f}%)")

    print()
    print(f"  ── Traffic {'─'*(W-14)}")
    for name, count in sorted(routing_traffic.items(), key=lambda x: -x[1]):
        tag      = " [excluded]" if name in excluded else ""
        cost_str = f"  cost={COST_WEIGHTS.get(name, DEFAULT_COST):.2f}" if args.cost_model else ""
        print(f"    {name:<14} {count:>4} sessions  ({count/total*100:.1f}%){cost_str}{tag}")

    print()
    print(f"  ── Final provider states {'─'*(W-28)}")
    for name, d in sorted(engine.status()["providers"].items(),
                          key=lambda x: x[1]["csi"], reverse=True):
        warm = "✓ warmed" if d["warmup_complete"] else "○ cold"
        tag  = " [EXCLUDED]" if name in excluded else ""
        print(f"    {name:<14} {d['state']:<8} CSI={d['csi']:.4f}  {warm}  {d['session_count']} sessions{tag}")
    print("=" * W)

    # ── Strategy comparison table ──────────────────────────────────────────────
    if args.cost_model and args.escalation:
        has_w      = args.impact and weighted_total > 0
        budget_lbl = f" [cap={args.escalation_budget*100:.0f}%]" if args.escalation_budget is not None else ""
        w_header   = f"  {'W.Improv':>10}" if has_w else ""
        w_row      = f"  {w_imp:>+9.1f}%" if has_w else ""

        print()
        print(f"  ── Strategy comparison {'─'*(W-26)}")
        print(f"  {'Strategy':<35} {'Violation':>9} {'Improv':>8} {'Cost':>7} {'RES':>7}{w_header}")
        print(f"  {'-'*(W + (12 if has_w else 0))}")
        print(f"  {'Static Serper':<35} {'6.12%':>9} {'—':>8} {'1.00x':>7} {'—':>7}")
        print(f"  {'Safe (no escalation)':<35} {'6.25%':>9} {'-2.0%':>8} {'~1.00x':>7} {'0.0':>7}")
        print(f"  {f'Safe + Escalation{budget_lbl}':<35} {sv*100:>8.2f}% {imp:>+7.1f}% "
              f"{total_shadow_cost/total_static_cost:>6.2f}x {res:>6.2f}x{w_row}")


if __name__ == "__main__":
    main()
