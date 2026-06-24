# SHACKLE Protocol Specification — SP/1.0

## Runtime Circuit Breaker for Autonomous AI Agents

**Version:** 1.0.0  
**Status:** Published  
**Date:** 2026-06-25  
**Authors:** Dante Bullock, Sovereign Logic  
**License:** Creative Commons Attribution 4.0 International (CC-BY 4.0)  
**Reference Implementation:** <https://github.com/Fame510/SHACKLE-PRO->  
**First Public Commit:** 2026-06-17 23:12 UTC  

> **Implementations of this specification are subject to the SHACKLE license terms.**
> The reference implementation is dual-licensed: AGPLv3 (open source) and
> Commercial (proprietary). Contact: docspoc101@gmail.com

---

## Abstract

SHACKLE is a runtime circuit breaker for autonomous AI agents. It answers one question:

> **Should this agent be allowed to execute this tool with these parameters at this moment?**

The protocol defines a deterministic, verifiable decision function backed by 9 mathematical invariants, a language-agnostic message schema, Ed25519-signed append-only audit logging, and a Redis-backed distributed state engine. It operates as a sidecar daemon with gRPC/Unix socket transport, or as an in-process library for single-agent deployments.

SHACKLE is the **first open-source runtime circuit breaker for AI agents** with cryptographic audit chain-of-custody. This specification is the definitive reference for the SP/1.0 protocol.

---

## 1. Introduction

### 1.1 The Problem

Autonomous AI agents execute tools — web search, file I/O, API calls, code execution — with no runtime oversight. The framework's recursion limit or token cap is the only guardrail. When an agent enters a retry loop (same tool, same error, burning tokens each time), there is no mechanism to detect, intercept, and stop it before the wallet is empty.

This is not hypothetical. Production deployments have documented:
- Agent infinite loops consuming $6,000+ in API costs before the recursion limit fired
- Duplicate tool calls repeating 50+ times with no variation
- Spawned child processes hanging indefinitely while consuming tokens

The industry consensus — independently reached by multiple teams in June 2026 — is that **generation authority is not release authority.** The model generates candidates. A separate mediation layer must authorize execution.

SHACKLE is that mediation layer.

### 1.2 Design Principles

| Principle | Meaning |
|-----------|---------|
| **Deterministic core** | `decide(state, call) → Verdict` is a pure function. Same inputs always produce same outputs. |
| **Daemon as authority** | The SHACKLE daemon is the sole source of truth for time, state, and verdicts. Agents are untrusted. |
| **Append-only audit** | Every decision is Ed25519-signed and written to an immutable audit log. Chain-of-custody is cryptographically verifiable. |
| **Mathematically verified** | 9 invariant properties hold under all inputs, proven by property-based testing (Hypothesis, 500+ examples each). |
| **Graceful degradation** | Agents function in local/library mode without a daemon. Distributed state is an upgrade path. |
| **Fail-closed** | Network failure, daemon crash, or timeout → DENY. No execution without explicit authorization. |

### 1.3 Scope

This specification covers:
- The decision function and its 9 mathematical invariants (§3)
- Message schemas and semantics (§4)
- State model (§5)
- Transport bindings (§6)
- Audit and security (§7)
- Compliance framework (§8)

This specification does NOT cover:
- Daemon persistence layer (implementation detail)
- HITL console UI (presentation concern)
- Pricing or commercial terms

---

## 2. Architecture

### 2.1 Deployment Models

