"""
substralink/sla/evidence.py

Evidence packet construction and export.

The evidence packet is the machine-readable dispute record for a session.
It is generated at finalization and can be exported for:
  - Kernel proof verification (GET /proofs/:decision_id)
  - External arbitration
  - Compliance logging
  - Audit trails

The packet is deterministic: same session inputs ? same packet.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from .attestation import CallerAttestation, ProviderAttestation
from .evaluator import SlaEvaluationResult
from .intent import SlaIntent


class EvidencePacket(BaseModel):
    """
    Complete evidence record for a resolved SLA session.

    This is the canonical artifact handed to arbiters and regulators.
    All fields are deterministically derived from session inputs.
    """

    # Session identity
    session_id: str
    decision_id: str          # SubstraLink kernel Decision ID

    # Participants
    caller_id: str
    provider_id: str

    # Contract
    sla: dict                 # SlaIntent serialized as dict

    # Evidence
    provider_attestations: list[dict]
    caller_attestations: list[dict]

    # Evaluation result
    evaluation: dict

    # Integrity
    evidence_root_hash: str   # SHA-256 of canonical packet (excluding this field)

    @classmethod
    def build(
        cls,
        session_id: str,
        decision_id: str,
        caller_id: str,
        provider_id: str,
        intent: SlaIntent,
        provider_attestations: list[ProviderAttestation],
        caller_attestations: list[CallerAttestation],
        evaluation: SlaEvaluationResult,
    ) -> "EvidencePacket":
        sla_dict = intent.model_dump()
        provider_dicts = [a.model_dump() for a in provider_attestations]
        caller_dicts = [a.model_dump() for a in caller_attestations]
        eval_dict = {
            "outcome": evaluation.outcome.to_dict(),
            "provider_slash": evaluation.provider_slash,
            "caller_slash": evaluation.caller_slash,
            "violation_count": evaluation.violation_count,
            "total_calls": evaluation.total_calls,
            "latency_violation_count": evaluation.latency_violation_count,
            "correctness_violation_count": evaluation.correctness_violation_count,
            "caller_fault_count": evaluation.caller_fault_count,
            "attribution_confidence": evaluation.attribution_confidence,
            "reason_summary": evaluation.reason_summary,
        }

        # Compute root hash over the packet contents (excluding the hash field itself)
        hashable = {
            "session_id": session_id,
            "decision_id": decision_id,
            "caller_id": caller_id,
            "provider_id": provider_id,
            "sla": sla_dict,
            "provider_attestations": provider_dicts,
            "caller_attestations": caller_dicts,
            "evaluation": eval_dict,
        }
        canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"))
        root_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        return cls(
            session_id=session_id,
            decision_id=decision_id,
            caller_id=caller_id,
            provider_id=provider_id,
            sla=sla_dict,
            provider_attestations=provider_dicts,
            caller_attestations=caller_dicts,
            evaluation=eval_dict,
            evidence_root_hash=root_hash,
        )

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(
            self.model_dump(),
            sort_keys=True,
            separators=(",", ":") if indent is None else (",", ": "),
            indent=indent,
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json(indent=2))
