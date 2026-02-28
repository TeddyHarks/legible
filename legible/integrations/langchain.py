"""
substralink/integrations/langchain.py

LangChain integration for SubstraLink SLA.

Wraps LangChain tools and chains so every call is automatically
tracked in an active Session.

Usage:
    from substralink.integrations.langchain import SlaTrackedTool

    tool = SlaTrackedTool(base_tool=my_langchain_tool, session=session)
    result = tool.run("query")
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.tools import BaseTool
except ImportError:
    raise ImportError(
        "langchain-core is required for this integration. "
        "Install with: pip install substralink-sla[langchain]"
    )

from ..sla.session import Session


class SlaTrackedTool(BaseTool):
    """
    A LangChain tool wrapper that tracks every invocation as a
    SubstraLink SLA call within an active Session.

    Drop-in replacement for any BaseTool. All tool behavior is
    preserved; evidence collection is transparent.

    Example:
        sla = SlaIntent(declared_var=500, latency_ms=300, ...)

        with Session(caller_id="agent", provider_id="search_tool", sla=sla) as session:
            tool = SlaTrackedTool(base_tool=search_tool, session=session)
            result = tool.run("latest AI papers")
    """

    base_tool: Any       # The wrapped BaseTool
    session: Any         # The active Session

    class Config:
        arbitrary_types_allowed = True

    @property
    def name(self) -> str:
        return self.base_tool.name

    @property
    def description(self) -> str:
        return self.base_tool.description

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        return self.session.track_call(self.base_tool._run, *args, **kwargs)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        # V1: wrap async as sync. Full async support in V2.
        return self._run(*args, **kwargs)


def tracked_tool(session: Session, tool: BaseTool) -> SlaTrackedTool:
    """Convenience constructor for SlaTrackedTool."""
    return SlaTrackedTool(base_tool=tool, session=session)
