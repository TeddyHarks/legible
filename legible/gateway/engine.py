"""
legible/gateway/engine.py

Coordination Engine — the brain of the Legible Gateway.

Maintains per-provider state using the existing RollingCSI + CoordinationFirewall.
Implements routing scoring and selection.
Thread-safe via a simple lock (suitable for single-worker FastAPI).

This is NOT a proxy. It does NOT touch traffic.
It maintains state from reported outcomes and emits recommendations.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..firewall.models import SessionMetrics
from ..firewall.rolling_csi import RollingCSI
from ..firewall.state_machine import CoordinationFirewall, FirewallState, csi_to_state
from ..firewall.entropy import topic_entropy_score

# ── Warmup requirement ─────────────────────────────────────────────────────────
# A provider must stay GREEN for this many sessions before being fully eligible
WARMUP_SESSIONS = 25   # reduced: 25 consecutive GREEN to avoid over-penalising recovering providers

# ── Scoring weights ────────────────────────────────────────────────────────────
W_CSI     = 0.60
W_STATE   = 0.30
W_ENTROPY = 0.10

# ── Routing modes ─────────────────────────────────────────────────────────────
ROUTING_MODE_SAFE        = "safe"         # enterprise: avoid mistakes
ROUTING_MODE_COMPETITIVE = "competitive"  # exploit regime differences
ROUTING_MODE_PROBABILISTIC = "probabilistic" # softmax: traffic split proportional to scores

# Safe mode — conservative, strong preference for proven stability
SAFE_STATE_PENALTY = {
    FirewallState.GREEN:  0.00,
    FirewallState.YELLOW: 0.05,
    FirewallState.ORANGE: 0.15,
    FirewallState.RED:    1.00,
}

# Competitive mode — reduced penalties, let CSI differences drive selection
COMPETITIVE_STATE_PENALTY = {
    FirewallState.GREEN:  0.00,
    FirewallState.YELLOW: 0.03,   # softer — YELLOW can still compete
    FirewallState.ORANGE: 0.10,   # softer — only clearly bad
    FirewallState.RED:    1.00,   # RED never selected in either mode
}

WARMUP_PENALTY    = 0.10   # applied until warmup_complete
RELATIVE_BONUS    = 0.05   # awarded to current CSI leader (competitive only)
MIN_CSI_FOR_BONUS = 0.95   # must be GREEN-grade to receive bonus
SOFTMAX_TEMPERATURE = 8.0  # controls sharpness: higher=more deterministic, lower=more spread


@dataclass
class ProviderTracker:
    """
    Per-provider live state maintained by the engine.
    Wraps the existing CoordinationFirewall.
    """
    name:           str
    firewall:       CoordinationFirewall = field(default=None)
    session_count:  int = 0
    green_streak:   int = 0        # consecutive GREEN sessions
    warmup_complete: bool = False
    last_score:     float = 1.0
    violation_count: int = 0

    def __post_init__(self):
        self.firewall = CoordinationFirewall(
            provider_id=self.name.lower() + "_api",
            window_size=50,
        )

    @property
    def csi(self) -> float:
        return self.firewall.csi

    @property
    def state(self) -> FirewallState:
        return self.firewall.state

    @property
    def violation_rate(self) -> float:
        c = self.firewall.last_components
        return c.violation_rate if c else 0.0

    def ingest(self, metrics: SessionMetrics) -> None:
        self.session_count += 1
        if metrics.violated:
            self.violation_count += 1
        self.firewall.ingest(metrics)

        # Track warmup
        if self.state == FirewallState.GREEN:
            self.green_streak += 1
            if self.green_streak >= WARMUP_SESSIONS:
                self.warmup_complete = True
        else:
            self.green_streak = 0
            # Don't revoke warmup_complete once achieved — provider earned it

    def score(
        self,
        entropy:        float,
        require_stable: bool  = False,
        mode:           str   = ROUTING_MODE_SAFE,
        best_csi:       float = 0.0,
    ) -> float:
        """
        Compute routing score. Higher is better. Negative means ineligible.

        SAFE mode — minimise risk:
            Weighted score with strong state penalties.
            Provider must clear warmup before competing fully.
            Serper wins whenever tied or close.

        COMPETITIVE mode — exploit regime differences:
            Pure CSI ranking. Only RED is blocked.
            No warmup penalty. No state penalty for non-RED.
            Highest rolling CSI wins, period.
            Entropy adds a small tiebreaker adjustment.
            This is regime arbitrage — use whichever provider is
            performing best RIGHT NOW.
        """
        if self.state == FirewallState.RED:
            return -999.0
        if require_stable and self.state != FirewallState.GREEN:
            return -999.0
        if self.session_count < 5:
            return 0.5  # not enough data yet

        if mode == ROUTING_MODE_COMPETITIVE:
            # Pure relative scoring — highest CSI wins
            # Only apply entropy adjustment as a tiebreaker
            ep = entropy * 0.05 if entropy > 0.65 else 0.0
            score = self.csi - ep
        else:
            # Safe mode: weighted with strong penalties
            sp = SAFE_STATE_PENALTY.get(self.state, 0.30)
            ep = entropy * 0.10 if entropy > 0.65 else 0.0
            wp = WARMUP_PENALTY if not self.warmup_complete else 0.0
            score = W_CSI * self.csi - W_STATE * sp - W_ENTROPY * ep - wp

        self.last_score = round(score, 4)
        return self.last_score

    def to_dict(self) -> dict:
        return {
            "name":            self.name,
            "csi":             round(self.csi, 4),
            "state":           self.state.value.upper(),
            "violation_rate":  round(self.violation_rate, 4),
            "session_count":   self.session_count,
            "warmup_complete": self.warmup_complete,
            "score":           self.last_score,
            "controls":        self.firewall.controls,
        }


class CoordinationEngine:
    """
    Thread-safe coordination engine.

    Usage:
        engine = CoordinationEngine(providers=["Serper", "Groq", "Cerebras"])
        engine.report_session(session_data)   # after each completed session
        rec = engine.recommend(topic, entropy) # before each new session
    """

    def __init__(
        self,
        providers:        List[str] = None,
        default_provider: str       = "Serper",
        routing_mode:     str       = ROUTING_MODE_SAFE,
    ):
        self._lock     = threading.Lock()
        self._start_ts = time.time()
        self._decisions = 0
        self._last_decision_at: Optional[str] = None

        # Decision log kept in memory (also written to disk by gateway)
        self._decision_log: List[dict] = []
        self._routing_mode = routing_mode

        providers = providers or ["Serper", "Groq", "Cerebras"]
        self._default = default_provider
        self._providers: Dict[str, ProviderTracker] = {
            name: ProviderTracker(name=name) for name in providers
        }

    # ── State reporting ────────────────────────────────────────────────────────

    def report_session(
        self,
        provider:      str,
        violation:     bool,
        latency_ms:    float,
        sla_ms:        float,
        confidence:    float,
        entropy:       float,
        latency_p99_ms: Optional[float] = None,
        session_id:    str = "",
        topic:         str = "",
    ) -> dict:
        """
        Called after a session completes.
        Updates rolling CSI for the specified provider.
        Returns the provider's new state summary.
        """
        # Compute entropy from topic if provided
        if topic:
            entropy = topic_entropy_score(topic)

        metrics = SessionMetrics(
            violated       = violation,
            latency_ms     = latency_ms,
            latency_p99_ms = latency_p99_ms or latency_ms,
            sla_ms         = sla_ms,
            confidence     = confidence,
            topic_entropy  = entropy,
            provider_id    = provider.lower() + "_api",
            session_id     = session_id,
            timestamp_ms   = int(time.time() * 1000),
        )

        with self._lock:
            if provider not in self._providers:
                self._providers[provider] = ProviderTracker(name=provider)
            self._providers[provider].ingest(metrics)
            return self._providers[provider].to_dict()

    # ── Routing recommendation ─────────────────────────────────────────────────

    def recommend(
        self,
        topic:          str = "",
        entropy:        float = 0.5,
        require_stable: bool = False,
    ) -> Tuple[str, float, str, List[str]]:
        """
        Returns: (recommended_provider, confidence, reason, fallback_order)
        """
        if topic:
            entropy = topic_entropy_score(topic)

        import math, random

        with self._lock:
            # Pre-compute best_csi for relative bonus (competitive/probabilistic modes)
            best_csi = max(
                (t.csi for t in self._providers.values() if t.session_count >= 5),
                default=1.0,
            )
            scored = []
            for name, tracker in self._providers.items():
                sc = tracker.score(
                    entropy,
                    require_stable = require_stable,
                    mode           = self._routing_mode,
                    best_csi       = best_csi,
                )
                scored.append((sc, name, tracker))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Filter eligible (score > -999)
            eligible = [(sc, name, tr) for sc, name, tr in scored if sc > -999]

            if not eligible:
                best_name  = self._default
                best_score = 0.0
                reason     = f"All providers ineligible. Defaulting to {self._default}."
            elif self._routing_mode == ROUTING_MODE_PROBABILISTIC:
                # Softmax: scores → probabilities, then sample
                # Temperature controls sharpness of distribution
                raw_scores = [sc for sc, _, _ in eligible]
                # Shift scores to avoid large negatives before exp
                max_s  = max(raw_scores)
                exps   = [math.exp(SOFTMAX_TEMPERATURE * (sc - max_s)) for sc in raw_scores]
                total  = sum(exps)
                probs  = [e / total for e in exps]

                # Weighted random choice (deterministic seed for replay: use session counter)
                rng   = random.Random(self._decisions)
                r     = rng.random()
                cumulative = 0.0
                chosen_idx = 0
                for idx, p in enumerate(probs):
                    cumulative += p
                    if r <= cumulative:
                        chosen_idx = idx
                        break

                best_score, best_name, best_tracker = eligible[chosen_idx]
                state_str = best_tracker.state.value.upper()
                pct_str   = f"{probs[chosen_idx]*100:.1f}%"
                reason    = (
                    f"{best_name} selected (probabilistic {pct_str} weight): "
                    f"state={state_str} CSI={best_tracker.csi:.4f}"
                )
            else:
                # Deterministic argmax (safe / competitive)
                best_score, best_name, best_tracker = eligible[0]
                state_str = best_tracker.state.value.upper()
                reason    = (
                    f"{best_name} selected: state={state_str} "
                    f"CSI={best_tracker.csi:.4f} score={best_score:.4f}"
                )
                if not best_tracker.warmup_complete:
                    reason += " [warmup pending]"

            fallback_order = [name for _, name, _ in eligible if name != best_name]

            self._decisions += 1
            self._last_decision_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            return best_name, round(best_score, 4), reason, fallback_order

    # ── Log shadow decision ────────────────────────────────────────────────────

    def log_shadow_decision(
        self,
        session_id:           str,
        topic:                str,
        entropy:              float,
        current_provider:     str,
        recommended_provider: str,
        violation_occurred:   bool,
        current_provider_sessions: Optional[list] = None,
    ) -> dict:
        """
        After a session: compare what happened vs what was recommended.
        Returns the decision log entry.
        """
        with self._lock:
            # Would the recommended provider have violated?
            rec_tracker = self._providers.get(recommended_provider)
            # We can't know for certain — use violation rate as proxy
            would_have_violated = False
            if rec_tracker and rec_tracker.session_count > 5:
                would_have_violated = rec_tracker.violation_rate > 0.15

            # Delta: positive = recommendation was better
            cur_tracker = self._providers.get(current_provider)
            cur_vr  = cur_tracker.violation_rate  if cur_tracker  else 0.5
            rec_vr  = rec_tracker.violation_rate  if rec_tracker  else 0.5
            delta   = round(cur_vr - rec_vr, 4)

            entry = {
                "timestamp":                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id":               session_id,
                "topic":                    topic,
                "entropy":                  round(entropy, 4),
                "current_provider":         current_provider,
                "recommended_provider":     recommended_provider,
                "agreed":                   current_provider == recommended_provider,
                "current_provider_state":   self._providers[current_provider].state.value.upper() if current_provider in self._providers else "UNKNOWN",
                "recommended_provider_state": self._providers[recommended_provider].state.value.upper() if recommended_provider in self._providers else "UNKNOWN",
                "provider_csis":            {n: round(t.csi, 4) for n, t in self._providers.items()},
                "violation_occurred":       violation_occurred,
                "would_have_violated":      would_have_violated,
                "delta_signal":             delta,
            }
            self._decision_log.append(entry)
            return entry

    # ── Status ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            return {
                "uptime_seconds":    round(time.time() - self._start_ts, 1),
                "total_sessions":    sum(t.session_count for t in self._providers.values()),
                "providers":         {n: t.to_dict() for n, t in self._providers.items()},
                "routing_decisions": self._decisions,
                "last_decision_at":  self._last_decision_at,
                "routing_mode":      self._routing_mode,
                "shadow_mode":       True,
            }

    def provider_status(self, name: str) -> Optional[dict]:
        with self._lock:
            t = self._providers.get(name)
            return t.to_dict() if t else None

    @property
    def decision_log(self) -> List[dict]:
        with self._lock:
            return list(self._decision_log)