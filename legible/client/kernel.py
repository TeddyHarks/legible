"""
substralink/client/kernel.py

Thin HTTP client for the SubstraLink kernel API.

Wraps the four kernel endpoints the SLA SDK needs:
  POST /decisions/commit       ? start_decision()
  POST /decisions/:id/attest   ? submit_attestation()
  POST /decisions/:id/resolve  ? submit_resolution()
  GET  /proofs/:id             ? get_proof()

All methods are synchronous in V1. Async upgrade planned for V2.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class KernelClient:
    """
    HTTP client for the SubstraLink kernel.

    Configuration via environment variables or constructor args:
      SUBSTRALINK_KERNEL_URL   ? base URL (default: http://localhost:3000)
      SUBSTRALINK_API_KEY      ? optional API key for authenticated deployments
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("SUBSTRALINK_KERNEL_URL", "http://localhost:3000")
        ).rstrip("/")

        self.api_key = api_key or os.environ.get("SUBSTRALINK_API_KEY")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=self.timeout,
        )

    # ?? Decisions ?????????????????????????????????????????????????????????????

    def commit_decision(
        self,
        context_id: str,
        proposer_id: str,
        intent: str,
    ) -> dict[str, Any]:
        """
        POST /decisions/commit
        Creates a new Decision in the kernel. Returns the Decision object.
        """
        with self._client() as c:
            r = c.post("/decisions/commit", json={
                "context_id": context_id,
                "proposer_id": proposer_id,
                "intent": intent,
            })
            r.raise_for_status()
            return r.json()

    def submit_attestation(
        self,
        decision_id: str,
        actor_id: str,
        approve: bool,
        reasoning: str,
    ) -> None:
        """
        POST /decisions/:id/attest
        Appends an attestation to an open Decision.
        """
        with self._client() as c:
            r = c.post(f"/decisions/{decision_id}/attest", json={
                "actor_id": actor_id,
                "approve": approve,
                "reasoning": reasoning,
            })
            r.raise_for_status()

    def submit_resolution(
        self,
        decision_id: str,
        resolver_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        POST /decisions/:id/resolve
        Proposes a resolution. Kernel recomputes and verifies deterministically.
        Returns the committed Resolution object.
        """
        with self._client() as c:
            r = c.post(f"/decisions/{decision_id}/resolve", json={
                "resolver_id": resolver_id,
                "reason": reason,
            })
            r.raise_for_status()
            return r.json()

    def get_proof(self, decision_id: str) -> dict[str, Any]:
        """
        GET /proofs/:id
        Returns the exportable resolution proof for a decision.
        """
        with self._client() as c:
            r = c.get(f"/proofs/{decision_id}")
            r.raise_for_status()
            return r.json()

    def health(self) -> bool:
        """Returns True if the kernel is reachable."""
        try:
            with self._client() as c:
                r = c.get("/ledger/health")
                return r.status_code == 200
        except Exception:
            return False


# Module-level default client (configured from environment)
_default_client: KernelClient | None = None


def get_client() -> KernelClient:
    global _default_client
    if _default_client is None:
        _default_client = KernelClient()
    return _default_client


def configure(base_url: str, api_key: str | None = None) -> None:
    """Configure the module-level default client."""
    global _default_client
    _default_client = KernelClient(base_url=base_url, api_key=api_key)
