# SHACKLE V2 Daemon

Sovereign governance daemon for tool execution control with budget tracking, repeat call detection, and human-in-the-loop intervention.

## Architecture

```
┌─────────────────┐
│  Tool Client    │
│  (decorator)    │
└────────┬────────┘
         │ Unix Socket
         ▼
┌─────────────────┐
│  FastAPI Daemon │
│  daemon.py      │
└────┬───────┬────┘
     │       │
     ▼       ▼
┌─────┐  ┌─────┐
│Redis│  │ PG  │
│state│  │audit│
└─────┘  └─────┘
```

## Components

### 1. daemon.py
FastAPI server with:
- Unix socket communication
- WebSocket support for HITL
- Pre-execution authorization (`/pre_exec`)
- Post-execution logging (`/post_exec`)
- HITL flow (`/hitl_response`, `/hitl_wait`)

### 2. state.py
Redis state manager:
- Budget tracking per session
- Repeat call detection (hash-based)
- Session state management
- Automatic expiry (24h budgets, 1h call history)

### 3. audit.py
Postgres audit logger:
- Append-only logs
- Ed25519 signatures for integrity
- Decision logging (ALLOW/DENY/HITL)
- Execution logging with timing and costs
- Cryptographic verification

### 4. client.py
Thin decorator client:
- Auto-detect daemon availability
- Fallback to local execution if daemon down
- `@shackled` decorator for easy integration
- Blocking HITL wait support

### 5. docker-compose.yml
Local dev stack:
- Redis (7-alpine)
- Postgres (15-alpine)
- Daemon container
- Health checks and auto-restart

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Start infrastructure
```bash
docker-compose up -d redis postgres
```

### 3. Run daemon
```bash
export REDIS_URL="redis://localhost:6379/0"
export POSTGRES_URL="postgresql://shackle:shackle@localhost:5432/shackle"
export SHACKLE_SOCKET="/tmp/shackle.sock"

python daemon.py
```

### 4. Use client decorator
```python
from client import shackled, ShackleClient

client = ShackleClient(session_id="my_session")

@shackled(tool_name="my_tool", estimate_cost=lambda x: 0.01, client=client)
async def my_tool(value: str):
    # Your tool implementation
    return {"result": value}

# Tool execution is now governed by SHACKLE
result = await my_tool("test")
```

## Protocol

### Pre-execution check
```json
POST /pre_exec
{
  "session_id": "session_123",
  "tool_name": "exec",
  "parameters": {"command": "ls"},
  "estimated_cost": 0.001
}

Response:
{
  "decision": "ALLOW|DENY|HITL",
  "reason": "Budget exceeded",
  "hitl_token": "hitl_xxx" // if HITL
}
```

### Post-execution logging
```json
POST /post_exec
{
  "session_id": "session_123",
  "tool_name": "exec",
  "parameters": {"command": "ls"},
  "result": {"output": "..."},
  "error": null,
  "actual_cost": 0.001,
  "execution_time_ms": 123.45
}

Response:
{
  "status": "ACK",
  "message": "Execution logged"
}
```

### HITL flow
1. Daemon returns `decision: "HITL"` with `hitl_token`
2. Client polls `/hitl_wait/{token}` (blocks up to 5 min)
3. Human responds via WebSocket or POST `/hitl_response`
4. Blocked client receives final decision (ALLOW/DENY)

## Configuration

### Environment Variables
- `REDIS_URL`: Redis connection string (default: `redis://localhost:6379/0`)
- `POSTGRES_URL`: Postgres connection string
- `SHACKLE_SOCKET`: Unix socket path (default: `/tmp/shackle.sock`)
- `SHACKLE_SESSION`: Default session ID (client)

### Budget Limits
Default: $10 per session (24h expiry)

Set custom limit:
```python
await state_manager.set_budget_limit("session_123", 50.0)
```

### Repeat Call Detection
- Tracks last 100 calls per session (1h expiry)
- Triggers HITL after 3 identical calls
- Hash-based: same tool + parameters

## WebSocket Interface

Connect to `ws://localhost/ws` for real-time HITL notifications:

```javascript
const ws = new WebSocket('ws://unix:/tmp/shackle.sock:/ws');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  
  if (msg.type === 'hitl_request') {
    // Display to human, get decision
    const decision = await getUserDecision(msg.data);
    
    // Send response
    ws.send(JSON.stringify({
      type: 'hitl_response',
      data: {
        hitl_token: msg.data.hitl_token,
        decision: decision, // "ALLOW" or "DENY"
        notes: "User approved"
      }
    }));
  }
};
```

## Audit Log Schema

```sql
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL,  -- 'decision' or 'execution'
    session_id VARCHAR(255) NOT NULL,
    tool_name VARCHAR(255),
    decision VARCHAR(20),              -- ALLOW/DENY/HITL
    reason TEXT,
    parameters JSONB,
    result JSONB,
    error TEXT,
    cost DECIMAL(10, 6),
    execution_time_ms DECIMAL(10, 2),
    signature TEXT NOT NULL,           -- Ed25519 hex signature
    metadata JSONB
);
```

## Testing

### Test daemon health
```bash
curl --unix-socket /tmp/shackle.sock http://localhost/health
```

### Test with client
```bash
python client.py
```

### Run full stack
```bash
docker-compose up
```

## Production Deployment

1. **Secure the signing key**: Load from secure storage (not generate)
2. **Set proper budget limits**: Per session/user
3. **Configure HITL timeout**: Adjust 5-minute default
4. **Monitor Redis/Postgres**: Set up alerts
5. **Use proper authentication**: Add auth middleware
6. **Enable TLS**: For non-Unix socket deployments
7. **Log retention**: Configure Postgres archival

## Performance

- **Latency**: <5ms for pre_exec (Redis lookup)
- **Throughput**: 1000+ req/sec per daemon instance
- **Storage**: ~1KB per audit log entry
- **Redis memory**: ~256MB recommended

## Troubleshooting

### Daemon won't start
- Check socket path permissions
- Verify Redis/Postgres connectivity
- Check logs: `docker-compose logs daemon`

### Client can't connect
- Verify socket path matches
- Check daemon is running: `curl --unix-socket /tmp/shackle.sock http://localhost/health`
- Ensure fallback mode enabled if daemon optional

### HITL timeout
- Default 5 minutes, extend in `/hitl_wait` endpoint
- Check WebSocket connection for notifications
- Verify `hitl_token` is valid

## License

**AGPLv3** — GNU Affero General Public License v3.0.

See the root [LICENSE](../../LICENSE) file for full terms.

Using SHACKLE in a closed-source product? Commercial licensing is available.
Contact: docspoc101@gmail.com
