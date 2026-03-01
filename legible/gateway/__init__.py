"""
legible/gateway/__init__.py

Legible Coordination Gateway — V2 Shadow Mode
"""
from .engine import CoordinationEngine
from .models import SessionReport, RoutingRequest, RoutingRecommendation

__all__ = ["CoordinationEngine", "SessionReport", "RoutingRequest", "RoutingRecommendation"]