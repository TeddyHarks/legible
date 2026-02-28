"""
substralink/sla/attestation.py

Attestation types for SLA sessions.

Provider attestations record what happened on each call.
Caller attestations record request conformance (auto-generated in V1).

Both serialize into the `reasoning` field of a SubstraLink Attestation.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


def _sha256(data: Any) -> str:
    """SHA-256 hex digest of the canonical JSON encoding of any serializable value."""
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ProviderAttestation(BaseModel):
    """
    Evidence for a single tracked call, submitted by the provider side.
    Generated automatically by session.track_call().
    """
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    request_hash: str
    response_hash: str
    latency_ms: int
    status_code: int
    correctness_passed: bool

    @classmethod
    def build(
        cls,
        request: Any,
        response: Any,
        latency_ms: int,
        status_code: int,
        correctness_passed: bool,
    ) -> "ProviderAttestation":
        return cls(
            request_hash=_sha256(request),
            response_hash=_sha256(response),
            latency_ms=latency_ms,
            status_code=status_code,
            correctness_passed=correctness_passed,
        )

    def to_reasoning_json(self) -> str:
        """Serialized form for the kernel Attestation.reasoning field."""
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))


class CallerAttestation(BaseModel):
    """
    Evidence submitted by the caller side, auto-generated in V1.
    Records whether the outbound request conformed to declared SLA parameters.
    """
    call_id: str
    request_well_formed: bool = True
    declared_var_mismatch: bool = False

    def to_reasoning_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))


def evaluate_correctness(response: Any, rule: str) -> bool:
    """
    Applies the declared correctness rule to a response.
    Returns True if the response satisfies the rule.
    """
    if rule == "valid_json":
        if isinstance(response, (dict, list)):
            return True
        if isinstance(response, str):
            try:
                json.loads(response)
                return True
            except (json.JSONDecodeError, ValueError):
                return False
        return False

    if rule == "status_2xx":
        # Expects response to have a .status_code attribute or be an int
        code = getattr(response, "status_code", response)
        return isinstance(code, int) and 200 <= code < 300

    if rule == "non_empty":
        if response is None:
            return False
        if isinstance(response, (str, bytes, list, dict)):
            return len(response) > 0
        return True

    if rule == "schema_match":
        # V1: schema_match is a pass-through ? full schema validation in V2
        return True

    # Unknown rule: conservative pass
    return True
