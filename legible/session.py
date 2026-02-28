"""
substralink/sla/session.py

Session ? the primary developer-facing primitive.

A Session represents a single accountable coordination event between
a caller agent and a provider agent. It wraps API calls, collects
cryptographic evidence, evaluates SLA compliance deterministically,
and submits an immutable resolution to the SubstraLink kernel.

Usage (context manager ? recommended):

    with Session(caller_id="agent_a", provider_id="svc_b", sla=sla) as s:
        result = s.track_call(my_api_fn, payload)
    # Auto-finalizes on exit. s.result contains the EvidencePacket.

Usage (manual):

    s = Session(caller_id="agent_a", provider_id="svc_b", sla=sla)
    s.start()
    result = s.track_call(my_api_fn, payload)
    packet = s.finalize()
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, TypeVar

from .attestation import (
    CallerAttestation,
    ProviderAttestation,
    evaluate_correctness,
)
from .evaluator import (
    SlaAttestationBundle,
    SlaCallerAttestation,
    SlaProviderAttestation,
    evaluate_sla,
)
from .evidence import EvidencePacket
from .intent import SlaIntent
from .client.kernel import KernelClient, get_client

F = TypeVar("F", bound=Callable[..., Any])


class SessionState:
    PENDING = "pending"
    ACTIVE = "active"
    FINALIZED = "finalized"
    FAILED = "failed"


class SessionError(Exception):
    pass


class Session:
    """
    A single accountable coordination session between a caller and provider.

    Lifecycle:
      pending ? active (after start() or __enter__)
      active  ? finalized (after finalize() or __exit__)
      active  ? failed (on unhandled exception in context manager)
    """

    def __init__(
        self,
        caller_id: str,
        provider_id: str,
        sla: SlaIntent,
        session_id: str | None = None,
        client: KernelClient | None = None,
        shadow_mode: bool = True,
        context_id: str | None = None,
    ) -> None:
        """
        Args:
            caller_id:    Identifier for the agent initiating the session.
            provider_id:  Identifier for the agent/service being called.
            sla:          SLA contract for this session.
            session_id:   Optional override. Auto-generated if not provided.
            client:       Kernel client. Uses module default if not provided.
            shadow_mode:  If True, collateral is simulated (no real funds).
                          Always True in V1.
            context_id:   SubstraLink context for this session. Defaults to
                          "{caller_id}:{provider_id}".
        """
        self.session_id = session_id or str(uuid.uuid4())
        self.caller_id = caller_id.lower()
        self.provider_id = provider_id.lower()
        self.sla = sla
        self.client = client or get_client()
        self.shadow_mode = shadow_mode
        self.context_id = context_id or f"{self.caller_id}:{self.provider_id}"

        self._state = SessionState.PENDING
        self._decision_id: str | None = None
        self._provider_attestations: list[ProviderAttestation] = []
        self._caller_attestations: list[CallerAttestation] = []
        self.result: EvidencePacket | None = None

    # ?? Lifecycle ?????????????????????????????????????????????????????????????

    def start(self) -> "Session":
        """
        Commits the session to the SubstraLink kernel as a Decision.
        Must be called before track_call().
        """
        if self._state != SessionState.PENDING:
            raise SessionError(f"Cannot start session in state '{self._state}'")

        decision = self.client.commit_decision(
            context_id=self.context_id,
            proposer_id=self.caller_id,
            intent=self.sla.to_kernel_intent(),
        )
        self._decision_id = decision["id"]
        self._state = SessionState.ACTIVE
        return self

    def track_call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Wraps a single API call with evidence collection.

        Records:
          - Request hash (SHA-256 of serialized args/kwargs)
          - Response hash (SHA-256 of serialized response)
          - Latency in milliseconds
          - HTTP status code (if response has .status_code)
          - Correctness evaluation against declared SLA rule

        Returns the original response unmodified.
        """
        if self._state != SessionState.ACTIVE:
            raise SessionError(
                f"Cannot track calls in state '{self._state}'. Call start() first."
            )

        request_payload = {"args": args, "kwargs": kwargs}
        t0 = time.monotonic()

        try:
            response = fn(*args, **kwargs)
        except Exception as exc:
            # Treat exceptions as correctness failures with max latency
            latency = int((time.monotonic() - t0) * 1000)
            attestation = ProviderAttestation.build(
                request=request_payload,
                response={"error": str(exc)},
                latency_ms=latency,
                status_code=500,
                correctness_passed=False,
            )
            self._record_attestation(attestation, request_payload)
            raise

        latency = int((time.monotonic() - t0) * 1000)
        status_code = getattr(response, "status_code", 200)
        correctness_passed = evaluate_correctness(response, self.sla.correctness_rule)

        attestation = ProviderAttestation.build(
            request=request_payload,
            response=response,
            latency_ms=latency,
            status_code=status_code,
            correctness_passed=correctness_passed,
        )
        self._record_attestation(attestation, request_payload)
        return response

    def track(self, fn: F) -> F:
        """
        Decorator form of track_call for use with pre-defined tool functions.

        Example:
            @session.track
            def call_market_api(payload):
                return requests.post(url, json=payload).json()
        """
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.track_call(fn, *args, **kwargs)
        wrapper.__name__ = fn.__name__  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    def finalize(self) -> EvidencePacket:
        """
        Evaluates the session, submits the resolution to the kernel,
        and returns the evidence packet.

        After finalize():
          - Session state is FINALIZED
          - self.result contains the EvidencePacket
          - The resolution is immutably committed to SubstraLink
        """
        if self._state != SessionState.ACTIVE:
            raise SessionError(f"Cannot finalize session in state '{self._state}'")
        if not self._decision_id:
            raise SessionError("Session not started. Call start() first.")

        # Build evaluator input types from collected attestations
        provider_eval_calls = [
            SlaProviderAttestation(
                call_id=a.call_id,
                timestamp=a.timestamp,
                request_hash=a.request_hash,
                response_hash=a.response_hash,
                latency_ms=a.latency_ms,
                status_code=a.status_code,
                correctness_passed=a.correctness_passed,
            )
            for a in self._provider_attestations
        ]

        caller_eval_calls = [
            SlaCallerAttestation(
                call_id=c.call_id,
                request_well_formed=c.request_well_formed,
                declared_var_mismatch=c.declared_var_mismatch,
            )
            for c in self._caller_attestations
        ]

        # Build evaluator intent
        from .evaluator import SlaIntent as EvalIntent
        eval_intent = EvalIntent(
            declared_var=self.sla.declared_var,
            latency_ms=self.sla.latency_ms,
            strictness_multiplier=self.sla.strictness_multiplier,
            dependency_depth=self.sla.dependency_depth,
            exposure_time_ms=self.sla.exposure_time_ms,
            provider_stake_ratio=self.sla.provider_stake_ratio,
            caller_stake_ratio=self.sla.caller_stake_ratio,
        )

        bundle = SlaAttestationBundle(
            provider_calls=provider_eval_calls,
            caller_calls=caller_eval_calls,
        )

        # Deterministic evaluation (mirrors kernel-side recomputation)
        evaluation = evaluate_sla(eval_intent, bundle)

        # Submit resolution proposal to kernel
        # Kernel independently recomputes and verifies before committing.
        self.client.submit_resolution(
            decision_id=self._decision_id,
            resolver_id="substralink.sla.evaluator",
            reason=evaluation.reason_summary,
        )

        # Build evidence packet
        packet = EvidencePacket.build(
            session_id=self.session_id,
            decision_id=self._decision_id,
            caller_id=self.caller_id,
            provider_id=self.provider_id,
            intent=self.sla,
            provider_attestations=self._provider_attestations,
            caller_attestations=self._caller_attestations,
            evaluation=evaluation,
        )

        self.result = packet
        self._state = SessionState.FINALIZED
        return packet

    # ?? Context manager ???????????????????????????????????????????????????????

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            # Exception in the with block ? finalize anyway to record partial evidence
            self._state = SessionState.ACTIVE  # ensure finalize() can run
            try:
                self.finalize()
            except Exception:
                self._state = SessionState.FAILED
            return False  # Re-raise the original exception
        self.finalize()
        return False

    # ?? Internal ??????????????????????????????????????????????????????????????

    def _record_attestation(
        self,
        attestation: ProviderAttestation,
        request_payload: dict[str, Any],
    ) -> None:
        """Logs attestation locally and submits to kernel."""
        self._provider_attestations.append(attestation)

        # Auto-generate caller attestation for V1
        caller_att = CallerAttestation(
            call_id=attestation.call_id,
            request_well_formed=True,   # V1: always well-formed unless overridden
            declared_var_mismatch=False,
        )
        self._caller_attestations.append(caller_att)

        # Submit provider evidence to kernel
        self.client.submit_attestation(
            decision_id=self._decision_id,  # type: ignore[arg-type]
            actor_id=self.provider_id,
            approve=attestation.correctness_passed,
            reasoning=attestation.to_reasoning_json(),
        )

    @property
    def state(self) -> str:
        return self._state

    @property
    def call_count(self) -> int:
        return len(self._provider_attestations)

    def __repr__(self) -> str:
        return (
            f"Session(id={self.session_id!r}, "
            f"caller={self.caller_id!r}, "
            f"provider={self.provider_id!r}, "
            f"state={self._state!r}, "
            f"calls={self.call_count})"
        )