```
MODEL A — Library Mode (In-Process)
┌─────────────────────────┐
│  Agent Process          │
│  ┌───────────────────┐  │
│  │ @Guard decorator  │  │
│  │ Local state only  │  │
│  └───────────────────┘  │
└─────────────────────────┘

MODEL B — Sidecar Daemon (Production)
┌─────────────────┐     Unix/gRPC      ┌──────────────────────────┐
│  Agent Process  │ ◄────────────────► │  SHACKLE Daemon          │
│  ┌───────────┐  │   pre_exec         │  ┌────────────────────┐  │
│  │ Thin      │  │   post_exec        │  │ Policy Engine      │  │
│  │ Client    │  │   register         │  │ - Budgets          │  │
│  │ Shim      │  │   heartbeat        │  │ - Counters         │  │
│  └───────────┘  │                    │  │ - Circuit Breakers │  │
└─────────────────┘                    │  └────────────────────┘  │
                                        │  ┌────────────────────┐  │
                                        │  │ Audit Log          │  │
                                        │  │ Ed25519-signed     │  │
                                        │  │ Append-only        │  │
                                        │  │ Chain-linked       │  │
                                        │  └────────────────────┘  │
                                        └──────────────────────────┘

MODEL C — Distributed (Enterprise)
┌──────────┐  ┌──────────┐  ┌──────────┐
│ Agent A  │  │ Agent B  │  │ Agent C  │
└────┬─────┘  └────┬─────┘  └────┬─────┘
     └─────────────┬─────────────┘
                   │  gRPC/TLS
          ┌────────┴────────┐
          │  SHACKLE        │
          │  Daemon Cluster │
          │  Redis (state)  │
          │  Postgres (logs)│
          └─────────────────┘
```

### 2.2 Protocol Layers

```
┌──────────────────────────────────┐
│    Policy Language (future)      │  ← DSL for guard rules
├──────────────────────────────────┤
│    Decision Function             │  ← decide(state, call) → Verdict
├──────────────────────────────────┤
│    Message Protocol              │  ← This specification
├──────────────────────────────────┤
│    Transport (Unix/gRPC/WS)      │  ← Binding layer
└──────────────────────────────────┘
```

---

## 3. The Decision Function

### 3.1 Core Function

The decision function is the heart of SHACKLE. It is a pure function — no I/O, no side effects, no allocations in the hot path. It is human-auditable in under 10 minutes. It is under 200 lines of logic.

```
function decide(
    state: SessionState,
    call: ToolCall,
    config: GuardConfig,
    rng_float: float
) → Verdict
```

### 3.2 Decision Algorithm — 8 Stacked Layers

```
Layer 1: Circuit Breaker
    IF state.circuit_tripped:
        → DENY(CIRCUIT_OPEN)

Layer 2: Nonce Validation (Anti-Replay)
    IF call.nonce IN state.seen_nonces:
        → DENY(POLICY_VIOLATION)

Layer 3: Budget Guard
    IF config.budget_usd > 0:
        IF state.budget_remaining_usd <= 0:
            → DENY(BUDGET_EXHAUSTED)
        IF hitl_mode == ON_THRESHOLD AND fraction <= threshold:
            → HITL("budget threshold reached")
        IF call.cost > state.budget_remaining:
            IF hitl_mode IN (ON_DENY, ALWAYS):
                → HITL("cost exceeds remaining budget")
            → DENY(BUDGET_EXHAUSTED)

Layer 4: Repeat Call Guard
    IF config.max_repeat_calls > 0:
        IF is_repeat(call, state.last_call):
            limit = max_repeat_calls
            IF error_amplification AND has_error_signal(call):
                limit = max(1, limit - 1)
            IF repeat_count >= limit:
                → DENY(MAX_REPEAT_EXCEEDED)

Layer 5: Time Window Guard
    IF config.window_max_calls > 0:
        IF window_count >= window_max_calls:
            → DENY(WINDOW_EXCEEDED)

Layer 6: Global Call Limit
    IF config.max_total_calls > 0 AND total_calls >= max_total_calls:
        → DENY(GLOBAL_LIMIT)

Layer 7: Probabilistic Denial (Adversarial Hardening)
    IF probabilistic_deny AND budget_ratio < 0.2:
        IF rng < deny_jitter_ratio * (1.0 - budget_ratio):
            → DENY(BUDGET_EXHAUSTED, probabilistic=true)

Layer 8: HITL Always
    IF hitl_mode == ALWAYS:
        → HITL("HITL required for all calls")

Default:
    → ALLOW
```

### 3.3 Mathematical Invariants (Must Hold Under All Inputs)

