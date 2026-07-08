# SHACKLE Protocol Specification

## SP/1.0 — Runtime Security Envelope for Autonomous AI Agents

**Status:** Draft  
**Version:** 1.0.0  
**Authors:** Dante Bullock, Sovereign Logic  
**License:** CC-BY-4.0 (specification — free to implement; attribution to SP/1.0 required); implementations carry their own terms (SHACKLE reference implementation is AGPL-3.0 + commercial)  

---

## 1. Introduction

### 1.1 Purpose

The SHACKLE Protocol (SP) defines a language-agnostic, transport-agnostic interface between autonomous AI agent runtimes and a policy enforcement daemon. It answers one question:

> **Should this agent be allowed to execute this tool with these parameters at this moment?**

### 1.2 Design Principles

| Principle | Meaning |
|-----------|---------|
| **Deterministic core** | The decision function is stateless per-invocation; all state lives in the daemon |
| **Daemon as authority** | The SHACKLE daemon is the sole source of truth for time, state, and verdicts |
| **Append-only audit** | Every decision (ALLOW/DENY/HITL) is cryptographically logged; logs are immutable |
| **Graceful degradation** | Agents function without the daemon in local/library mode; distributed state is an upgrade |
| **Protocol > implementation** | This spec outlives any single language binding |

### 1.3 Scope

This specification covers:

- Message schemas and semantics
- State model (budgets, counters, violations, sessions)
- Decision function and verdicts
- Transport bindings
- Version negotiation
- Security considerations

This specification does **not** cover:

- How the daemon persists state (implementation detail)
- How the HITL console renders (UI concern)
- Pricing models or commercial terms

---

## 2. Architecture

### 2.1 Deployment Models

```
MODEL A: Library Mode (V1 compatible)
┌─────────────────────────┐
│  Agent Process          │
│  ┌───────────────────┐  │
│  │ @Guard decorator  │  │
│  │ (in-process)      │  │
│  │ Local state only  │  │
│  └───────────────────┘  │
└─────────────────────────┘

MODEL B: Sidecar Daemon (V2)
┌─────────────────┐     Unix Socket      ┌──────────────────────────┐
│  Agent Process  │ ◄──────────────────► │  SHACKLE Daemon          │
│  ┌───────────┐  │   pre_exec           │  ┌────────────────────┐  │
│  │ Thin      │  │   post_exec          │  │ Policy Engine      │  │
│  │ Client    │  │   register           │  │ - Budgets          │  │
│  │ Shim      │  │   heartbeat          │  │ - Counters         │  │
│  └───────────┘  │                      │  │ - Circuit Breakers │  │
└─────────────────┘                      │  └────────────────────┘  │
                                          │  ┌────────────────────┐  │
                                          │  │ Audit Log          │  │
                                          │  │ (append-only,      │  │
                                          │  │  cryptographically │  │
                                          │  │  signed)           │  │
                                          │  └────────────────────┘  │
                                          └──────────────────────────┘

MODEL C: Distributed (V2 Enterprise)
┌──────────┐  ┌──────────┐  ┌──────────┐
│ Agent A  │  │ Agent B  │  │ Agent C  │
│ (Lambda) │  │ (K8s)    │  │ (local)  │
└────┬─────┘  └────┬─────┘  └────┬─────┘
     │             │             │
     └─────────────┬─────────────┘
                   │  gRPC/TLS
          ┌────────┴────────┐
          │  SHACKLE        │
          │  Daemon Cluster │
          │  ┌────────────┐ │
          │  │ Redis      │ │──── Shared State
          │  │ Postgres   │ │──── Audit Logs
          │  └────────────┘ │
          └─────────────────┘
```

### 2.2 Protocol Layers

```
┌──────────────────────────────────┐
│         Policy Language          │  ← Future: DSL for guard rules
├──────────────────────────────────┤
│      Decision Function           │  ← decide(state, call) → Verdict
├──────────────────────────────────┤
│      Message Protocol            │  ← This specification
├──────────────────────────────────┤
│      Transport (Unix/gRPC/WS)    │  ← Binding layer
└──────────────────────────────────┘
```

---

## 3. Message Protocol

### 3.1 Common Envelope

Every SHACKLE protocol message is wrapped in an envelope:

