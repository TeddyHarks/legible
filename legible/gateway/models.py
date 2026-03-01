"""
legible/gateway/models.py

Pydantic models for the Legible Coordination Gateway API.
All request/response shapes live here for clean separation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


# ── Inbound ────────────────────────────────────────────────────────────────────

class SessionReport(BaseModel):
    """
    Posted by the caller after each completed session.
    Tells the gateway what actually happened — provider used, outcome, latency.
    The gateway uses this to update rolling CSI for that provider.
    """
    provider:    str   = Field(..., description="Provider that handled the session: 'Serper', 'Groq', 'Cerebras'")
    violation:   bool  = Field(..., description="True if any SLA target was missed")
    latency_ms:  float = Field(..., description="p50 latency for the session in ms")
    latency_p99_ms: Optional[float] = Field(None, description="p99 latency if known")
    sla_ms:      float = Field(5000.0, description="Declared SLA target for this session")
    confidence:  float = Field(1.0,   ge=0.0, le=1.0, description="Attribution confidence 0–1")
    entropy:     float = Field(0.5,   ge=0.0, le=1.0, description="Query entropy 0–1")
    topic:       str   = Field("",    description="Query topic for entropy calculation (optional, overrides entropy if set)")
    session_id:  str   = Field("",    description="Caller trace ID")


class RoutingRequest(BaseModel):
    """
    Posted BEFORE a session to get a routing recommendation.
    The gateway looks at current provider states and returns the best choice.
    """
    topic:      str   = Field("",  description="Query topic — used for entropy scoring")
    entropy:    float = Field(0.5, ge=0.0, le=1.0, description="Pre-computed entropy (overridden by topic if provided)")
    sla_ms:     float = Field(5000.0, description="Required SLA for this query")
    require_stable: bool = Field(False, description="If True, only GREEN providers are eligible")


# ── Outbound ───────────────────────────────────────────────────────────────────

class ProviderStatus(BaseModel):
    """Current status of one provider as known to the gateway."""
    name:              str
    csi:               float
    state:             str          # GREEN / YELLOW / ORANGE / RED
    violation_rate:    float
    session_count:     int
    warmup_complete:   bool         # True once 50+ sessions seen in GREEN
    score:             float        # Last computed routing score
    controls:          Dict[str, Any]


class RoutingRecommendation(BaseModel):
    """
    Response to POST /recommend.
    Shadow mode: this is advisory only. The caller decides whether to follow it.
    """
    recommended_provider:  str
    confidence:            float
    reason:                str
    entropy:               float
    provider_states:       Dict[str, ProviderStatus]
    fallback_order:        List[str]    # ordered list if primary is unavailable
    shadow_mode:           bool = True  # always True in V2


class GatewayState(BaseModel):
    """Full state snapshot — returned by GET /status."""
    uptime_seconds:    float
    total_sessions:    int
    providers:         Dict[str, ProviderStatus]
    routing_decisions: int
    last_decision_at:  Optional[str]
    shadow_mode:       bool = True


class DecisionLogEntry(BaseModel):
    """One entry in the shadow decision log."""
    timestamp:             str
    session_id:            str
    topic:                 str
    entropy:               float
    current_provider:      str   # what the caller actually used
    recommended_provider:  str   # what Legible would have used
    agreed:                bool  # did caller match recommendation?
    current_provider_state: str
    recommended_provider_state: str
    provider_csis:         Dict[str, float]
    violation_occurred:    bool
    would_have_violated:   bool  # would the recommendation have violated?
    delta_signal:          float # positive = recommendation was better