These 9 properties are verified by property-based testing (Hypothesis, Python) with 500+ randomly generated inputs each. Properties P1-P9 are **provably correct.**

| # | Property | Formal Statement |
|---|----------|-----------------|
| **P1** | Budget monotonically non-increasing | ∀ calls: budget_after ≤ budget_before |
| **P2** | Repeat counts non-decreasing | ∀ tool: repeat_count never decreases |
| **P3** | Once tripped, always tripped | circuit_tripped ⇒ all subsequent verdicts = DENY |
| **P4** | Budget never negative | budget_remaining ≥ 0 |
| **P5** | Repeat limit triggers DENY | repeat_count ≥ max_repeat_calls ⇒ verdict = DENY |
| **P6** | Fresh state ALLOWs first call | fresh SessionState + any ToolCall ⇒ ALLOW |
| **P7** | Deterministic output | identical (state, call, config, rng) ⇒ identical Decision |
| **P8** | HITL_ALWAYS produces HITL | hitl_mode = ALWAYS ∧ ¬circuit_tripped ⇒ verdict = HITL |
| **P9** | Nonce uniqueness enforced | duplicate nonce ⇒ DENY |

### 3.4 Verdict Types

| Verdict | Meaning | Agent Action |
|---------|---------|-------------|
| **ALLOW** | Execute as requested | Proceed with tool call |
| **DENY** | Block execution | Abort, surface deny reason |
| **HITL** | Human decision required | Pause, await human verdict via HITL console |

### 3.5 Deny Reasons

| Reason | Trigger |
|--------|---------|
| `CIRCUIT_OPEN` | Circuit breaker was previously tripped |
| `BUDGET_EXHAUSTED` | Budget remaining ≤ 0 or cost > remaining |
| `MAX_REPEAT_EXCEEDED` | Same tool + same params repeated too many times |
| `WINDOW_EXCEEDED` | Too many calls in the current time window |
| `GLOBAL_LIMIT` | Session-wide call limit reached |
| `POLICY_VIOLATION` | Duplicate nonce (replay attack) |
| `AUTH_FAILED` | Authentication failure |

### 3.6 Error Signal Amplification

SHACKLE detects error signals in tool parameters WITHOUT regex (no ReDoS attack surface). When `error_amplification` is enabled, the repeat call threshold is reduced by 1 if the parameters contain known error signals:

```
Error signals: 401, unauthorized, 403, forbidden, 500, 502, 503,
               504, timeout, connection refused, connection reset,
               permission denied, rate limit, quota exceeded,
               invalid api key, token expired, model not found,
               resource exhausted, deadline exceeded
```

This catches the "loop of death" — agent hits 401, retries, gets 401, retries — without waiting for the full repeat threshold.

### 3.7 Probabilistic Denial (Adversarial Hardening)

When `probabilistic_deny` is enabled and the agent is below 20% budget, a random factor is introduced:

```
probability = deny_jitter_ratio × (1.0 − budget_ratio)
IF rng < probability:
    → DENY (appears as BUDGET_EXHAUSTED)
```

This prevents adversarial agents from probing the exact budget threshold and gaming it. The denial appears identical to a real budget exhaustion, making it impossible to distinguish signal from noise.

---

## 4. Message Protocol

### 4.1 Common Envelope

Every SHACKLE message is wrapped in a Protocol Buffers envelope:

```protobuf
message Envelope {
  string protocol_version = 1;       // "1.0.0"
  string message_id = 2;             // UUIDv7, client-generated
  string correlation_id = 3;         // Request/response pairing
  int64 client_timestamp_ns = 4;     // Client clock (informational)
  int64 server_timestamp_ns = 5;     // Set by daemon on receipt
  bytes hmac = 6;                    // HMAC-SHA256 over payload
  oneof payload {
    PreExecRequest pre_exec = 10;
    PreExecResponse pre_exec_response = 11;
    PostExecNotification post_exec = 12;
    RegisterRequest register = 13;
    RegisterResponse register_response = 14;
    Heartbeat heartbeat = 15;
    HeartbeatAck heartbeat_ack = 16;
    Error error = 17;
  }
}
```