```protobuf
message Envelope {
  string protocol_version = 1;    // "1.0.0"
  string message_id = 2;          // UUIDv7, client-generated
  string correlation_id = 3;      // For request/response pairing
  int64 client_timestamp_ns = 4;  // Client clock (informational only)
  int64 server_timestamp_ns = 5;  // Set by daemon on receipt
  bytes hmac = 6;                 // HMAC-SHA256 over payload
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

### 3.2 Registration

An agent MUST register before sending `pre_exec` messages.

```protobuf
message RegisterRequest {
  string agent_id = 1;            // Unique agent identifier
  string agent_version = 2;       // Agent software version
  string framework = 3;           // "crewai" | "autogen" | "langgraph" | ...
  string session_id = 4;          // Optional: resume existing session
  string organization_id = 5;     // Multi-tenant namespace
  string runtime = 6;             // "python3.11" | "node20" | ...
  map<string, string> metadata = 7;
}

message RegisterResponse {
  string session_id = 1;          // Daemon-assigned session ID
  string daemon_version = 2;      // Daemon software version
  string negotiated_protocol = 3; // Agreed protocol version
  GuardConfig active_config = 4;  // Policy configuration
  int64 daemon_time_ns = 5;       // For clock synchronization
}
```

### 3.3 Pre-Execution Check

Sent before every tool call. The daemon MUST respond within the SLA (default: 5ms).

```protobuf
message PreExecRequest {
  string session_id = 1;
  uint64 call_number = 2;         // Monotonically increasing per session
  string tool_name = 3;           // Qualified tool name
  bytes tool_params_hash = 4;     // SHA-256 of canonical JSON params
  double estimated_cost_usd = 5;  // Agent's cost estimate
  string parent_guard_id = 6;     // For nested guard trees
  uint64 nonce = 7;               // Anti-replay: never repeat
  map<string, string> tags = 8;   // Arbitrary key-value metadata
}

enum Verdict {
  ALLOW = 0;
  DENY = 1;
  HITL = 2;
}

enum DenyReason {
  UNSPECIFIED = 0;
  BUDGET_EXHAUSTED = 1;
  MAX_REPEAT_CALLS_EXCEEDED = 2;
  CIRCUIT_OPEN = 3;
  TIMEOUT_WINDOW_EXCEEDED = 4;
  GLOBAL_RATE_LIMIT = 5;
  POLICY_VIOLATION = 6;
  AUTHENTICATION_FAILED = 7;
  ORGANIZATION_QUOTA_EXCEEDED = 8;
}

message PreExecResponse {
  string session_id = 1;
  uint64 call_number = 2;         // Echo of request
  Verdict verdict = 3;
  DenyReason deny_reason = 4;     // Only meaningful for DENY
  string human_readable_reason = 5;
  double budget_remaining_usd = 6; // Current budget state
  int32 repeat_count = 7;          // Current repeat count for this tool
  int64 daemon_time_ns = 8;
  bool probabilistic_deny = 9;     // True if denial was randomized (adversarial hardening)
}
```

### 3.4 Post-Execution Notification

Sent after tool execution completes (success or failure). Fire-and-forget — no response expected.

```protobuf
message PostExecNotification {
  string session_id = 1;
  uint64 call_number = 2;
  double actual_cost_usd = 3;      // Actual cost (from API response headers)
  bool success = 4;                // Did the tool succeed?
  string error_message = 5;        // If failed
  int64 duration_ms = 6;           // Wall-clock execution time
  uint64 tokens_in = 7;
  uint64 tokens_out = 8;
  string model_used = 9;           // Actual model (may differ from request)
}
```

### 3.5 Heartbeat

Agents SHOULD send heartbeats every 30 seconds while active. If the daemon misses 3 consecutive heartbeats, it considers the session stale and releases state.

```protobuf
message Heartbeat {
  string session_id = 1;
  uint64 last_call_number = 2;     // Last call the agent believes it executed
  double local_budget_remaining = 3; // Agent's local budget view (for drift detection)
}

message HeartbeatAck {
  string session_id = 1;
  double daemon_budget_remaining = 2; // Daemon's authoritative view
  bool drift_detected = 3;           // True if agent and daemon disagree
  int64 daemon_time_ns = 4;
}
```

### 3.6 Error

```protobuf
message Error {
  string code = 1;                 // Machine-readable error code
  string message = 2;              // Human-readable description
  map<string, string> details = 3;
}
```

---

## 4. State Model

### 4.1 Session State

```protobuf
message SessionState {
  string session_id = 1;
  string agent_id = 2;
  string organization_id = 3;
  SessionStatus status = 4;        // ACTIVE | PAUSED | TERMINATED | STALE

  // Budget
  double budget_initial_usd = 10;
  double budget_remaining_usd = 11;
  double budget_spent_usd = 12;

  // Counters
  uint64 total_calls = 20;
  map<string, uint32> repeat_counts = 21;  // tool_name → consecutive identical calls
  map<string, uint32> window_counts = 22;  // tool_name → calls in current time window

  // Circuit state
  bool circuit_tripped = 30;
  string circuit_trip_reason = 31;
  int64 circuit_tripped_at_ns = 32;

  // Time windows
  int64 window_start_ns = 40;      // Start of current time window
  uint32 window_duration_s = 41;   // Duration of time window
  uint32 window_max_calls = 42;    // Max calls allowed in window

  // Last known state
  string last_tool_name = 50;
  bytes last_tool_params_hash = 51;
  int64 last_activity_ns = 52;

  // Metadata
  map<string, string> metadata = 60;
}

