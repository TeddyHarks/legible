"""
tests/test_evaluator.py

Unit tests for the SLA evaluation engine.
Tests mirror the Rust test suite and validate all reference vectors
from SLA_EVAL_SPEC_v1.0.0.

Run: pytest tests/test_evaluator.py -v
"""

import math
import pytest

from substralink.sla.evaluator import (
    ACS_SHARED_THRESHOLD,
    MIN_CALLS_FOR_ATTRIBUTION,
    SlaAttestationBundle,
    SlaCallerAttestation,
    SlaEvaluationResult,
    SlaIntent,
    SlaProviderAttestation,
    OutcomeType,
    evaluate_sla,
    severity_from_slash,
    validate_intent,
)


# ??? Fixtures ?????????????????????????????????????????????????????????????????

@pytest.fixture
def base_intent() -> SlaIntent:
    return SlaIntent(
        declared_var=1000,
        latency_ms=200,
        strictness_multiplier=1.5,
        dependency_depth=2,
        exposure_time_ms=5000,
        provider_stake_ratio=0.9,
        caller_stake_ratio=0.1,
    )


def passing_call(latency: int = 100) -> SlaProviderAttestation:
    return SlaProviderAttestation(
        call_id="test-call",
        timestamp=0,
        request_hash="aaa",
        response_hash="bbb",
        latency_ms=latency,
        status_code=200,
        correctness_passed=True,
    )


def failing_latency_call(latency: int = 300) -> SlaProviderAttestation:
    p = passing_call(latency)
    return p


def failing_correctness_call() -> SlaProviderAttestation:
    return SlaProviderAttestation(
        call_id="test-call",
        timestamp=0,
        request_hash="aaa",
        response_hash="bbb",
        latency_ms=100,
        status_code=200,
        correctness_passed=False,
    )


def clean_caller(call_id: str = "test-call") -> SlaCallerAttestation:
    return SlaCallerAttestation(
        call_id=call_id, request_well_formed=True, declared_var_mismatch=False
    )


def faulty_caller(call_id: str = "test-call") -> SlaCallerAttestation:
    return SlaCallerAttestation(
        call_id=call_id, request_well_formed=False, declared_var_mismatch=False
    )


# ??? Reference Vector Tests (spec ?8) ????????????????????????????????????????

