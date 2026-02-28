"""
legible/firewall/state_machine.py

V2 Coordination Firewall ? deterministic state machine.

States:
    GREEN   CSI >= 0.95   Autonomous mode
    YELLOW  CSI >= 0.85   Supervised automation
    ORANGE  CSI >= 0.70   Risk throttling
    RED     CSI <  0.70   Circuit breaker

The firewall does NOT execute actions.
It emits control signals that the orchestrator applies.
This separation keeps the firewall auditable and testable.

Transitions are logged. State history is queryable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any

from .rolling_csi import RollingCSI, CSIComponents
from .models import SessionMetrics


# ??? State definitions ????????????????????????????????????????????????????????

class FirewallState(Enum):
    GREEN  = "green"   # CSI >= 0.95 ? full autonomous
    YELLOW = "yellow"  # CSI >= 0.85 ? supervised
    ORANGE = "orange"  # CSI >= 0.70 ? throttled
    RED    = "red"     # CSI <  0.70 ? circuit breaker


# State thresholds ? single source of truth
STATE_THRESHOLDS = {
    FirewallState.GREEN:  0.95,
    FirewallState.YELLOW: 0.85,
    FirewallState.ORANGE: 0.70,
    FirewallState.RED:    0.0,
}

STATE_COLORS = {
    FirewallState.GREEN:  "GREEN ",
    FirewallState.YELLOW: "YELLOW",
    FirewallState.ORANGE: "ORANGE",
    FirewallState.RED:    "RED   ",
}


def csi_to_state(csi: float) -> FirewallState:
    """Deterministic mapping from CSI value to firewall state."""
    if csi >= 0.95:
        return FirewallState.GREEN
    elif csi >= 0.85:
        return FirewallState.YELLOW
    elif csi >= 0.70:
        return FirewallState.ORANGE
    else:
        return FirewallState.RED


# ??? Control signals ??????????????????????????????????????????????????????????

def control_actions(state: FirewallState) -> Dict[str, Any]:
    """
    Return control signal dict for a given state.

    The orchestrator reads these signals and adjusts:
        mode          ? execution authorization level
        var_multiplier ? scale the declared VAR (risk budget)
        redundancy    ? number of parallel providers to call
        quorum_boost  ? additional quorum threshold (additive)
        log_expansion ? whether to expand evidence capture
        human_required ? whether human approval needed for high-VAR actions

    These are RECOMMENDATIONS, not commands.
    The orchestrator decides how to apply them.
    """
    return {
        FirewallState.GREEN: {
            "mode":           "autonomous",
            "var_multiplier": 1.0,
            "redundancy":     1,
            "quorum_boost":   0,
            "log_expansion":  False,
            "human_required": False,
            "description":    "Full autonomous operation. Normal VAR. Standard quorum.",
        },
        FirewallState.YELLOW: {
            "mode":           "supervised",
            "var_multiplier": 0.8,
            "redundancy":     1,
            "quorum_boost":   1,
            "log_expansion":  True,
            "human_required": False,
            "description":    "Supervised mode. VAR reduced 20%. Quorum threshold +1. "
                              "Expanded logging active.",
        },
        FirewallState.ORANGE: {
            "mode":           "throttled",
            "var_multiplier": 0.5,
            "redundancy":     2,
            "quorum_boost":   2,
            "log_expansion":  True,
            "human_required": True,
            "description":    "Throttled mode. VAR reduced 50%. Dual-provider redundancy. "
                              "Human approval required for high-VAR actions.",
        },
        FirewallState.RED: {
            "mode":           "circuit_breaker",
            "var_multiplier": 0.0,
            "redundancy":     0,
            "quorum_boost":   999,
            "log_expansion":  True,
            "human_required": True,
            "description":    "Circuit breaker active. Automated execution frozen. "
                              "Read-only / low-risk only. Manual override required to resume.",
        },
    }[state]


# ??? Transition record ????????????????????????????????????????????????????????

@dataclass
class StateTransition:
    from_state:  FirewallState
    to_state:    FirewallState
    csi:         float
    timestamp_ms: int
    trigger:     str = ""


# ??? Main firewall class ??????????????????????????????????????????????????????

class CoordinationFirewall:
    """
    V2 Coordination Firewall.

    Maintains rolling CSI and transitions between states automatically.
    Emits control signals to the orchestration layer.

    Usage:
        fw = CoordinationFirewall(provider_id="serper_api")
        fw.ingest(session_metrics)
        print(fw.csi)      # 0.9370
        print(fw.state)    # FirewallState.YELLOW
        print(fw.controls) # {"mode": "supervised", ...}
        print(fw.report()) # human-readable status
    """

    def __init__(
        self,
        provider_id: str = "unknown",
        window_size: int = 100,
    ):
        self.provider_id    = provider_id
        self.engine         = RollingCSI(window_size=window_size)
        self.state          = FirewallState.GREEN
        self.last_csi       = 1.0
        self.last_components: Optional[CSIComponents] = None
        self.transitions:   List[StateTransition] = []
        self.session_count  = 0

    def ingest(self, session: SessionMetrics) -> Optional[StateTransition]:
        """
        Feed one session into the firewall.
        Recomputes CSI. Transitions state if threshold crossed.

        Returns StateTransition if state changed, None if stable.
        """
        self.engine.add(session)
        self.session_count += 1

        components    = self.engine.compute()
        self.last_csi = components.csi
        self.last_components = components

        new_state  = csi_to_state(components.csi)
        prev_state = self.state

        if new_state != prev_state:
            transition = StateTransition(
                from_state   = prev_state,
                to_state     = new_state,
                csi          = components.csi,
                timestamp_ms = int(time.time() * 1000),
                trigger      = f"session_{self.session_count}",
            )
            self.transitions.append(transition)
            self.state = new_state
            return transition

        self.state = new_state
        return None

    def ingest_many(self, sessions: List[SessionMetrics]) -> List[StateTransition]:
        """Ingest a batch of sessions. Returns all transitions that occurred."""
        transitions = []
        for s in sessions:
            t = self.ingest(s)
            if t:
                transitions.append(t)
        return transitions

    @property
    def csi(self) -> float:
        return self.last_csi

    @property
    def controls(self) -> Dict[str, Any]:
        return control_actions(self.state)

    @property
    def is_safe(self) -> bool:
        """True if autonomous operation is permitted."""
        return self.state in (FirewallState.GREEN, FirewallState.YELLOW)

    @property
    def is_degraded(self) -> bool:
        """True if the system is in a degraded state."""
        return self.state in (FirewallState.ORANGE, FirewallState.RED)

    def report(self) -> str:
        """Human-readable firewall status."""
        c = self.last_components
        col = STATE_COLORS.get(self.state, "?")
        lines = [
            f"",
            f"  Coordination Firewall ? {self.provider_id}",
            f"  {'?'*46}",
            f"  State:    [{col}]  CSI = {self.csi:.4f}",
        ]
        if c:
            lines += [
                f"  Window:   {c.window_size} sessions",
                f"  Violations: {c.violation_count} ({c.violation_rate:.1%})",
                f"  RF  = {c.rf:.4f}   TRF = {c.trf:.4f}",
                f"  AC  = {c.ac:.4f}   EC  = {c.ec:.4f}",
            ]
        ctrl = self.controls
        lines += [
            f"  Mode:     {ctrl['mode']}",
            f"  VAR:      {ctrl['var_multiplier']*100:.0f}%",
            f"  Redundancy: {ctrl['redundancy']}x",
        ]
        if self.transitions:
            lines.append(f"  Transitions: {len(self.transitions)}")
            for t in self.transitions[-3:]:
                lines.append(
                    f"    {STATE_COLORS[t.from_state]} -> "
                    f"{STATE_COLORS[t.to_state]}  "
                    f"CSI={t.csi:.4f}  @session_{t.trigger}"
                )
        lines.append(f"  {'?'*46}")
        return "\n".join(lines)
