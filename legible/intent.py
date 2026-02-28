"""
legible/intent.py

SlaIntent schema and canonical JSON serialization.

Implements: RFC-0002 ?2.1, ?4, ?7
Protocol:   Legible Protocol v1.0.0
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

SUPPORTED_CORRECTNESS_RULES = frozenset({
    "valid_json",
    "status_2xx",
    "schema_match",
    "non_empty",
})


class SlaIntent(BaseModel):
    """
    Defines the SLA contract for a coordination session.

    Canonical JSON serialization (RFC-0002 ?7):
      Keys sorted alphabetically. No whitespace. UTF-8.
    """

    type: Literal["sla.v1"] = "sla.v1"
    domain: str = "ai.api"

    caller_id: str
    provider_id: str

    declared_var: int
    latency_ms: int
    correctness_rule: str
    availability_window_ms: int = 5000
    strictness_multiplier: float = 1.0
    dependency_depth: int = 1
    exposure_time_ms: int = 5000
    provider_stake_ratio: float = 0.9
    caller_stake_ratio: float = 0.1

    @field_validator("declared_var")
    @classmethod
    def var_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("declared_var must be > 0")
        return v

    @field_validator("latency_ms")
    @classmethod
    def latency_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("latency_ms must be > 0")
        return v

    @field_validator("strictness_multiplier")
    @classmethod
    def strictness_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("strictness_multiplier must be positive")
        return v

    @field_validator("correctness_rule")
    @classmethod
    def rule_known(cls, v: str) -> str:
        if v not in SUPPORTED_CORRECTNESS_RULES:
            raise ValueError(
                f"Unknown correctness_rule '{v}'. "
                f"Supported: {sorted(SUPPORTED_CORRECTNESS_RULES)}"
            )
        return v

    @model_validator(mode="after")
    def stake_ratios_sum_to_one(self) -> "SlaIntent":
        total = self.provider_stake_ratio + self.caller_stake_ratio
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"stake ratios must sum to 1.0, got {total:.4f}"
            )
        return self

    def to_canonical_json(self) -> str:
        """RFC-0002 ?7: sorted keys, no whitespace, UTF-8."""
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    def to_kernel_intent(self) -> str:
        return self.to_canonical_json()
