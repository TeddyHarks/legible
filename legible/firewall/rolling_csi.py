"""
legible/firewall/rolling_csi.py

Rolling Coordination Stability Index (CSI) engine.

CSI is a bounded 0?1 metric derived from four components:

    CSI = (0.4 * RF) + (0.25 * TRF) + (0.2 * AC) + (0.15 * EC)

Where:
    RF  = Reliability Factor     = 1 - violation_rate
    TRF = Tail Risk Factor       = 1 - min(0.25, mean_tail_excess)
    AC  = Attribution Clarity    = mean confidence across window
    EC  = Entropy Clustering     = 1 - (mean_entropy * 0.10)

Weights rationale:
    RF  0.40 ? reliability is the primary signal
    TRF 0.25 ? tail behavior predicts cascade risk
    AC  0.20 ? ambiguity destroys trust faster than violations
    EC  0.15 ? entropy is context, not cause

CSI thresholds:
    >= 0.95  GREEN   ? autonomous mode
    >= 0.85  YELLOW  ? supervised
    >= 0.70  ORANGE  ? throttled
     < 0.70  RED     ? circuit breaker
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import mean
from typing import List, Optional

from .models import SessionMetrics
from .entropy import entropy_cluster_factor


# ??? CSI formula weights ??????????????????????????????????????????????????????
W_RF  = 0.40   # Reliability Factor
W_TRF = 0.25   # Tail Risk Factor
W_AC  = 0.20   # Attribution Clarity
W_EC  = 0.15   # Entropy Clustering


@dataclass
class CSIComponents:
    """
    Decomposed CSI ? every component exposed for auditability.
    Enterprises need to see why the number is what it is.
    """
    rf:  float   # Reliability Factor
    trf: float   # Tail Risk Factor
    ac:  float   # Attribution Clarity
    ec:  float   # Entropy Clustering
    csi: float   # Final weighted score

    window_size:     int
    violation_count: int
    violation_rate:  float
    mean_latency_ms: float
    mean_confidence: float
    mean_entropy:    float

    def explain(self) -> str:
        lines = [
            f"CSI = {self.csi:.4f}",
            f"",
            f"  RF  (Reliability,   w=0.40): {self.rf:.4f}  "
            f"[{self.violation_count}/{self.window_size} violations, "
            f"rate={self.violation_rate:.1%}]",
            f"  TRF (Tail Risk,     w=0.25): {self.trf:.4f}",
            f"  AC  (Attribution,   w=0.20): {self.ac:.4f}  "
            f"[mean confidence={self.mean_confidence:.3f}]",
            f"  EC  (Entropy Clust, w=0.15): {self.ec:.4f}  "
            f"[mean entropy={self.mean_entropy:.3f}]",
            f"",
            f"  Window:         {self.window_size} sessions",
            f"  Mean latency:   {self.mean_latency_ms:.0f}ms",
        ]
        return "\n".join(lines)


class RollingCSI:
    """
    Maintains a rolling window of SessionMetrics and computes CSI on demand.

    Usage:
        engine = RollingCSI(window_size=100)
        engine.add(session_metrics)
        components = engine.compute()
        print(components.csi)   # 0.9370
        print(components.explain())
    """

    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.window: deque = deque(maxlen=window_size)

    def add(self, session: SessionMetrics) -> None:
        self.window.append(session)

    def add_many(self, sessions: List[SessionMetrics]) -> None:
        for s in sessions:
            self.add(s)

    @property
    def count(self) -> int:
        return len(self.window)

    @property
    def is_empty(self) -> bool:
        return len(self.window) == 0

    def compute(self) -> CSIComponents:
        """
        Compute CSI from the current window.
        Returns CSIComponents with all intermediate values exposed.
        If window is empty, returns perfect score (no evidence of instability).
        """
        if self.is_empty:
            return CSIComponents(
                rf=1.0, trf=1.0, ac=1.0, ec=1.0, csi=1.0,
                window_size=0, violation_count=0, violation_rate=0.0,
                mean_latency_ms=0.0, mean_confidence=1.0, mean_entropy=0.0,
            )

        sessions = list(self.window)
        n = len(sessions)

        # ?? RF ? Reliability Factor ???????????????????????????????????????????
        violations    = sum(1 for s in sessions if s.violated)
        violation_rate = violations / n
        rf            = 1.0 - violation_rate

        # ?? TRF ? Tail Risk Factor ????????????????????????????????????????????
        # Mean tail excess across all sessions
        # tail_excess = max(0, (p99 - sla) / sla) per session
        tail_excesses = [s.tail_excess_ratio for s in sessions]
        mean_tail     = mean(tail_excesses)
        trf           = 1.0 - min(0.25, mean_tail)

        # ?? AC ? Attribution Clarity ??????????????????????????????????????????
        # Mean confidence from evaluator
        # 1.0 = all attributions clear, 0.5 = all ambiguous
        confidences = [s.confidence for s in sessions]
        ac          = mean(confidences)

        # ?? EC ? Entropy Clustering ???????????????????????????????????????????
        entropy_scores = [s.topic_entropy for s in sessions]
        ec             = entropy_cluster_factor(entropy_scores)
        mean_entropy   = mean(entropy_scores)

        # ?? Final CSI ?????????????????????????????????????????????????????????
        csi = (W_RF * rf) + (W_TRF * trf) + (W_AC * ac) + (W_EC * ec)
        csi = round(max(0.0, min(1.0, csi)), 4)

        return CSIComponents(
            rf=round(rf, 4),
            trf=round(trf, 4),
            ac=round(ac, 4),
            ec=round(ec, 4),
            csi=csi,
            window_size=n,
            violation_count=violations,
            violation_rate=round(violation_rate, 6),
            mean_latency_ms=round(mean(s.latency_ms for s in sessions), 1),
            mean_confidence=round(mean(confidences), 4),
            mean_entropy=round(mean_entropy, 4),
        )

    def csi_value(self) -> float:
        """Convenience: just the scalar CSI."""
        return self.compute().csi

    def rolling_csi_series(
        self,
        all_sessions: List[SessionMetrics],
        step: int = 1,
    ) -> List[tuple]:
        """
        Compute CSI at each step through a list of sessions.
        Useful for plotting stability over time.

        Returns list of (session_index, csi_value) tuples.
        """
        engine = RollingCSI(window_size=self.window_size)
        result = []
        for i, s in enumerate(all_sessions):
            engine.add(s)
            if (i + 1) % step == 0 or i == len(all_sessions) - 1:
                result.append((i + 1, engine.csi_value()))
        return result
