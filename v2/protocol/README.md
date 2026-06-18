# SHACKLE V2.0 Protocol Specification

**Secure Hook Architecture for Controlled Kernel-Level Execution**

Version: 2.0.0  
Status: Draft  
Created: 2025-01-23  

---

## Overview

SHACKLE is a zero-trust command execution monitoring protocol that enables real-time authorization and audit of system commands with human-in-the-loop (HITL) capabilities.

### Key Features

- ✅ **Zero-trust execution**: Every command requires explicit authorization
- ✅ **Human-in-the-loop**: Escalate risky commands to human operators
- ✅ **Low-latency**: <10ms typical authorization time
- ✅ **Fail-secure**: Network failures default to DENY
- ✅ **Complete audit trail**: Full execution logging with correlation IDs
- ✅ **Version negotiation**: Backward compatibility support

---

## Quick Start

### 1. Review the Protocol Specification

Start with [`PROTOCOL.md`](PROTOCOL.md) for the complete formal specification including:
- Transport layer (Unix socket + WebSocket)
- Message types and flows
- Verdict system (ALLOW/DENY/HITL)
- Error handling
- Security considerations

### 2. Explore Protocol Examples

See [`protocol-examples.md`](protocol-examples.md) for practical examples:
- Connection lifecycle (register, heartbeat, disconnect)
- Execution flow (pre_exec, post_exec)
- HITL request/response patterns
- Error scenarios
- Wire format examples

### 3. Generate Protobuf Code

The protocol uses Protocol Buffers for efficient serialization:

```bash
# Install protobuf compiler
sudo apt-get install protobuf-compiler  # Ubuntu/Debian
brew install protobuf                    # macOS

# Generate Python bindings
protoc --python_out=. shackle.proto

# Generate Go bindings
protoc --go_out=. shackle.proto

# Generate other languages as needed
protoc --cpp_out=. shackle.proto    # C++
protoc --java_out=. shackle.proto   # Java
protoc --rust_out=. shackle.proto   # Rust
```

### 4. Run the Reference Implementation

Python reference implementations are provided:

```bash
# Install dependencies
pip install -r requirements.txt

# Terminal 1: Start Guardian (server)
python server_stub.py --socket /tmp/shackle-test.sock

# Terminal 2: Run client example
python client_stub.py
```

**Note**: The reference implementations use JSON over the wire for readability. Production implementations should use Protocol Buffers.

---

## Repository Contents

```
SHACKLE-V2-PROTOCOL/
├── README.md                  # This file
├── PROTOCOL.md                # Formal protocol specification
├── shackle.proto              # Protocol Buffer schemas
├── protocol-examples.md       # Practical examples and test payloads
├── client_stub.py             # Python client reference implementation
├── server_stub.py             # Python Guardian reference implementation
├── requirements.txt           # Python dependencies
└── examples/                  # Additional examples (future)
```

---

## Architecture

```
┌─────────────┐         ┌──────────────┐         ┌──────────────┐
│   Client    │◄───────►│   Guardian   │◄───────►│   Operator   │
│  (Process)  │  Unix/WS│   (Policy)   │  WebUI  │   (Human)    │
└─────────────┘         └──────────────┘         └──────────────┘
     │                         │
     │  1. pre_exec           │
     ├────────────────────────►│
     │                         │ 2. Policy Check
     │                         │ 3. HITL (if needed)
     │  4. Verdict            │
     │◄────────────────────────┤
     │                         │
     │  5. Execute             │
     │                         │
     │  6. post_exec          │
     ├────────────────────────►│
```

### Components

1. **Client**: Shell integration or process wrapper that intercepts commands
2. **Guardian**: Central authorization service with policy engine
3. **Operator**: Human decision-maker for HITL escalations (via web UI)

---

## Protocol Messages

### Core Message Types

| Message | Direction | Purpose |
|---------|-----------|---------|
| `register` | Client → Guardian | Establish session |
| `heartbeat` | Bidirectional | Keepalive |
| `pre_exec` | Client → Guardian | Request authorization |
| `post_exec` | Client → Guardian | Report outcome |
| `hitl_request` | Guardian → Operator | Escalate to human |
| `hitl_response` | Operator → Guardian | Human decision |

### Verdict Types

| Verdict | Meaning |
|---------|---------|
| `ALLOW` | Execute command as-is |
| `DENY` | Block execution |
| `HITL` | Wait for human approval |
| `MODIFY` | Execute modified command |

---

## Transport Options

### Unix Domain Socket (Primary)

- **Path**: `/var/run/shackle/guardian.sock`
- **Framing**: Length-prefixed (4 bytes big-endian + protobuf payload)
- **Use case**: Local process monitoring

```python
# Python example
import socket, struct

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/var/run/shackle/guardian.sock")

# Send message
payload = serialize_protobuf(message)
sock.sendall(struct.pack(">I", len(payload)) + payload)

# Receive response
length = struct.unpack(">I", sock.recv(4))[0]
response = sock.recv(length)
```

### WebSocket (Secondary)

- **Endpoint**: `ws://localhost:8765/shackle/v2` or `wss://` for TLS
- **Authentication**: Bearer token in `Authorization` header
- **Use case**: Remote monitoring, web UI

```javascript
// JavaScript example
const ws = new WebSocket('ws://localhost:8765/shackle/v2', {
  headers: { 'Authorization': 'Bearer <token>' }
});

ws.on('message', (data) => {
  const message = deserialize_protobuf(data);
  // Handle message
});
```

---

## Implementation Guide

### Minimum Viable Client

Required features for a basic client:

