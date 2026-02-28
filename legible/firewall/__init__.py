"""
legible/firewall/

Coordination Stability Index (CSI) and V2 Firewall state machine.

Components:
    models.py       ? SessionMetrics dataclass
    entropy.py      ? topic entropy scoring
    rolling_csi.py  ? rolling CSI computation
    state_machine.py ? firewall state transitions + control signals

Usage:
    from legible.firewall import CoordinationFirewall, SessionMetrics

    fw = CoordinationFirewall(window_size=100)
    fw.ingest(SessionMetrics(...))
    print(fw.csi)        # 0.9370
    print(fw.state)      # FirewallState.YELLOW
    print(fw.controls)   # {"mode": "supervised", "var_multiplier": 0.8, ...}
"""

from .models import SessionMetrics
from .entropy import topic_entropy_score, ENTROPY_SCORES
from .rolling_csi import RollingCSI, CSIComponents
from .state_machine import FirewallState, CoordinationFirewall, control_actions

__all__ = [
    "SessionMetrics",
    "topic_entropy_score",
    "ENTROPY_SCORES",
    "RollingCSI",
    "CSIComponents",
    "FirewallState",
    "CoordinationFirewall",
    "control_actions",
]