enum SessionStatus {
  ACTIVE = 0;
  PAUSED = 1;       // HITL in progress
  TERMINATED = 2;   // Normal termination
  STALE = 3;        // Heartbeat timeout
}
```

### 4.2 Guard Configuration

```protobuf
message GuardConfig {
  // Budget guard
  double budget_usd = 1;           // 0 = disabled
  BudgetScope budget_scope = 2;    // PER_SESSION | PER_AGENT | PER_ORG

  // Repeat call guard
  uint32 max_repeat_calls = 10;    // 0 = disabled
  bool error_amplification = 11;   // Lower threshold on error signals

  // Timeout guard
  uint32 timeout_seconds = 20;     // 0 = disabled

  // Time window guard
  uint32 window_duration_s = 30;
  uint32 window_max_calls = 31;

  // Global limits
  uint32 max_total_calls = 40;     // 0 = disabled

  // Adversarial hardening
  bool probabilistic_deny = 50;    // Introduce random denials near thresholds
  double deny_jitter_ratio = 51;   // 0.0-1.0, how much randomness to inject

  // HITL
  HitlMode hitl_mode = 60;         // NEVER | ON_DENY | ON_BUDGET_THRESHOLD | ALWAYS
  double hitl_budget_threshold = 61; // Trigger HITL at this fraction of budget

  // Parent
  string parent_guard_id = 70;     // For hierarchical budget trees
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

---

## 5. Decision Function

### 5.1 Specification

The decision function `decide(state, call) → Verdict` is the core of SHACKLE. It MUST be:

- **Deterministic:** Same state + same call = same verdict
- **Monotonic:** Budget never increases, repeat counts never decrease, once tripped stays tripped
- **Side-effect-free:** Does not modify state; the daemon applies state changes after the verdict

### 5.2 Decision Algorithm

```
function decide(state: SessionState, call: PreExecRequest, config: GuardConfig) -> Verdict:
    // 1. Circuit breaker check (highest priority)
    if state.circuit_tripped:
        return DENY(CIRCUIT_OPEN)

    // 2. Budget check
    if config.budget_usd > 0:
        if state.budget_remaining_usd <= 0:
            return DENY(BUDGET_EXHAUSTED)
        if state.budget_remaining_usd < call.estimated_cost_usd:
            if config.hitl_mode == HITL_ON_DENY or config.hitl_mode == HITL_ALWAYS:
                return HITL("budget insufficient for estimated cost")

    // 3. Repeat call check
    if config.max_repeat_calls > 0:
        repeat_count = state.repeat_counts.get(call.tool_name, 0)
        if call.tool_params_hash == state.last_tool_params_hash:
            if repeat_count >= config.max_repeat_calls:
                return DENY(MAX_REPEAT_CALLS_EXCEEDED)
        // Error amplification: lower threshold if params contain error signals
        if config.error_amplification and has_error_signal(call.tool_params_hash):
            if repeat_count >= max(1, config.max_repeat_calls - 1):
                return DENY(MAX_REPEAT_CALLS_EXCEEDED)

    // 4. Time window check
    if config.window_max_calls > 0:
        window_count = state.window_counts.get(call.tool_name, 0)
        if window_count >= config.window_max_calls:
            return DENY(TIMEOUT_WINDOW_EXCEEDED)

    // 5. Global call limit
    if config.max_total_calls > 0 and state.total_calls >= config.max_total_calls:
        return DENY(POLICY_VIOLATION)

    // 6. Probabilistic denial (adversarial hardening)
    if config.probabilistic_deny:
        budget_ratio = state.budget_remaining_usd / state.budget_initial_usd
        if budget_ratio < 0.2:  // Below 20% budget
            if random() < config.deny_jitter_ratio * (1.0 - budget_ratio):
                return DENY(BUDGET_EXHAUSTED, probabilistic=true)  // May appear as budget denial

    // 7. HITL checkpoint
    if config.hitl_mode == HITL_ON_BUDGET_THRESHOLD:
        budget_fraction = state.budget_remaining_usd / state.budget_initial_usd
        if budget_fraction <= config.hitl_budget_threshold:
            return HITL("budget threshold reached")

    return ALLOW
```

### 5.3 Properties (Must Hold Under All Inputs)

| # | Property | Verification |
|---|----------|-------------|
| P1 | Budget remaining is monotonically non-increasing | ∀ calls: budget_after ≤ budget_before |
| P2 | Repeat counts are non-decreasing | ∀ tool: repeat_count never decreases |
| P3 | Once tripped, always tripped | circuit_tripped → all subsequent verdicts are DENY |
| P4 | Budget never negative | budget_remaining ≥ 0 |
| P5 | Repeat limit triggers DENY | repeat_count ≥ max_repeat_calls → verdict = DENY |
| P6 | Empty state always ALLOWs first call | fresh state + any call → ALLOW |
| P7 | Same input produces same output | deterministic for identical (state, call, config) |
| P8 | HITL never produces ALLOW | verdict = HITL is terminal until human action |
| P9 | Nonce uniqueness enforced | duplicate nonce → DENY(AUTHENTICATION_FAILED) |

---

## 6. Transport Bindings

### 6.1 Unix Domain Socket (Default)

```
Path: /var/run/shackle.sock (configurable)
Permissions: 0660, owned shackle:agents
Framing: Length-prefixed protobuf (4-byte big-endian length + protobuf bytes)
Timeout: 5ms SLA for pre_exec, 1s for register
```

### 6.2 gRPC (Enterprise)

```protobuf
service ShackleDaemon {
  rpc Register(RegisterRequest) returns (RegisterResponse);
  rpc PreExec(PreExecRequest) returns (PreExecResponse);
  rpc PostExec(PostExecNotification) returns (google.protobuf.Empty);
  rpc Heartbeat(Heartbeat) returns (HeartbeatAck);
  rpc GetSessionState(GetSessionStateRequest) returns (SessionState);
  rpc ResumeSession(ResumeSessionRequest) returns (ResumeSessionResponse);
}
```

### 6.3 WebSocket (Remote HITL)

```
Endpoint: wss://shackle.example.com/v1/control
Auth: Bearer token in initial connect message
Messages: JSON-encoded protobuf over text frames
Purpose: Remote HITL console, mobile control, cross-network agents
```

---

## 7. Security Considerations

### 7.1 Trust Model

| Component | Trust Level | Rationale |
|-----------|-------------|-----------|
| SHACKLE Daemon | **Fully trusted** | Holds state, writes audit log, issues verdicts |
| Agent Process | **Untrusted** | May be compromised, buggy, or adversarial |
| Transport | **Authenticated + integrity-protected** | HMAC on every message |
| HITL Console | **Authenticated user** | Human in the loop with audit trail |

### 7.2 Threat Mitigations

| Threat | Mitigation | Section |
|--------|-----------|---------|
| Replay attack | Nonce per call; daemon tracks seen nonces | 3.3 |
| Identity spoofing | Registration with organization-level authentication | 3.2 |
| Clock manipulation | Daemon is sole time authority; client timestamps are informational | 3.1 |
| Budget drift | Heartbeat sync with authoritative state | 3.5 |
| Adversarial probing | Probabilistic denial near thresholds | 5.2 §6 |
| Audit log tampering | Append-only file permissions; cryptographic signing | 7.4 |
| DoS against daemon | Rate limiting per session; message size limits | 7.3 |
| Protocol parser exploits | Separate process for message parsing; sandboxed | 7.3 |

### 7.3 Operational Security

- Daemon runs as dedicated user (`shackle`), NOT root
- Message parsing in forked child process with seccomp profile
- Unix socket owned `shackle:agents`, mode 0660
- Audit log file owned `shackle:shackle`, mode 0640, append-only
- Rate limit: 1000 pre_exec/sec/session, 10 register/sec/IP
- Max message size: 1MB

### 7.4 Audit Log

Every verdict is logged as an immutable record:

```protobuf
message AuditEntry {
  string entry_id = 1;             // UUIDv7
  int64 timestamp_ns = 2;         // Daemon time
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
  string operator_id = 13;         // Human operator if HITL override
  bytes signature = 14;            // Ed25519 signature over fields 1-13
}
```

Audit log entries are:
- Append-only (file opened O_APPEND, no seek)
- Cryptographically signed (Ed25519)
- Chain-linked (each entry includes previous entry's hash)
- Rotatable (daily rotation, compressed, archived)

---

## 8. Version Negotiation

### 8.1 Protocol Versioning

Protocol versions follow SemVer: `MAJOR.MINOR.PATCH`.

- **MAJOR:** Incompatible message schema changes
- **MINOR:** New message types, backward-compatible additions
- **PATCH:** Clarifications, bug fixes, no schema changes

### 8.2 Negotiation

On registration, client sends its highest supported version. Daemon responds with the negotiated version:

```
Client → Daemon: protocol_version = "1.2.0"
Daemon checks: can support up to 1.0.0
Daemon → Client: negotiated_protocol = "1.0.0"
```

If no compatible version exists, daemon returns Error with code `PROTOCOL_VERSION_MISMATCH`.

### 8.3 Long-Term Support

- SP/1.0 is the LTS version, guaranteed support through 2031
- New major versions must coexist with previous LTS for minimum 2 years
- Audit log schema is append-only: new fields added, old fields never removed
- Deprecated fields are marked with `[deprecated]` annotation, never deleted

---

## 9. Compliance Framework

### 9.1 SOC2 Mapping

| SOC2 TSC | SHACKLE Feature | Evidence |
|----------|----------------|----------|
| **CC6.1** (Logical Access) | Session registration + authentication | RegisterRequest with org_id |
| **CC6.3** (Security Incidents) | Circuit breaker trip events | AuditEntry with DENY verdict |
| **CC7.2** (System Monitoring) | Heartbeat + drift detection | Heartbeat/HeartbeatAck messages |
| **CC7.3** (Incident Response) | HITL console with operator audit trail | operator_id in AuditEntry |
| **CC8.1** (Change Management) | Version negotiation + LTS policy | Section 8 |
| **A1.2** (Availability) | Timeout enforcement | timeout_seconds in GuardConfig |
| **C1.1** (Confidentiality) | On-premise daemon, no telemetry | Model B/C deployment; local-only |
| **PI1.3** (Processing Integrity) | Deterministic decision function | Section 5 properties P1-P9 |

### 9.2 Audit Readiness

SHACKLE audit logs are designed to satisfy:
- SOC2 Type II auditor requests
- ISO 27001 Annex A.12.4 (Logging and Monitoring)
- GDPR Article 30 (Records of Processing) — for agent actions on personal data
- Cyber insurance underwriting requirements

---

## 10. Reference Implementation

The Python reference implementation lives at `github.com/Fame510/SHACKLE-PRO-/v2/`.

### 10.1 Implementation Checklist

- [ ] Unix socket daemon (Python, async)
- [ ] `decide()` function with property-based tests
- [ ] Protobuf code generation from this spec
- [ ] In-process library mode (V1 compatibility shim)
- [ ] Redis state backend
- [ ] Postgres audit log backend
- [ ] CLI management tool (`shacklectl`)
- [ ] Remote HITL WebSocket server
- [ ] License key validation
- [ ] SOC2 compliance report template

---

## Appendix A: Example Flow

```
1. Agent: REGISTER(agent_id="research-bot", framework="crewai")
2. Daemon: REGISTER_RESPONSE(session_id="sess_abc123", config={budget: 0.50, max_repeat: 3})

3. Agent: PRE_EXEC(call=1, tool="web_search", hash=0xDEAD, cost=0.002)
4. Daemon: PRE_EXEC_RESPONSE(verdict=ALLOW, budget_remaining=0.498)

5. Agent: [executes web_search]
6. Agent: POST_EXEC(call=1, actual_cost=0.0015, success=true)

7. Agent: PRE_EXEC(call=2, tool="web_search", hash=0xDEAD, cost=0.002)
8. Daemon: PRE_EXEC_RESPONSE(verdict=ALLOW, budget_remaining=0.4965, repeat_count=1)

... agent repeats 2 more times ...

9. Agent: PRE_EXEC(call=4, tool="web_search", hash=0xDEAD, cost=0.002)
10. Daemon: PRE_EXEC_RESPONSE(verdict=DENY, reason=MAX_REPEAT_CALLS_EXCEEDED, repeat_count=3)

11. Daemon: [writes AuditEntry to append-only log]
12. Daemon: [trips circuit breaker for session]
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

*SP/1.0 — Sovereign Logic, 2026. Licensed under Creative Commons Attribution 4.0 International.*
