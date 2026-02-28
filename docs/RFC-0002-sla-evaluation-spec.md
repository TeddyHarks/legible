# RFC-0002 ? SLA Evaluation Specification
**Status:** Canonical  
**Version:** 1.0.0  
**Repository:** legible-protocol  
**Implements:** RFC-0001 ?1, ?5, ?7  

---

## Abstract

This document defines the complete, language-neutral specification for
SLA session evaluation in the Legible Protocol. All conformant
implementations MUST produce identical outputs for identical inputs.

This document is the authoritative source. In any divergence between
this document and an implementation, this document governs.

---

## 1. Constants

All implementations MUST use these exact values.
A change to any constant increments the spec MINOR version.

```
VIOLATION_RATIO_PASS_THRESHOLD  = 0.0
ACS_SHARED_THRESHOLD            = 0.65
MIN_CALLS_FOR_ATTRIBUTION       = 3
THIN_EVIDENCE_DISCOUNT          = 0.5
STRICTNESS_MIN                  = 0.1
STRICTNESS_MAX                  = 10.0
STAKE_RATIO_MIN                 = 0.0
STAKE_RATIO_MAX                 = 1.0
STAKE_RATIO_TOLERANCE           = 0.001
```

---

## 2. Input Schema

### 2.1 SlaIntent

```
SlaIntent {
  declared_var:          u64     # MUST be > 0
  latency_ms:            u64     # MUST be > 0
  strictness_multiplier: f64     # Clamped to [STRICTNESS_MIN, STRICTNESS_MAX]
  dependency_depth:      u32     # Logged only. Not used in v1 computation.
  exposure_time_ms:      u64     # Logged only. Not used in v1 computation.
  provider_stake_ratio:  f64     # Clamped to [STAKE_RATIO_MIN, STAKE_RATIO_MAX]
  caller_stake_ratio:    f64     # Clamped to [STAKE_RATIO_MIN, STAKE_RATIO_MAX]
}
```

Constraint: `|provider_stake_ratio + caller_stake_ratio - 1.0| ? STAKE_RATIO_TOLERANCE`

### 2.2 SlaProviderAttestation

```
SlaProviderAttestation {
  call_id:            string   # UUID. Unique per call.
  timestamp:          u64      # Unix timestamp in milliseconds.
  request_hash:       string   # SHA-256 hex of serialized request.
  response_hash:      string   # SHA-256 hex of serialized response.
  latency_ms:         u64      # Observed round-trip latency.
  status_code:        u16      # HTTP or protocol status code.
  correctness_passed: bool     # Satisfies declared correctness rule.
}
```

### 2.3 SlaCallerAttestation

```
SlaCallerAttestation {
  call_id:               string   # Matches provider attestation call_id.
  request_well_formed:   bool     # Request conformed to declared SLA format.
  declared_var_mismatch: bool     # Declared VAR inconsistent with actual risk.
}
```

---

## 3. Output Schema

```
SlaEvaluationResult {
  outcome:                     ResolveOutcome   # See RFC-0004
  provider_slash:              u64
  caller_slash:                u64
  violation_count:             u32
  total_calls:                 u32
  latency_violation_count:     u32
  correctness_violation_count: u32
  caller_fault_count:          u32
  attribution_confidence:      f64              # In [0.0, 1.0]
  reason_summary:              string
}
```

---

## 4. Intent Validation

Validate in this exact order. Return error on first failure.

```
1. |provider_stake_ratio + caller_stake_ratio - 1.0| > STAKE_RATIO_TOLERANCE
   ? Error: "stake ratios must sum to 1.0"

2. strictness_multiplier ? 0.0
   ? Error: "strictness_multiplier must be positive"

3. latency_ms == 0
   ? Error: "latency_ms must be non-zero"

4. declared_var == 0
   ? Error: "declared_var must be > 0"
```

---

## 5. Evaluation Pipeline

Execute steps in order. No step may be skipped or reordered.

### Step 0: Empty Bundle Guard

```
IF len(provider_calls) == 0:
  RETURN SlaEvaluationResult {
    outcome = SlaPass,
    all numeric fields = 0,
    attribution_confidence = 1.0,
    reason_summary = "No calls recorded. Session passes vacuously."
  }
```

### Step 1: Count Provider Violations

```
latency_violations     = COUNT(c WHERE c.latency_ms > intent.latency_ms)
correctness_violations = COUNT(c WHERE c.correctness_passed == false)
provider_violation_count = latency_violations + correctness_violations
total_calls = len(provider_calls)
```

### Step 2: Count Caller Faults

```
IF len(caller_calls) == 0:
  caller_fault_count = 0
ELSE:
  caller_fault_count = COUNT(c WHERE NOT c.request_well_formed
                                  OR c.declared_var_mismatch)
```

### Step 3: Pass Check

```
total_fault_signals = provider_violation_count + caller_fault_count

IF total_fault_signals == 0:
  RETURN SlaEvaluationResult {
    outcome = SlaPass,
    provider_slash = 0,
    caller_slash = 0,
    violation_count = 0,
    total_calls = total_calls,
    latency_violation_count = latency_violations,
    correctness_violation_count = correctness_violations,
    caller_fault_count = 0,
    attribution_confidence = 1.0,
    reason_summary = "All {N} calls passed SLA. No violations detected."
  }
```