### 4.2 Session Registration

```protobuf
message RegisterRequest {
  string agent_id = 1;
  string agent_version = 2;
  string framework = 3;              // "crewai" | "autogen" | "langgraph"
  string session_id = 4;             // Optional: resume existing session
  string organization_id = 5;
  string runtime = 6;
  map<string, string> metadata = 7;
}

message RegisterResponse {
  string session_id = 1;
  string daemon_version = 2;
  string negotiated_protocol = 3;
  GuardConfig active_config = 4;
  int64 daemon_time_ns = 5;
}
```

### 4.3 Pre-Execution Check

```protobuf
message PreExecRequest {
  string session_id = 1;
  uint64 call_number = 2;            // Monotonically increasing
  string tool_name = 3;
  bytes tool_params_hash = 4;        // SHA-256 of canonical JSON params
  double estimated_cost_usd = 5;
  string parent_guard_id = 6;        // For nested guard trees
  uint64 nonce = 7;                  // Anti-replay
  map<string, string> tags = 8;
}

message PreExecResponse {
  string session_id = 1;
  uint64 call_number = 2;
  Verdict verdict = 3;
  DenyReason deny_reason = 4;
  string human_readable_reason = 5;
  double budget_remaining_usd = 6;
  int32 repeat_count = 7;
  int64 daemon_time_ns = 8;
  bool probabilistic_deny = 9;
}
```

### 4.4 Post-Execution Notification

Fire-and-forget. No response expected.

```protobuf
message PostExecNotification {
  string session_id = 1;
  uint64 call_number = 2;
  double actual_cost_usd = 3;
  bool success = 4;
  string error_message = 5;
  int64 duration_ms = 6;
  uint64 tokens_in = 7;
  uint64 tokens_out = 8;
  string model_used = 9;
}
```

### 4.5 Heartbeat

Agents SHOULD send heartbeats every 30 seconds. 3 consecutive missed heartbeats → session marked STALE.

```protobuf
message Heartbeat {
  string session_id = 1;
  uint64 last_call_number = 2;
  double local_budget_remaining = 3;  // For drift detection
}

message HeartbeatAck {
  string session_id = 1;
  double daemon_budget_remaining = 2; // Authoritative view
  bool drift_detected = 3;
  int64 daemon_time_ns = 4;
}
```

### 4.6 gRPC Service Definition

```protobuf
service ShackleDaemon {
  rpc Register(RegisterRequest) returns (RegisterResponse);
  rpc PreExec(PreExecRequest) returns (PreExecResponse);
  rpc PostExec(PostExecNotification) returns (google.protobuf.Empty);
  rpc Heartbeat(Heartbeat) returns (HeartbeatAck);
  rpc GetSessionState(GetSessionStateRequest) returns (SessionState);
}
```

---

## 5. State Model

### 5.1 Session State

```protobuf
message SessionState {
  string session_id = 1;
  string agent_id = 2;
  string organization_id = 3;
  SessionStatus status = 4;          // ACTIVE | PAUSED | TERMINATED | STALE

  // Budget
  double budget_initial_usd = 10;
  double budget_remaining_usd = 11;
  double budget_spent_usd = 12;

  // Counters
  uint64 total_calls = 20;
  map<string, uint32> repeat_counts = 21;   // tool_name → consecutive identical calls
  map<string, uint32> window_counts = 22;   // tool_name → calls in current window

  // Circuit
  bool circuit_tripped = 30;
  string circuit_trip_reason = 31;
  int64 circuit_tripped_at_ns = 32;

  // Time
  int64 window_start_ns = 40;
  uint32 window_duration_s = 41;
  uint32 window_max_calls = 42;

  // Last known
  string last_tool_name = 50;
  bytes last_tool_params_hash = 51;
  int64 last_activity_ns = 52;

  // Metadata
  map<string, string> metadata = 60;
}

enum SessionStatus {
  ACTIVE = 0;
  PAUSED = 1;       // HITL in progress
  TERMINATED = 2;
  STALE = 3;        // Heartbeat timeout
}
```