class TestReferenceVectors:
    """All 7 reference vectors from SLA_EVAL_SPEC_v1.0.0 ?8."""

    def test_vector_1_empty_bundle(self, base_intent):
        r = evaluate_sla(base_intent, SlaAttestationBundle())
        assert r.outcome.outcome_type == OutcomeType.SLA_PASS
        assert r.provider_slash == 0
        assert r.caller_slash == 0
        assert r.total_calls == 0
        assert r.attribution_confidence == 1.0

    def test_vector_2_all_pass(self, base_intent):
        bundle = SlaAttestationBundle(provider_calls=[
            passing_call(100), passing_call(150), passing_call(199),
        ])
        r = evaluate_sla(base_intent, bundle)
        assert r.outcome.outcome_type == OutcomeType.SLA_PASS
        assert r.violation_count == 0
        assert r.total_calls == 3
        assert r.provider_slash == 0

    def test_vector_3_single_latency_violation(self, base_intent):
        """1 of 4 calls exceeds latency. Clean caller. Expect SlaSlashProvider{337}."""
        calls = [
            SlaProviderAttestation("c1", 0, "a", "b", 300, 200, True),
            SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
            SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
            SlaProviderAttestation("c4", 0, "a", "b", 100, 200, True),
        ]
        bundle = SlaAttestationBundle(
            provider_calls=calls,
            caller_calls=[SlaCallerAttestation("c1", True, False)],
        )
        r = evaluate_sla(base_intent, bundle)
        assert r.outcome.outcome_type == OutcomeType.SLA_SLASH_PROVIDER
        assert r.outcome.amount == 337
        assert r.provider_slash == 337
        assert r.caller_slash == 0
        assert r.violation_count == 1
        assert r.total_calls == 4
        assert abs(r.attribution_confidence - 1.0) < 0.0001

    def test_vector_4_thin_evidence(self, base_intent):
        """1 call, 1 violation. Thin evidence ? SlaSharedSlash."""
        bundle = SlaAttestationBundle(
            provider_calls=[SlaProviderAttestation("c1", 0, "a", "b", 400, 200, True)],
            caller_calls=[SlaCallerAttestation("c1", True, False)],
        )
        r = evaluate_sla(base_intent, bundle)
        assert r.outcome.outcome_type == OutcomeType.SLA_SHARED_SLASH
        assert r.outcome.provider_amount == 1350
        assert r.outcome.caller_amount == 0
        assert abs(r.attribution_confidence - 0.5) < 0.0001

    def test_vector_5_mixed_fault(self, base_intent):
        """1 provider violation + 1 caller fault in 5 calls. Confidence=0 ? SlaSharedSlash."""
        calls = [
            SlaProviderAttestation("c1", 0, "a", "b", 300, 200, True),
            SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
            SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
            SlaProviderAttestation("c4", 0, "a", "b", 100, 200, True),
            SlaProviderAttestation("c5", 0, "a", "b", 100, 200, True),
        ]
        bundle = SlaAttestationBundle(
            provider_calls=calls,
            caller_calls=[SlaCallerAttestation("c1", False, False)],
        )
        r = evaluate_sla(base_intent, bundle)
        assert r.outcome.outcome_type == OutcomeType.SLA_SHARED_SLASH
        assert r.outcome.provider_amount == 135
        assert r.outcome.caller_amount == 15
        assert abs(r.attribution_confidence - 0.0) < 0.0001

    def test_vector_6_severity(self):
        assert abs(severity_from_slash(337, 1000) - 0.337) < 0.0001
        assert severity_from_slash(0, 1000) == 0.0
        assert severity_from_slash(1000, 1000) == 1.0
        assert severity_from_slash(2000, 1000) == 1.0   # clamped
        assert severity_from_slash(500, 0) == 0.0        # zero denominator

    def test_vector_7_intent_validation(self, base_intent):
        validate_intent(base_intent)  # should not raise

        with pytest.raises(ValueError, match="sum to 1.0"):
            validate_intent(SlaIntent(1000, 200, 1.5, 2, 5000, 0.8, 0.8))

        with pytest.raises(ValueError, match="strictness"):
            validate_intent(SlaIntent(1000, 200, 0.0, 2, 5000, 0.9, 0.1))

        with pytest.raises(ValueError, match="latency"):
            validate_intent(SlaIntent(1000, 0, 1.5, 2, 5000, 0.9, 0.1))

        with pytest.raises(ValueError, match="declared_var"):
            validate_intent(SlaIntent(0, 200, 1.5, 2, 5000, 0.9, 0.1))


# ??? Evaluation Logic Tests ???????????????????????????????????????????????????