### Step 4: Base Slash

```
violation_ratio = provider_violation_count / total_calls   # float division
strictness      = CLAMP(strictness_multiplier, STRICTNESS_MIN, STRICTNESS_MAX)
base_slash      = FLOOR(declared_var ? violation_ratio ? strictness)
```

### Step 5: Attribution Confidence

```
provider_weight = provider_violation_count / total_fault_signals  # float
caller_weight   = caller_fault_count       / total_fault_signals  # float
raw_confidence  = ABS(provider_weight - caller_weight)

IF total_calls < MIN_CALLS_FOR_ATTRIBUTION:
  confidence = raw_confidence ? THIN_EVIDENCE_DISCOUNT
ELSE:
  confidence = raw_confidence
```

### Step 6: Slash Split

```
provider_ratio = CLAMP(provider_stake_ratio, STAKE_RATIO_MIN, STAKE_RATIO_MAX)
caller_ratio   = CLAMP(caller_stake_ratio,   STAKE_RATIO_MIN, STAKE_RATIO_MAX)

provider_slash = FLOOR(base_slash ? provider_ratio ? provider_weight)
caller_slash   = FLOOR(base_slash ? caller_ratio   ? caller_weight)
```

### Step 7: Outcome Selection

```
IF confidence < ACS_SHARED_THRESHOLD:
  outcome = SlaSharedSlash { provider_amount, caller_amount }

ELSE IF provider_weight ? caller_weight:
  IF caller_slash == 0:
    outcome = SlaSlashProvider { amount: provider_slash }
  ELSE:
    outcome = SlaSharedSlash { provider_amount, caller_amount }

ELSE:
  IF provider_slash == 0:
    outcome = SlaSlashCaller { amount: caller_slash }
  ELSE:
    outcome = SlaSharedSlash { provider_amount, caller_amount }
```

### Step 8: Reason Summary

```
reason_summary = "{LABEL} | calls={N} provider_violations={V}
  (latency={L} correctness={C}) caller_faults={F}
  provider_slash={PS} caller_slash={CS} confidence={CONF:.2f}"
```

---

## 6. Rounding Policy

- All final integer outputs: **FLOOR only**
- All intermediate computation: **64-bit IEEE 754 float**
- All division: **float division** (never integer division)
- Python: `math.floor()`
- Rust: `as u64` after float computation

---

## 7. Canonical Serialization

SlaIntent JSON serialization for hashing and kernel submission:

1. Sort all keys alphabetically (recursive for nested objects)
2. No whitespace
3. UTF-8 encoded

Python: `json.dumps(d, sort_keys=True, separators=(',', ':'))`  
Rust: Serialize from `BTreeMap` to ensure key ordering

---

## 8. Reference Test Vectors

All conformant implementations MUST pass these vectors exactly.
Float fields compared to 4 decimal places. Integer fields exact.

### Vector 1: Empty Bundle
```
Input:  declared_var=1000, latency_ms=200, strictness=1.5,
        provider_ratio=0.9, caller_ratio=0.1, provider_calls=[], caller_calls=[]
Output: outcome=SlaPass, provider_slash=0, total_calls=0, confidence=1.0
```

### Vector 2: All Pass
```
Input:  (same intent), calls=[100ms?, 150ms?, 199ms?]
Output: outcome=SlaPass, violation_count=0, total_calls=3
```

### Vector 3: Single Latency Violation, 4 Calls
```
Input:  calls=[300ms?, 100ms?, 100ms?, 100ms?], caller=[clean]
Output: outcome=SlaSlashProvider{337}, confidence=1.0
Trace:  violation_ratio=0.25, base=FLOOR(1000?0.25?1.5)=375,
        provider_slash=FLOOR(375?0.9?1.0)=337
```

### Vector 4: Thin Evidence (1 call)
```
Input:  calls=[400ms?], caller=[clean]
Output: outcome=SlaSharedSlash{provider=1350, caller=0}, confidence=0.5
Trace:  base=1500, raw_conf=1.0, discounted=0.5 < 0.65 ? forced shared
```

### Vector 5: Mixed Fault (5 calls)
```
Input:  calls=[300ms?, 100ms??4], caller=[malformed]
Output: outcome=SlaSharedSlash{provider=135, caller=15}, confidence=0.0
Trace:  provider_weight=0.5, caller_weight=0.5, conf=0.0
        provider_slash=FLOOR(300?0.9?0.5)=135
        caller_slash=FLOOR(300?0.1?0.5)=15
```

### Vector 6: Severity Helper
```
severity(337, 1000) = 0.337
severity(0, 1000)   = 0.0
severity(1000, 1000) = 1.0
severity(2000, 1000) = 1.0  (clamped)
severity(500, 0)    = 0.0   (zero denominator guard)
```

### Vector 7: Intent Validation
```
valid(ratio=0.9+0.1)  ? Ok
invalid(ratio=0.8+0.8) ? Err
invalid(strictness=0)  ? Err
invalid(latency=0)     ? Err
invalid(var=0)         ? Err
```

---

## Version History

| Version | Change |
|---------|--------|
| 1.0.0   | Initial canonical specification |
