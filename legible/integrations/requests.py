"""
substralink/integrations/requests.py

Raw HTTP wrapper for SubstraLink SLA.

Usage:
    from substralink.integrations.requests import tracked_request

    with Session(caller_id="agent", provider_id="market-api", sla=sla) as session:
        response = tracked_request(session, "POST", "https://api.example.com/order", json=payload)
"""

from __future__ import annotations

from typing import Any

import httpx

from ..sla.session import Session


def tracked_request(
    session: Session,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """
    Makes an HTTP request tracked as a SubstraLink SLA call.

    Uses httpx under the hood. All kwargs are passed through to httpx.request().
    Returns the raw httpx.Response.

    The response is evaluated for correctness using the session's
    declared correctness_rule against the response body.
    """
    def _call(m: str, u: str, **kw: Any) -> httpx.Response:
        return httpx.request(m, u, **kw)

    return session.track_call(_call, method, url, **kwargs)