class TestEvaluationLogic:

    def test_deterministic_same_inputs_same_output(self, base_intent):
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 300, 200, True),
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c4", 0, "a", "b", 100, 200, True),
            ],
        )
        r1 = evaluate_sla(base_intent, bundle)
        r2 = evaluate_sla(base_intent, bundle)
        assert r1.provider_slash == r2.provider_slash
        assert r1.caller_slash == r2.caller_slash
        assert r1.violation_count == r2.violation_count
        assert r1.outcome == r2.outcome

    def test_correctness_violation(self, base_intent):
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 100, 200, False),  # correctness fail
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
            ],
        )
        r = evaluate_sla(base_intent, bundle)
        assert r.correctness_violation_count == 1
        assert r.provider_slash > 0

    def test_both_violations_counted(self, base_intent):
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 300, 200, False),  # both
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
            ],
        )
        r = evaluate_sla(base_intent, bundle)
        assert r.latency_violation_count == 1
        assert r.correctness_violation_count == 1
        assert r.violation_count == 2  # both counted independently

    def test_base_slash_uses_floor(self, base_intent):
        """Verify FLOOR not ROUND: 1/3 violation ratio should produce floored result."""
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 300, 200, True),
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
            ],
        )
        r = evaluate_sla(base_intent, bundle)
        # violation_ratio = 1/3, base = floor(1000 * 1/3 * 1.5) = floor(500.0) = 500
        expected_base = math.floor(1000 * (1/3) * 1.5)
        expected_provider = math.floor(expected_base * 0.9 * 1.0)
        assert r.provider_slash == expected_provider

    def test_thin_evidence_discount_applied(self, base_intent):
        """Sessions with < MIN_CALLS_FOR_ATTRIBUTION calls get halved confidence."""
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 400, 200, True),
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
            ],
            # 2 calls < MIN_CALLS_FOR_ATTRIBUTION=3
        )
        r = evaluate_sla(base_intent, bundle)
        # raw_confidence = 1.0 (all provider), discounted = 0.5
        assert abs(r.attribution_confidence - 0.5) < 0.0001
        # 0.5 < ACS_SHARED_THRESHOLD=0.65 ? forced shared
        assert r.outcome.outcome_type == OutcomeType.SLA_SHARED_SLASH

    def test_caller_only_faults_no_economic_slash(self, base_intent):
        """Pure caller faults generate risk signals but no economic slash (V1 design)."""
        calls = [passing_call() for _ in range(5)]
        call_ids = [c.call_id for c in calls]
        bundle = SlaAttestationBundle(
            provider_calls=calls,
            caller_calls=[
                SlaCallerAttestation(cid, False, False) for cid in call_ids
            ],
        )
        r = evaluate_sla(base_intent, bundle)
        # Provider performed perfectly ? base_slash = 0
        assert r.provider_slash == 0
        assert r.caller_slash == 0
        assert r.caller_fault_count == 5

    def test_strictness_multiplier_amplifies_slash(self, base_intent):
        """Higher strictness ? higher slash for same violation."""
        bundle = SlaAttestationBundle(
            provider_calls=[
                SlaProviderAttestation("c1", 0, "a", "b", 300, 200, True),
                SlaProviderAttestation("c2", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c3", 0, "a", "b", 100, 200, True),
                SlaProviderAttestation("c4", 0, "a", "b", 100, 200, True),
            ],
        )
        r1 = evaluate_sla(base_intent, bundle)  # strictness=1.5

        strict_intent = SlaIntent(
            declared_var=1000, latency_ms=200, strictness_multiplier=3.0,
            dependency_depth=2, exposure_time_ms=5000,
            provider_stake_ratio=0.9, caller_stake_ratio=0.1,
        )
        r2 = evaluate_sla(strict_intent, bundle)
        assert r2.provider_slash > r1.provider_slash

    def test_outcome_equality(self):
        from substralink.sla.evaluator import ResolveOutcome, OutcomeType
        assert ResolveOutcome.sla_pass() == ResolveOutcome.sla_pass()
        assert ResolveOutcome.sla_slash_provider(100) == ResolveOutcome.sla_slash_provider(100)
        assert ResolveOutcome.sla_slash_provider(100) != ResolveOutcome.sla_slash_provider(200)
        assert ResolveOutcome.sla_shared_slash(100, 10) == ResolveOutcome.sla_shared_slash(100, 10)
        assert ResolveOutcome.sla_pass() != ResolveOutcome.sla_slash_provider(0)


# ??? Intent Serialization Tests ???????????????????????????????????????????????

class TestIntentSerialization:

    def test_canonical_json_keys_sorted(self):
        from substralink.sla.intent import SlaIntent as PydanticIntent
        intent = PydanticIntent(
            caller_id="agent_a",
            provider_id="service_b",
            declared_var=1000,
            latency_ms=200,
            correctness_rule="valid_json",
            strictness_multiplier=1.5,
        )
        canonical = intent.to_canonical_json()
        import json
        parsed = json.loads(canonical)
        keys = list(parsed.keys())
        assert keys == sorted(keys), "Keys must be sorted alphabetically"

    def test_canonical_json_no_whitespace(self):
        from substralink.sla.intent import SlaIntent as PydanticIntent
        intent = PydanticIntent(
            caller_id="a", provider_id="b",
            declared_var=100, latency_ms=100,
            correctness_rule="valid_json",
        )
        canonical = intent.to_canonical_json()
        assert " " not in canonical
        assert "\n" not in canonical