### 5.2 Guard Configuration

```protobuf
message GuardConfig {
  // Budget
  double budget_usd = 1;              // 0 = disabled
  BudgetScope budget_scope = 2;       // PER_SESSION | PER_AGENT | PER_ORG

  // Repeat calls
  uint32 max_repeat_calls = 10;       // 0 = disabled
  bool error_amplification = 11;      // Lower threshold on error signals

  // Timeout
  uint32 timeout_seconds = 20;        // Wall-clock timeout. 0 = disabled

  // Time window
  uint32 window_duration_s = 30;
  uint32 window_max_calls = 31;

  // Global limits
  uint32 max_total_calls = 40;        // 0 = disabled

  // Adversarial hardening
  bool probabilistic_deny = 50;
  double deny_jitter_ratio = 51;      // 0.0–1.0

  // HITL
  HitlMode hitl_mode = 60;            // NEVER | ON_DENY | ON_THRESHOLD | ALWAYS
  double hitl_budget_threshold = 61;  // 0.0–1.0

  // Hierarchy
  string parent_guard_id = 70;        // For nested guard trees
}

enum BudgetScope {
  PER_SESSION = 0;
  PER_AGENT = 1;
  PER_ORGANIZATION = 2;
}

enum HitlMode {
  HITL_NEVER = 0;
  HITL_ON_DENY = 1;
  HITL_ON_BUDGET_THRESHOLD = 2;
  HITL_ALWAYS = 3;
}
```

### 5.3 State Transitions

State is NEVER mutated by the decision function. The daemon applies state changes AFTER the verdict is returned:

- **After ALLOW:** Increment counters, record nonce, update repeat/window counts
- **After DENY:** Trip circuit breaker (session-wide block)
- **After HITL:** Set session status to PAUSED, await human verdict
- **After PostExec:** Update budget (budget_spent += actual_cost)

---

## 6. Transport Bindings

### 6.1 Unix Domain Socket (Default)

```
Path:       /var/run/shackle.sock
Permissions: 0660, owned shackle:agents
Framing:    Length-prefixed protobuf (4-byte big-endian length + protobuf bytes)
SLA:        5ms for pre_exec, 1s for register
```

### 6.2 gRPC (Enterprise)

```
Endpoint:   grpc://localhost:9000 or grpcs:// for TLS
Service:    ShackleDaemon (see §4.6)
Auth:       mTLS with client certificates
SLA:        5ms for pre_exec, 1s for register
```

### 6.3 WebSocket (Remote HITL)

```
Endpoint:   wss://shackle.example.com/v1/control
Auth:       Bearer token in initial connect
Messages:   JSON-encoded protobuf over text frames
Purpose:    Remote HITL console, cross-network agents
```

---

## 7. Audit and Security

### 7.1 Audit Log Entry

```protobuf
message AuditEntry {
  string entry_id = 1;               // UUIDv7
  int64 timestamp_ns = 2;            // Daemon time
  string session_id = 3;
  string agent_id = 4;
  string organization_id = 5;
  uint64 call_number = 6;
  string tool_name = 7;
  bytes tool_params_hash = 8;
  Verdict verdict = 9;
  DenyReason deny_reason = 10;
  double budget_before_usd = 11;
  double budget_after_usd = 12;
  string operator_id = 13;           // Human operator if HITL override
  bytes signature = 14;              // Ed25519 over fields 1–13
  bytes previous_entry_hash = 15;    // Chain-link to previous entry
}
```

### 7.2 Cryptographic Properties

| Property | Mechanism |
|----------|-----------|
| **Authenticity** | Ed25519 signature over all entry fields |
| **Integrity** | Chain-linked via `previous_entry_hash` (SHA-256) |
| **Immutability** | Append-only file (O_APPEND, no seek permitted) |
| **Non-repudiation** | Signing key held exclusively by daemon; verification key is public |
| **Verifiability** | Any third party can verify the chain with only the public verification key |

