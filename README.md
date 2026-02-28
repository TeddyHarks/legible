# Legible

> Autonomous coordination, readable, verifiable, enforceable.

Built on [SubstraLink](https://github.com/substralink/kernel).  
Implements [Legible Protocol v1.0.0](https://github.com/substralink/legible-protocol).

---

## The Problem

When AI agents call other AI agents, things break.  
Latency spikes. Outputs fail. APIs return garbage.  
And nobody can prove whose fault it was.

There is no machine-native fault protocol.  
No session record. No attribution. No consequence.

## What Legible Does

Legible adds a coordination accountability layer to autonomous agent workflows:

- **Readable** ? every session produces a cryptographic evidence packet
- **Verifiable** ? outcomes are deterministically reproducible from raw evidence  
- **Enforceable** ? fault is attributed, consequences are proportional, records are immutable

## Quickstart

```python
from legible import Session, SlaIntent

sla = SlaIntent(
    caller_id="agent_alpha",
    provider_id="market_api",
    declared_var=5000,
    latency_ms=200,
    correctness_rule="valid_json",
    strictness_multiplier=1.5,
)

with Session(caller_id="agent_alpha", provider_id="market_api", sla=sla) as session:
    response = session.track_call(my_api_call, payload)

# Session auto-finalizes. Evidence committed to SubstraLink kernel.
print(session.result.evaluation)
```

## Protocol Conformance

Legible implements [Legible Protocol v1.0.0](https://github.com/substralink/legible-protocol).

- RFC-0001: Core Invariants ?
- RFC-0002: SLA Evaluation Specification ?  
- RFC-0003: Attribution Confidence ?
- RFC-0004: Resolution Outcomes ?
- RFC-0005: Evidence Packet Format ?
- RFC-0006: Legible Verified tier ?

Run reference vectors: `python legible/evaluator.py`

## Integrations

```python
from legible.integrations.langchain import tracked_tool
from legible.integrations.openai import tracked_openai_call
from legible.integrations.requests import tracked_request
```

## V1 / V2 Roadmap

**V1 (current):** Post-event accountability. Something broke ? here's the
cryptographic proof of what happened and whose fault it was.

**V2 (planned):** Coordinated fulfillment. Agents negotiate SLA commitments
across a coordination graph. Confidence degradation triggers rerouting
before failure occurs.

## License

Apache 2.0