- [x] Register with Guardian
- [x] Send pre_exec before each command
- [x] Handle ALLOW/DENY verdicts
- [x] Send post_exec after execution
- [x] Heartbeat every 30s
- [x] Reconnect on connection loss

### Minimum Viable Guardian

Required features for a basic Guardian:

- [x] Accept client registrations
- [x] Policy evaluation (whitelist/blacklist)
- [x] Return verdicts
- [x] Audit logging
- [x] HITL queue management
- [x] Session lifecycle

### Production Checklist

Additional requirements for production deployment:

- [ ] TLS for WebSocket transport
- [ ] JWT token authentication
- [ ] Rate limiting
- [ ] Verdict caching
- [ ] Metrics export (Prometheus)
- [ ] Health check endpoint
- [ ] Graceful shutdown
- [ ] Hot config reload
- [ ] Structured logging (JSON)
- [ ] Performance profiling

---

## Testing

### Unit Tests

Test individual protocol components:

```bash
# Example using pytest
pytest test_protocol.py -v

# Test specific message types
pytest test_protocol.py::test_register_request
pytest test_protocol.py::test_pre_exec_verdict
```

### Integration Tests

Test full client-guardian interaction:

```bash
# Start test guardian
./test/start_test_guardian.sh

# Run integration tests
pytest test_integration.py -v

# Stop guardian
./test/stop_test_guardian.sh
```

### Load Tests

Performance testing:

```bash
# Simulate 10,000 requests/sec
python test/load_test.py --rps 10000 --duration 60

# Expected results:
# - P50 latency: <5ms
# - P99 latency: <50ms
# - 0% errors
```

---

## Security Considerations

### Authentication

- **Unix Socket**: Filesystem permissions + `SO_PEERCRED` validation
- **WebSocket**: JWT bearer tokens with role-based access

### Authorization

- Guardian enforces RBAC for operators
- Roles: `operator` (HITL), `auditor` (read-only), `admin` (config)

### Audit Logging

All events logged with:
- Microsecond timestamps
- Correlation IDs (session_id, execution_id)
- User identity and context
- Verdict rationale

### Replay Protection

- UUIDv4 message IDs
- 1-hour sliding window deduplication
- Duplicate detection → `DUPLICATE_MESSAGE` error

---

## Performance

### Latency Targets

| Operation | Target | P99 |
|-----------|--------|-----|
| Pre-exec (cached) | <1ms | <5ms |
| Pre-exec (policy) | <10ms | <50ms |
| Pre-exec (HITL) | <2s | <60s |
| Post-exec | <5ms | <20ms |

### Throughput

- Guardian: 10,000 pre-exec/sec per core
- Client overhead: <0.5ms per exec()

### Resource Usage

- **Guardian**: <100MB RAM baseline, <5% CPU idle
- **Client**: <1MB RAM per process

---

## Version Compatibility

Protocol follows semantic versioning: `MAJOR.MINOR.PATCH`

| Guardian | Client | Compatible? |
|----------|--------|-------------|
| 2.0.x | 2.0.x | ✅ Full |
| 2.1.x | 2.0.x | ✅ Backward |
| 2.0.x | 2.1.x | ⚠️ Forward (limited) |
| 3.0.x | 2.x.x | ❌ Incompatible |

### Negotiation Process

1. Client requests highest supported version
2. Guardian responds with highest mutual version
3. Both use negotiated version for session
4. Major version mismatch → connection rejected

---

## Extensions

The protocol supports custom extensions via:

1. **Capability negotiation**: Advertise optional features
2. **Metadata fields**: Custom key-value pairs
3. **Reserved field numbers**: Fields 90-99 for metadata, 100+ for extensions

Example custom capability:
```json
{
  "capabilities": ["hitl", "async", "x-acme-gpu-tracking"]
}
```

---

## Roadmap

### Version 2.1 (Planned)

- [ ] Batch pre-exec requests
- [ ] Streaming stdout/stderr during execution
- [ ] Policy rule versioning and rollback
- [ ] Multi-factor authentication for HITL

### Version 3.0 (Future)

- [ ] Plugin architecture for custom policy engines
- [ ] Distributed Guardian deployment
- [ ] ML-based anomaly detection
- [ ] Container/Kubernetes integration

---

## Contributing

### Reporting Issues

Report protocol issues or ambiguities:
- Open GitHub issue with example payloads
- Include protocol version and implementation language

### Proposing Changes

Protocol changes require:
1. RFC document with rationale
2. Example message flows
3. Backward compatibility analysis
4. Reference implementation

### Code Style

- Python: Follow PEP 8
- Protobuf: Use `snake_case` for fields
- Documentation: Markdown with code examples

---

## License

**Dual-licensed:**

- **Protocol specification** (`PROTOCOL.md`, `shackle.proto`): Creative Commons Attribution 4.0 International (CC-BY 4.0) — free to implement, attribute required.
- **Reference implementations** (`client_stub.py`, `server_stub.py`, all code): **AGPLv3** — GNU Affero General Public License v3.0.

See the root [LICENSE](../../LICENSE) file for full AGPLv3 terms.

Using SHACKLE in a closed-source product? Commercial licensing is available.
Contact: docspoc101@gmail.com

---

## Contact & Support

- **Documentation**: See [`PROTOCOL.md`](PROTOCOL.md)
- **Examples**: See [`protocol-examples.md`](protocol-examples.md)
- **Issues**: GitHub Issues
- **Discussions**: GitHub Discussions

---

## Acknowledgments

SHACKLE was designed with inspiration from:
- SELinux policy framework
- systemd socket activation
- Kubernetes admission controllers
- OpenSSH certificate authorities

Special thanks to the Protocol Buffers and WebSocket communities.

---

**End of README**
