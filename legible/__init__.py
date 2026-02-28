"""
Legible ? Autonomous coordination, readable, verifiable, enforceable.

Protocol: legible-protocol v1.0.0
Kernel:   SubstraLink

Primary interface:
    from legible import Session, SlaIntent
"""

from .intent import SlaIntent
from .session import Session, SessionError
from .evaluator import evaluate_sla, validate_intent, severity_from_slash
from .evidence import EvidencePacket

__all__ = [
    "Session",
    "SessionError",
    "SlaIntent",
    "EvidencePacket",
    "evaluate_sla",
    "validate_intent",
    "severity_from_slash",
]
