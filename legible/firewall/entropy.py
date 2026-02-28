"""
legible/firewall/entropy.py

Topic entropy scoring ? quantifies how "noisy" a query topic is.

Entropy here means: how likely is this topic to produce volatile,
rapidly-changing, high-competition search results?

We observed from 800 sessions that violations cluster around:
  - Market-sensitive queries (NVIDIA earnings, inflation CPI)
  - High-competition AI topics (LLM benchmarks, Meta AI Llama)
  - Breaking/volatile news domains

High entropy topics ? higher SLA violation probability ? lower EC component.

Scoring approach:
  1. Keyword-based scoring (fast, deterministic, auditable)
  2. Score = weighted sum of matched entropy signals
  3. Normalized to 0?1

This is intentionally transparent ? no ML, no black box.
Enterprises need to audit this formula.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


# ??? Entropy signal weights ???????????????????????????????????????????????????
# Each tuple: (keyword, entropy_score)
# Score 0.0 = stable/static topic
# Score 1.0 = maximally volatile/noisy topic

ENTROPY_SIGNALS: List[Tuple[str, float]] = [
    # High volatility ? market / financial
    ("earnings",       0.85),
    ("revenue",        0.80),
    ("quarterly",      0.80),
    ("stock",          0.75),
    ("market",         0.70),
    ("inflation",      0.80),
    ("cpi",            0.80),
    ("fed ",           0.75),
    ("federal reserve",0.80),
    ("yield",          0.70),
    ("ipo",            0.75),
    ("venture capital",0.65),
    ("investment",     0.60),

    # High volatility ? AI / tech news
    ("llama",          0.85),
    ("gpt",            0.80),
    ("openai",         0.75),
    ("nvidia",         0.80),
    ("semiconductor",  0.65),
    ("chip demand",    0.80),
    ("ai chip",        0.80),
    ("inference cost", 0.75),
    ("llm",            0.70),
    ("foundation model",0.65),
    ("benchmark",      0.65),

    # Medium volatility ? tech
    ("azure",          0.55),
    ("aws",            0.50),
    ("google",         0.50),
    ("microsoft",      0.50),
    ("meta ",          0.60),
    ("tesla",          0.65),
    ("autonomous",     0.55),

    # Low volatility ? stable concepts
    ("architecture",   0.20),
    ("regulation",     0.30),
    ("safety",         0.25),
    ("research",       0.25),
    ("theory",         0.15),
    ("drug discovery", 0.30),
    ("climate",        0.35),
    ("cybersecurity",  0.35),
    ("quantum",        0.30),
]

# Pre-build lookup for fast scoring
ENTROPY_SCORES: Dict[str, float] = dict(ENTROPY_SIGNALS)


def topic_entropy_score(topic: str) -> float:
    """
    Compute a 0?1 entropy score for a query topic.

    Higher score = more volatile/noisy = higher coordination risk.

    Algorithm:
        1. Lowercase the topic
        2. Find all matching entropy signals
        3. Return the maximum match (worst-case entropy)
           rather than average ? conservative risk posture.

    Returns:
        float in [0.0, 1.0]

    Examples:
        topic_entropy_score("NVIDIA earnings Q4 2025")   -> 0.85
        topic_entropy_score("AI regulation European Union") -> 0.30
        topic_entropy_score("quantum computing research")   -> 0.30
    """
    lower = topic.lower()
    matched = [
        score for keyword, score in ENTROPY_SIGNALS
        if keyword in lower
    ]
    if not matched:
        return 0.5  # unknown topic ? neutral assumption
    return max(matched)


def batch_entropy_scores(topics: List[str]) -> List[float]:
    """Compute entropy scores for a list of topics."""
    return [topic_entropy_score(t) for t in topics]


def entropy_cluster_factor(entropy_scores: List[float]) -> float:
    """
    EC component for CSI formula.

    If violations are randomly distributed ? EC close to 1.0
    If violations cluster on high-entropy topics ? slight penalty

    Formula:
        mean_entropy = average entropy of all sessions in window
        EC = 1 - (mean_entropy * CLUSTER_WEIGHT)

    CLUSTER_WEIGHT = 0.10 ? conservative, one entropy cluster
    shouldn't crash the whole index.

    Returns float in [0.75, 1.0]
    """
    if not entropy_scores:
        return 1.0
    CLUSTER_WEIGHT = 0.10
    mean_e = sum(entropy_scores) / len(entropy_scores)
    return max(0.75, 1.0 - mean_e * CLUSTER_WEIGHT)