### 7.3 Trust Model

| Component | Trust Level | Rationale |
|-----------|-------------|-----------|
| SHACKLE Daemon | **Fully trusted** | Holds state, writes audit log, issues verdicts |
| Agent Process | **Untrusted** | May be compromised, buggy, or adversarial |
| Transport | **Authenticated + integrity-protected** | HMAC on every message |
| HITL Console | **Authenticated user** | Human decision with audit trail |

### 7.4 Threat Mitigations

| Threat | Mitigation |
|--------|-----------|
| Replay attack | Nonce per call; daemon tracks seen nonces (§3.2, Layer 2) |
| Identity spoofing | Registration with org-level auth (§4.2) |
| Clock manipulation | Daemon is sole time authority; client timestamps are informational |
| Budget drift | Heartbeat sync with authoritative state (§4.5) |
| Adversarial probing | Probabilistic denial near thresholds (§3.7) |
| Audit tampering | Append-only file; Ed25519 signatures; chain-linked entries (§7.2) |
| DoS | Rate limiting per session; message size cap (1MB) |
| Protocol parser exploits | Separate process for parsing; seccomp sandbox |

### 7.5 Operational Security

- Daemon runs as dedicated user (`shackle`), NOT root
- Unix socket owned `shackle:agents`, mode 0660
- Audit log file owned `shackle:shackle`, mode 0640, append-only
- Rate limit: 1,000 pre_exec/sec/session; 10 register/sec/IP
- Max message size: 1MB
- Daily log rotation with compression and archival

---

## 8. Compliance Framework

### 8.1 SOC2 Mapping

| SOC2 TSC | SHACKLE Feature | Evidence |
|----------|----------------|----------|
| **CC6.1** Logical Access | Session registration + authentication | RegisterRequest with org_id |
| **CC6.3** Security Incidents | Circuit breaker trip events | AuditEntry with DENY verdict |
| **CC7.2** System Monitoring | Heartbeat + drift detection | Heartbeat/HeartbeatAck messages |
| **CC7.3** Incident Response | HITL console with operator audit trail | operator_id in AuditEntry |
| **CC8.1** Change Management | Version negotiation + LTS policy | §9 |
| **A1.2** Availability | Timeout enforcement | timeout_seconds in GuardConfig |
| **C1.1** Confidentiality | On-premise daemon, no telemetry | Model B/C deployment; local-only |
| **PI1.3** Processing Integrity | Deterministic decision function | §3.3 properties P1–P9 |

### 8.2 Standards Compliance

SHACKLE audit logs are designed to satisfy:
- **SOC2 Type II** auditor requests
- **ISO 27001** Annex A.12.4 (Logging and Monitoring)
- **GDPR Article 30** (Records of Processing) — for agent actions on personal data
- **Cyber insurance** underwriting requirements

---

## 9. Versioning and Long-Term Support

### 9.1 Protocol Versioning

Protocol versions follow SemVer: `MAJOR.MINOR.PATCH`

- **MAJOR:** Incompatible message schema changes
- **MINOR:** New message types, backward-compatible additions
- **PATCH:** Clarifications, bug fixes, no schema changes

### 9.2 Version History

| Version | Date | Changes |
|---------|------|---------|
| **1.0.0** | 2026-06-25 | Initial release. 9 invariant properties. Unix/gRPC transport. Ed25519 audit. |

### 9.3 Long-Term Support

- SP/1.0 is the LTS version, guaranteed support through 2031
- New major versions coexist with previous LTS for minimum 2 years
- Audit log schema is append-only: fields added, never removed
- Deprecated fields marked with annotation, never deleted

### 9.4 Negotiation

```
Client → Daemon: protocol_version = "1.2.0"
Daemon checks:   can support up to 1.0.0
Daemon → Client: negotiated_protocol = "1.0.0"
```

No compatible version → Error with code `PROTOCOL_VERSION_MISMATCH`.

---

