"""
substralink/integrations/openai.py

OpenAI integration for SubstraLink SLA.

Wraps the OpenAI client so every completion call is tracked
as a SubstraLink SLA attestation within an active Session.

Usage:
    from substralink.integrations.openai import tracked_openai_call

    with Session(caller_id="agent", provider_id="openai.gpt4o", sla=sla) as session:
        response = tracked_openai_call(
            session=session,
            client=openai_client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "Summarize this."}]
        )
"""

from __future__ import annotations

from typing import Any

from ..sla.session import Session


def tracked_openai_call(
    session: Session,
    client: Any,
    model: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> Any:
    """
    Wraps a single OpenAI chat completion call with SLA tracking.

    The response is evaluated for correctness using the session's
    declared correctness_rule. For OpenAI calls, "non_empty" or
    "valid_json" (if requesting JSON mode) are typical rules.

    Returns the raw OpenAI ChatCompletion response unmodified.
    """
    def _call(m: str, msgs: list, **kw: Any) -> Any:
        return client.chat.completions.create(model=m, messages=msgs, **kw)

    return session.track_call(_call, model, messages, **kwargs)
