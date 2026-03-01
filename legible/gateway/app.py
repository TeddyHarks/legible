"""
legible/gateway/app.py

Legible Coordination Gateway — Shadow Mode (V2)
================================================
FastAPI service that maintains live provider state and emits routing
recommendations. Does NOT proxy traffic. Advisory only.

Endpoints:
    POST /report        — report a completed session outcome
    POST /recommend     — get a routing recommendation before a session
    POST /evaluate      — combined: report outcome + get next recommendation
    GET  /status        — full gateway state
    GET  /providers     — all provider CSI summaries
    GET  /providers/{name} — single provider detail
    GET  /decisions     — shadow decision log (last N entries)
    GET  /health        — liveness check

Run:
    pip install fastapi uvicorn
    uvicorn legible.gateway.app:app --reload --port 8080

Or from repo root:
    python -m legible.gateway
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# FastAPI import — graceful error if not installed
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    raise ImportError(
        "\n\nFastAPI is required for the Coordination Gateway.\n"
        "Install it with:\n\n"
        "    pip install fastapi uvicorn\n"
    )

from .engine import CoordinationEngine, ROUTING_MODE_SAFE, ROUTING_MODE_COMPETITIVE, ROUTING_MODE_PROBABILISTIC
from .models import (
    SessionReport,
    RoutingRequest,
    RoutingRecommendation,
    ProviderStatus,
    GatewayState,
    DecisionLogEntry,
)

# ── Configuration ──────────────────────────────────────────────────────────────
PROVIDERS       = os.environ.get("LEGIBLE_PROVIDERS", "Serper,Groq,Cerebras").split(",")
DEFAULT_PROVIDER = os.environ.get("LEGIBLE_DEFAULT_PROVIDER", "Serper")
LOG_DIR         = Path(os.environ.get("LEGIBLE_LOG_DIR", "logs"))
ROUTING_MODE    = os.environ.get("LEGIBLE_ROUTING_MODE", "safe")
DECISION_LOG    = LOG_DIR / "shadow_decisions.jsonl"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Legible Coordination Gateway",
    description = (
        "Shadow-mode coordination intelligence for AI infrastructure.\n\n"
        "Maintains rolling CSI per provider, classifies regime state, "
        "and emits routing recommendations. Does not proxy traffic.\n\n"
        "**RFC-0002 compliant.** All decisions logged to JSONL for audit."
    ),
    version     = "2.0.0-shadow",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)

# Singleton engine — one per process
engine = CoordinationEngine(
    providers        = PROVIDERS,
    default_provider = DEFAULT_PROVIDER,
    routing_mode     = ROUTING_MODE,
)

_start_time = time.time()


# ── Helpers ────────────────────────────────────────────────────────────────────

def write_decision_log(entry: dict) -> None:
    with open(DECISION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def provider_status_model(name: str) -> ProviderStatus:
    d = engine.provider_status(name)
    if not d:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return ProviderStatus(**d)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    """Liveness check."""
    return {
        "status":  "ok",
        "uptime":  round(time.time() - _start_time, 1),
        "version": "2.0.0-shadow",
        "mode":    "shadow",
    }


@app.get("/status", response_model=GatewayState, tags=["System"])
def get_status():
    """Full gateway state — all providers, session counts, uptime."""
    s = engine.status()
    return GatewayState(
        uptime_seconds    = s["uptime_seconds"],
        total_sessions    = s["total_sessions"],
        providers         = {n: ProviderStatus(**v) for n, v in s["providers"].items()},
        routing_decisions = s["routing_decisions"],
        last_decision_at  = s["last_decision_at"],
        shadow_mode       = True,
    )


@app.get("/providers", tags=["Providers"])
def list_providers():
    """CSI summary for all providers."""
    return engine.status()["providers"]


@app.get("/providers/{name}", response_model=ProviderStatus, tags=["Providers"])
def get_provider(name: str):
    """Detailed status for a single provider."""
    return provider_status_model(name)


@app.post("/report", tags=["Coordination"])
def report_session(body: SessionReport):
    """
    Report a completed session outcome.

    Call this after every session your system processes.
    The gateway updates rolling CSI for the specified provider.

    Returns the provider's updated state.
    """
    result = engine.report_session(
        provider        = body.provider,
        violation       = body.violation,
        latency_ms      = body.latency_ms,
        latency_p99_ms  = body.latency_p99_ms,
        sla_ms          = body.sla_ms,
        confidence      = body.confidence,
        entropy         = body.entropy,
        topic           = body.topic,
        session_id      = body.session_id,
    )
    return {
        "accepted":  True,
        "provider":  body.provider,
        "new_state": result,
    }


@app.post("/recommend", response_model=RoutingRecommendation, tags=["Coordination"])
def get_recommendation(body: RoutingRequest):
    """
    Get a routing recommendation before sending a query.

    The gateway scores all providers based on:
    - Rolling CSI (last 50 sessions)
    - Regime state (GREEN/YELLOW/ORANGE/RED)
    - Query entropy
    - Warmup completion

    **Shadow mode**: this is advisory. Your system decides whether to follow it.
    """
    provider, confidence, reason, fallback = engine.recommend(
        topic          = body.topic,
        entropy        = body.entropy,
        require_stable = body.require_stable,
    )

    providers_status = {
        n: ProviderStatus(**engine.provider_status(n))
        for n in PROVIDERS
        if engine.provider_status(n)
    }

    return RoutingRecommendation(
        recommended_provider = provider,
        confidence           = confidence,
        reason               = reason,
        entropy              = body.entropy,
        provider_states      = providers_status,
        fallback_order       = fallback,
        shadow_mode          = True,
    )


@app.post("/evaluate", tags=["Coordination"])
def evaluate(body: SessionReport):
    """
    Combined endpoint: report what just happened + get recommendation for next query.

    This is the primary integration point for tight feedback loops.
    Call this after each session with the outcome, receive the next routing recommendation.

    Shadow decision is logged to `logs/shadow_decisions.jsonl`.
    """
    # 1. Update provider state with what happened
    engine.report_session(
        provider        = body.provider,
        violation       = body.violation,
        latency_ms      = body.latency_ms,
        latency_p99_ms  = body.latency_p99_ms,
        sla_ms          = body.sla_ms,
        confidence      = body.confidence,
        entropy         = body.entropy,
        topic           = body.topic,
        session_id      = body.session_id,
    )

    # 2. Get next recommendation
    recommended, confidence, reason, fallback = engine.recommend(
        topic   = body.topic,
        entropy = body.entropy,
    )

    # 3. Log shadow decision — compare actual vs recommended
    entry = engine.log_shadow_decision(
        session_id           = body.session_id,
        topic                = body.topic,
        entropy              = body.entropy,
        current_provider     = body.provider,
        recommended_provider = recommended,
        violation_occurred   = body.violation,
    )
    write_decision_log(entry)

    providers_status = {
        n: engine.provider_status(n)
        for n in PROVIDERS
        if engine.provider_status(n)
    }

    return {
        "session_accepted": True,
        "shadow_decision":  entry,
        "next_recommendation": {
            "provider":   recommended,
            "confidence": confidence,
            "reason":     reason,
            "fallback":   fallback,
        },
        "provider_states": providers_status,
    }


@app.get("/mode", tags=["System"])
def get_mode():
    """Current routing mode: safe or competitive."""
    return {
        "routing_mode": engine._routing_mode,
        "description": {
            "safe":        "Enterprise conservative — minimize risk, prefer stable providers",
            "competitive": "Alpha extraction — exploit regime leaders, relative CSI scoring",
            "probabilistic": "Softmax traffic split — scores become probabilities, diversification by design",
        }.get(engine._routing_mode, "unknown"),
    }


@app.post("/mode/{mode}", tags=["System"])
def set_mode(mode: str):
    """
    Switch routing mode at runtime — no restart required.

    - **safe**: conservative, strong state penalties, prefer proven stability
    - **competitive**: exploit regime differences, relative CSI scoring, reduced penalties
    """
    if mode not in (ROUTING_MODE_SAFE, ROUTING_MODE_COMPETITIVE, ROUTING_MODE_PROBABILISTIC):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'. Use 'safe', 'competitive', or 'probabilistic'.")
    prev = engine._routing_mode
    engine._routing_mode = mode
    return {
        "previous_mode": prev,
        "current_mode":  mode,
        "changed":       prev != mode,
    }


@app.get("/decisions", tags=["Audit"])
def get_decisions(
    limit: int = Query(50, ge=1, le=1000, description="Max entries to return"),
    agreed_only: bool = Query(False, description="Only show sessions where caller matched recommendation"),
    disagreed_only: bool = Query(False, description="Only show sessions where caller differed from recommendation"),
):
    """
    Shadow decision log.

    Shows what the gateway recommended vs what the caller actually used.
    Over time this builds the 'would have' dataset proving routing value.
    """
    log = engine.decision_log
    if agreed_only:
        log = [e for e in log if e["agreed"]]
    if disagreed_only:
        log = [e for e in log if not e["agreed"]]

    total     = len(log)
    log       = log[-limit:]
    agreed    = sum(1 for e in engine.decision_log if e["agreed"])
    agreement = round(agreed / total * 100, 1) if total else 0

    # Compute cumulative delta
    deltas = [e["delta_signal"] for e in engine.decision_log]
    cum_delta = round(sum(deltas), 4)

    return {
        "total_decisions":   total,
        "agreement_rate":    f"{agreement}%",
        "cumulative_delta":  cum_delta,
        "entries":           log,
    }


@app.delete("/decisions", tags=["Audit"])
def clear_decisions():
    """Clear in-memory decision log. Does not delete the JSONL file."""
    engine._decision_log.clear()
    return {"cleared": True}