"""
legible/firewall/models.py

SessionMetrics ? the unit of input to the CSI engine.

Every resolved session produces one SessionMetrics instance.
The firewall consumes these and maintains a rolling window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionMetrics:
    """
    Normalized snapshot of a single resolved session.

    Fields:
        violated        ? True if any SLA violation occurred
        latency_ms      ? p50 (median) latency of calls in this session
        latency_p99_ms  ? p99 latency if known, else same as latency_ms
        sla_ms          ? declared SLA target for this session
        confidence      ? attribution confidence 0?1 from evaluator
        topic_entropy   ? 0?1 normalized entropy score for the query topic
        provider_id     ? which provider this session measured
        session_id      ? optional trace id
        timestamp_ms    ? epoch ms when session resolved
    """

    violated: bool
    latency_ms: float
    sla_ms: float
    confidence: float
    topic_entropy: float          # 0.0 = low entropy, 1.0 = high entropy

    latency_p99_ms: Optional[float] = None
    provider_id: str = "unknown"
    session_id: str = ""
    timestamp_ms: int = 0

    def __post_init__(self):
        # Clamp entropy to valid range
        self.topic_entropy = max(0.0, min(1.0, self.topic_entropy))
        self.confidence    = max(0.0, min(1.0, self.confidence))
        # Default p99 to p50 if not provided
        if self.latency_p99_ms is None:
            self.latency_p99_ms = self.latency_ms

    @property
    def tail_excess_ratio(self) -> float:
        """
        How much does the p99 latency exceed the SLA target?
        Returns 0 if within SLA, positive ratio if over.
        """
        if self.sla_ms <= 0:
            return 0.0
        excess = self.latency_p99_ms - self.sla_ms
        return max(0.0, excess / self.sla_ms)

    @classmethod
    def from_batch_record(cls, record: dict, entropy_fn=None) -> "SessionMetrics":
        """
        Build a SessionMetrics from a batch_runner session record dict.

        Args:
            record    ? dict from sessions_YYYYMMDD.jsonl
            entropy_fn ? optional callable(topic: str) -> float
        """
        from .entropy import topic_entropy_score

        topic   = record.get("topic", "")
        entropy = entropy_fn(topic) if entropy_fn else topic_entropy_score(topic)

        latencies = record.get("latency_ms", [record.get("latency_p50", 3500)])
        p50 = record.get("latency_p50", latencies[0] if latencies else 3500)

        return cls(
            violated       = not record.get("passed", True),
            latency_ms     = float(p50),
            latency_p99_ms = float(record.get("latency_max", p50)),
            sla_ms         = float(record.get("sla_target_ms", 5000)),
            confidence     = float(record.get("confidence", 1.0)),
            topic_entropy  = entropy,
            provider_id    = record.get("provider", "unknown"),
            session_id     = record.get("session_id", ""),
            timestamp_ms   = record.get("timestamp", 0)
                             if isinstance(record.get("timestamp", 0), int)
                             else 0,
        )