## 10. Reference Implementation

The Python reference implementation lives at:

**<https://github.com/Fame510/SHACKLE-PRO->**

| Component | File | Status |
|-----------|------|--------|
| Decision function | `v2/spec/decide.py` | ✅ Production (187 lines) |
| Property-based tests | `v2/tests/test_decide_properties.py` | ✅ 18/18 passing |
| Protocol definitions | `v2/protocol/shackle.proto` | ✅ Complete |
| Service definitions | `v2/protocol/shackle_service.proto` | ✅ Complete |
| CI pipeline | `.github/workflows/ci.yml` | ✅ Configured |
| TypeScript library | `v2/ts/` | ✅ Published |
| Docker image | `Dockerfile` | ✅ Multi-stage |

### 10.1 Quick Start

```python
from shackle import Guard

@Guard(budget=0.50, max_repeat_calls=3, timeout_seconds=180)
def my_agent():
    # Agent logic here
    # SHACKLE intercepts every tool call
    pass
```

Install: `pip install git+https://github.com/Fame510/SHACKLE-PRO-.git`

---

## Appendix A: Example Flow

```
1.  Agent → Daemon: REGISTER(agent_id="research-bot", framework="crewai")
2.  Daemon → Agent: REGISTER_RESPONSE(session_id="s_01", config={budget:0.50, max_repeat:3})

3.  Agent → Daemon: PRE_EXEC(call=1, tool="web_search", hash=0xDEAD, cost=0.002)
4.  Daemon → Agent: PRE_EXEC_RESPONSE(verdict=ALLOW, budget_remaining=0.498)

5.  Agent: [executes web_search]
6.  Agent → Daemon: POST_EXEC(call=1, actual_cost=0.0015, success=true)

7.  Agent → Daemon: PRE_EXEC(call=2, tool="web_search", hash=0xDEAD, cost=0.002)
8.  Daemon → Agent: PRE_EXEC_RESPONSE(verdict=ALLOW, budget_remaining=0.496, repeat_count=1)

    ... agent repeats 2 more times ...

9.  Agent → Daemon: PRE_EXEC(call=4, tool="web_search", hash=0xDEAD, cost=0.002)
10. Daemon → Agent: PRE_EXEC_RESPONSE(verdict=DENY, reason=MAX_REPEAT_EXCEEDED, repeat_count=3)

11. Daemon: [writes AuditEntry to append-only log]
12. Daemon: [trips circuit breaker for session — all subsequent calls DENY]
```

---

## Appendix B: Error Codes

| Code | Description |
|------|-------------|
| `PROTOCOL_VERSION_MISMATCH` | No compatible protocol version |
| `SESSION_NOT_FOUND` | Unknown or expired session_id |
| `AUTHENTICATION_FAILED` | Invalid credentials or duplicate nonce |
| `RATE_LIMITED` | Too many requests |
| `MESSAGE_TOO_LARGE` | Exceeds 1MB limit |
| `DAEMON_UNAVAILABLE` | Internal daemon error |
| `ORGANIZATION_QUOTA_EXCEEDED` | Org-level limit reached |
| `PARENT_GUARD_DENIED` | Parent guard rejected the call |

---

## Appendix C: Glossary

| Term | Definition |
|------|-----------|
| **Agent** | An autonomous AI process that executes tools |
| **Circuit Breaker** | Once tripped, all subsequent calls are DENY |
| **Daemon** | The SHACKLE server process — sole authority for state and verdicts |
| **Guard** | A configured policy (budget, repeat limit, timeout) applied to an agent |
| **HITL** | Human-in-the-Loop — manual authorization step |
| **Nonce** | A number used once — anti-replay mechanism |
| **Tool Call** | A single invocation of an agent tool (API, file I/O, code exec) |
| **Verdict** | The decision: ALLOW, DENY, or HITL |

---

*SP/1.0 — Sovereign Logic, June 2026. Licensed under CC-BY 4.0.*  
*Reference implementation: AGPLv3 + Commercial. Contact: docspoc101@gmail.com*
