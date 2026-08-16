# SHACKLE Protocol Specification — SP/1.0

## Runtime Circuit Breaker for Autonomous AI Agents

**Version:** 1.0.0  
**Status:** Published  
**Date:** 2026-06-25  
**Authors:** Dante Bullock, Sovereign Logic  
**License:** Creative Commons Attribution 4.0 International (CC-BY 4.0) — see [LICENSE-SPEC.md](./LICENSE-SPEC.md)  
**Reference Implementation:** <https://github.com/Fame510/SHACKLE>  
**First Public Commit:** 2026-06-17 23:12 UTC  

> **License:** This specification and its conformance fixtures are licensed under
> [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —
> © 2026 Dante Bullock. Attribution required. See [LICENSE-SPEC.md](./LICENSE-SPEC.md).
> Implementing this specification requires no further license.
> The pyshackle software is licensed separately: AGPLv3 (open source) and
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
│    Decision Function             │  ← decide(config, state, call)
├──────────────────────────────────┤
│    Message Protocol              │  ← This specification
├──────────────────────────────────┤
│    Transport (Unix/gRPC/WS)      │  ← Binding layer
└──────────────────────────────────┘
```

---

## 3. The Decision Function

### 3.0 Which Function This Section Specifies

> **Normative.** SP/1.0 specifies exactly one decision surface:
>
> ```
> shackle/conformance.py :: decide(config, state, call) -> (verdict, reason)
> ```
>
> This is the function the 15 published vectors in `fixtures/conformance.json`
> encode, the function `tests/test_conformance.py` executes, the function
> `shackle/core.py` consults on every LLM and tool call, and the function
> `v2/daemon/decision.py` imports. An implementation is SHACKLE-conformant iff
> it reproduces this function's `(verdict, reason)` pairs on the published
> vectors.
>
> `v2/spec/decide.py` is a **forward-looking, non-normative** reference. It is
> typed with dataclasses/enums, carries an argument order and layer numbering
> of its own, and implements rules SP/1.0 does not certify (time windows,
> global call caps, probabilistic denial). It is imported only by benchmarks.
> Revisions §3.1–§3.5 of this document previously described *that* module;
> they now describe the certified core, and the extra layers are documented as
> non-normative in §3.7.

### 3.1 Core Function

The decision function is the heart of SHACKLE. It is a pure function — no I/O,
no side effects, no hidden state, no clock, no randomness. Given the same
`(config, state, call)` it returns the same `(verdict, reason)` forever.

```
function decide(
    config: Config,     # policy: budget_usd, max_repeat_calls, hitl_mode, ...
    state:  State,      # session: circuit_tripped, seen_nonces, budget_*, ...
    call:   Call        # proposal: tool_name, params, nonce, estimated_cost_usd
) -> (verdict, reason)

verdict ∈ { "ALLOW", "DENY", "HITL" }
reason  ∈ a stable string vocabulary (§3.5)
```

`reason` is part of the contract, not a diagnostic. Two implementations that
agree on every verdict but disagree on any reason are **not** conformant with
each other: the reason is what an auditor and an incident responder read.

### 3.2 Decision Algorithm — 10 Ordered Rules

Rules are evaluated in order; the first match returns. The ordering is the
specification — a runtime that applies the same rules in a different order is
not conformant, because the reason it reports will differ.

```
Rule 1: Canonicalizability
    IF call.params cannot be deterministically canonicalized:
        -> DENY("policy_violation:malformed_input")
    Non-canonicalizable means, structurally: not a JSON object; a non-string
    key; NaN or ±Infinity; a type JSON cannot represent; nesting deeper than
    the implementation limit. Detection MUST be structural. An implementation
    that only recognizes the reserved `__noncanonical__` marker used by the
    language-neutral fixtures does not conform (see §3.8).

Rule 2: Circuit Breaker
    IF state.circuit_tripped:
        -> DENY("circuit_open")

Rule 3: Anti-Replay
    IF call.nonce IS NOT NULL AND call.nonce IN state.seen_nonces:
        IF the pending transition is a resume of an already-terminal decision:
            -> DENY("policy_violation:duplicate_resume_no_effect")
        -> DENY("policy_violation:duplicate_nonce")
    The nonce is a per-dispatch replay token. It MUST NOT be derived from the
    call's content: two legitimate identical requests are not a replay.

Rule 4: HITL Transition Contract   (see §3.6)
    IF state.pending_transition IS PRESENT:
        -> resolve it, and return that result unconditionally.

Rule 5: Budget Exhausted
    IF config.budget_usd > 0 AND state.budget_remaining_usd <= 0:
        -> DENY("budget_exhausted")

Rule 5b: Budget Overrun
    IF config.budget_usd > 0 AND call.estimated_cost_usd > 0
       AND state.budget_remaining_usd - call.estimated_cost_usd < 0:
        -> DENY("budget_overrun")
    Distinct from Rule 5: budget remains, but not enough for THIS call. This
    is the rule that makes concurrent enforcement possible — the runtime MUST
    consult decide() with the PRE-call remaining so this fires before the
    budget is mutated, not after it has already gone negative.

Rule 6: Repeat Guard
    IF config.max_repeat_calls IS SET
       AND state.repeat_counts[call.tool_name] >= config.max_repeat_calls
       AND state.last_tool_name == call.tool_name:
        -> DENY("max_repeat_exceeded")

Rule 7: HITL Always
    IF config.hitl_mode == "always":
        -> HITL("hitl_all_calls")

Rule 8: HITL Budget Threshold
    IF config.hitl_mode == "on_threshold"
       AND remaining / initial <= config.hitl_budget_threshold:
        -> HITL("budget_threshold")

Rule 9: Opaque Context  (fail closed)
    IF any context-bearing binding in call.params is not deterministically
       evaluable:
        -> HITL("fail_closed:opaque_context")
    A binding is opaque when a context-bearing key (ctx, context, opaque,
    raw_context, blob — matched case- and whitespace-insensitively, at any
    depth) carries either an explicit non-evaluable marker (opaque, unknown,
    unevaluable, untestable) or a value the guard cannot introspect. The rule
    is a key-class × value-class PRODUCT. Matching only the literal pair
    {"ctx": "opaque"} does not conform; neither does matching any key named
    "context" regardless of value, which over-blocks ordinary traffic.

Rule 10: Default
    -> ALLOW("within_thresholds")
```

**Note on Rule 4 vs Rule 5.** A human approval is evaluated *before* budget.
This is a deliberate policy choice, not an oversight: an explicit,
digest-bound, single-use human release outranks the automated ceiling.
Deployments that require budget to be absolute MUST NOT populate
`pending_transition` once `budget_remaining_usd` reaches zero.

### 3.3 Invariants

These properties are exercised by the published vectors and by property-based
tests. They are **tested, not machine-proved** — there is no mechanized proof
of this function, and any claim otherwise would be an overstatement.

| # | Property | Statement |
|---|----------|-----------|
| **P1** | Totality | `decide()` returns a verdict for every input; it never raises, including on malformed, non-object, or non-serializable `params`. |
| **P2** | Determinism | identical `(config, state, call)` ⇒ identical `(verdict, reason)`. No clock, no RNG, no I/O. |
| **P3** | Circuit dominance | `circuit_tripped` ⇒ DENY for every call whose `params` are canonicalizable. |
| **P4** | Replay refusal | a nonce already in `seen_nonces` ⇒ DENY. |
| **P5** | Repeat limit | `repeat_counts[tool] ≥ max_repeat_calls` ∧ `last_tool_name == tool` ⇒ DENY. |
| **P6** | Budget floor | no ALLOW can drive `remaining` below zero when the runtime supplies `estimated_cost_usd`. |
| **P7** | HITL_ALWAYS | `hitl_mode == "always"` ∧ no higher-precedence rule ⇒ HITL. |
| **P8** | Fail closed | context that cannot be evaluated ⇒ HITL, never a silent ALLOW. |
| **P9** | Bound authorization | a `pending_transition` releases exactly one `(nonce, args_digest)` pair; every other call ⇒ DENY. |

> **P6 is conditional and P3 is scoped.** A fresh state does *not* imply ALLOW:
> a first-ever call carrying opaque context returns HITL, and one carrying
> non-canonicalizable params returns DENY. Rule 1 precedes Rule 2, so a call
> with malformed params reports `malformed_input` rather than `circuit_open`
> even when the circuit is tripped — both are closed, but the reason differs
> and the reason is normative.

### 3.4 Verdict Types

| Verdict | Meaning | Runtime obligation |
|---------|---------|--------------------|
| **ALLOW** | Execute as requested | Proceed. |
| **DENY** | Block execution | Do not execute. Do not mutate budget or counters on behalf of the call. Surface the reason. |
| **HITL** | Human decision required | Do not execute. Escalate to a human. In a non-interactive deployment (proxy, daemon, CI) there is no human, so HITL MUST be treated as a block — never downgraded to ALLOW. |

### 3.5 Reason Vocabulary

Reasons are lowercase, stable, and namespaced with `:` where a family exists.

| Reason | Verdict | Trigger |
|--------|---------|---------|
| `within_thresholds` | ALLOW | No rule matched. |
| `circuit_open` | DENY | Circuit previously tripped. |
| `budget_exhausted` | DENY | `remaining <= 0`. |
| `budget_overrun` | DENY | This call's estimated cost exceeds `remaining`. |
| `max_repeat_exceeded` | DENY | Same tool repeated past the limit. |
| `policy_violation:malformed_input` | DENY | `params` not canonicalizable. |
| `policy_violation:duplicate_nonce` | DENY | Replayed nonce. |
| `policy_violation:duplicate_resume_no_effect` | DENY | Re-dispatch of an already-terminal transition. |
| `hitl_transition:approve` | ALLOW | Authorized `(nonce, digest)` pair released. |
| `hitl_transition:modify_successor` | ALLOW | The successor of a MODIFY, with the successor's arguments. |
| `hitl_transition:reject` | DENY | Human rejected. |
| `hitl_transition:superseded_original` | DENY | The original of a MODIFY tried to execute. |
| `hitl_transition:terminal_no_effect` | DENY | Approval already consumed/expired/revoked. |
| `hitl_transition:nonce_mismatch` | DENY | Call is not the authorized call. |
| `hitl_transition:digest_mismatch` | DENY | Arguments differ from the approved arguments. |
| `hitl_transition:unbound_authorization` | DENY | Approval bound neither a nonce nor a digest. |
| `hitl_transition:unknown_decision` | DENY | Unrecognized human decision. |
| `hitl_transition:defer_escalate` | HITL | Deferred or escalated. |
| `hitl_all_calls` | HITL | `hitl_mode == "always"`. |
| `budget_threshold` | HITL | Remaining fraction at or below threshold. |
| `fail_closed:opaque_context` | HITL | Context not deterministically evaluable. |

### 3.6 The HITL Transition Contract

**A human approval is a single-use capability over one specific preimage. It
is not a permission to call the tool.**

`pending_transition` carries the human's `decision` plus the binding fields
`original_nonce`, `original_args_digest`, `successor_nonce`,
`successor_args_digest` and `terminal_status`. Resolution:

1. `decision` not in {approve, reject, modify, defer, escalate}
   → DENY `unknown_decision`. Forward compatibility is not a licence to execute.
2. `terminal_status` is terminal ∧ this is a resume attempt
   → DENY `duplicate_resume_no_effect`.
3. `reject` → DENY. `defer` / `escalate` → HITL.
4. `modify` → the authorized pair is `(successor_nonce, successor_args_digest)`;
   a call matching `original_nonce` is DENIED as `superseded_original`.
   `approve` → the authorized pair is `(original_nonce, original_args_digest)`,
   and a terminal status DENIES as `terminal_no_effect`.
5. If neither an authorized nonce nor an authorized digest is bound
   → DENY `unbound_authorization`.
6. Nonce mismatch → DENY. `canonical_hash(call.params)` ≠ authorized digest
   → DENY. Otherwise → ALLOW.

This is the machinery behind the invariant **history_visible ≠
runtime_executable**: a decision that is legible in the audit trail is not
thereby replayable at runtime.

### 3.7 Non-Normative Layers

`v2/spec/decide.py` additionally implements time-window limits
(`WINDOW_EXCEEDED`), a session-wide call cap (`GLOBAL_LIMIT`), probabilistic
denial under budget pressure, and error-signal amplification of the repeat
threshold (§3.8). **None of these are certified by SP/1.0.** No published
vector exercises them, the certified core does not implement them, and an
implementation that omits them is fully conformant. They are described here so
readers of that module know where its behaviour comes from, and are candidates
for a future revision that ships vectors for them.

### 3.8 Fixture Markers vs Structural Detection

JSON cannot literally encode NaN, ±Infinity, or a non-string object key. The
language-neutral fixtures therefore declare that class with a reserved marker,
`{"__noncanonical__": true}`. **The marker is a fixture-encoding device, not
the rule.** A conforming implementation must reject the underlying class
structurally; passing `malformed_non_canonical_input` by special-casing the
marker string is not conformance. `fixtures/conformance-1.0.1.json` and
`tests/test_sp101_regressions.py` probe the structural behaviour directly.

### 3.9 Error Signal Amplification — Non-Normative

> **Non-normative (§3.7).** Error amplification lives in `shackle/core.py`
> (which folds it into the `repeat_counts` it passes to `decide()`) and in
> `v2/spec/decide.py`. The certified core has no error-signal concept: it
> reads the repeat count it is given.

SHACKLE detects error signals in tool parameters WITHOUT regex (no ReDoS attack surface). When `error_amplification` is enabled, the repeat call threshold is reduced by 1 if the parameters contain known error signals:

```
Error signals: 401, unauthorized, 403, forbidden, 500, 502, 503,
               504, timeout, connection refused, connection reset,
               permission denied, rate limit, quota exceeded,
               invalid api key, token expired, model not found,
               resource exhausted, deadline exceeded
```

This catches the "loop of death" — agent hits 401, retries, gets 401, retries — without waiting for the full repeat threshold.

### 3.10 Probabilistic Denial (Adversarial Hardening) — Non-Normative

When `probabilistic_deny` is enabled and the agent is below 20% budget, a random factor is introduced:

```
probability = deny_jitter_ratio × (1.0 − budget_ratio)
IF rng < probability:
    → DENY (appears as BUDGET_EXHAUSTED)
```

This prevents adversarial agents from probing the exact budget threshold and
gaming it. The denial appears identical to a real budget exhaustion, making it
impossible to distinguish signal from noise.

> **Non-normative (§3.7).** This layer exists only in `v2/spec/decide.py`. It is
> deliberately excluded from the certified core: it takes an `rng_float`
> argument, so a decision surface that used it would no longer satisfy P2
> (determinism) and could not be pinned by fixtures. A runtime that wants
> jitter must apply it OUTSIDE `decide()`.

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

**<https://github.com/Fame510/SHACKLE>**

| Component | File | Status |
|-----------|------|--------|
| **Certified decision function** | `shackle/conformance.py` | ✅ Normative — the SP/1.0 surface (§3) |
| Conformance vectors | `fixtures/conformance.json` | ✅ 15 vectors, hash-sealed |
| SP/1.0.1 adversarial vectors | `fixtures/conformance-1.0.1.json` | ✅ Tightening probes |
| Conformance harness | `tests/test_conformance.py` | ✅ Runs the vectors |
| Forward-looking typed core | `v2/spec/decide.py` | ⬜ Non-normative (§3.7); benchmarks only |
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

Install: `pip install git+https://github.com/Fame510/SHACKLE.git`

Verify conformance: `python tests/test_conformance.py`

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

## Appendix D: Spend-Mediation Interoperability Profile — Non-Normative

SP/1.0 mediates **tool calls**. A spend-mediation layer — a transaction or budget firewall — mediates **money**, and its decision object is typically `{amount, merchant, category}` rather than an action envelope. The two are not competing implementations of one surface. Most SP/1.0 conformance vectors have no counterpart in a spend firewall (nonce/replay, circuit breaker, opaque context, and every HITL transition), because those objects are not modeled there at all. A missing counterpart is a scope boundary, not a defect in either layer.

**The shared surface is budget.** Where a spend-mediation layer and an SP/1.0 guard both observe a cumulative spend limit, their decisions are directly comparable. This profile defines exactly where:

| Vector | Shared meaning | SP/1.0 verdict |
|--------|----------------|----------------|
| `within_thresholds` | Spend below every configured limit | `ALLOW` |
| `budget_exhausted` | Configured budget fully consumed | `DENY` |
| `budget_overrun` | Call would exceed remaining budget | `DENY` |

An implementation is **spend-profile compatible** when it reproduces those three vectors from `fixtures/conformance.json` with matching verdicts. That claim covers the budget surface only; it says nothing about the remaining vectors and MUST NOT be presented as full SP/1.0 conformance.

Two boundary rules apply to any layer claiming this profile:

1. **Fail closed on malformed input.** Input that cannot be canonicalized resolves to `DENY`, never to a medium-severity flag. A severity signal is an observation; a verdict is enforcement.
2. **Escalation is a verdict, not a severity.** `ALLOW | DENY | HITL` is not satisfied by `APPROVED | BLOCKED | FLAGGED`. A flag with no defined authorization path is not the HITL transition of §3.6.

Fixtures for this profile are the sealed `vector_hash` entries as of SP/1.0.1 (2026-07-30), so an independent re-run diffs against published hashes rather than against prose. Profile discussion: `Significant-Gravitas/AutoGPT#12700`.

---

*SP/1.0 — Sovereign Logic, June 2026. Licensed under CC-BY 4.0.*  
*Reference implementation: AGPLv3 + Commercial. Contact: docspoc101@gmail.com*
