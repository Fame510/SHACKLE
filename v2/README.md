# SHACKLE V2: Enterprise Runtime Sovereignty Layer

**Status:** Production-ready optional upgrade for V1 users

---

## What's New in V2

SHACKLE V1 is a **single-process decorator** that works great for local development and CLI workflows. V2 adds **distributed state** and **enterprise compliance** for production deployments.

### V1 (Current - Always Free)
- ✅ One decorator (`@Guard`)
- ✅ Works locally, in-process
- ✅ Perfect for development, testing, CLI agents
- ✅ Zero infrastructure required

### V2 (Optional Upgrade)
- ✅ **Distributed state** (budget shared across serverless functions, Lambda, K8s pods)
- ✅ **Postgres audit logs** (cryptographically signed with Ed25519)
- ✅ **Remote HITL** (control headless agents from mobile/web)
- ✅ **SOC2 compliance pack** (for regulated industries)
- ✅ **Commercial licensing** (for closed-source products)

---

## Components

### `/v2/protocol`
- Complete wire protocol specification
- Protobuf message schemas
- Version negotiation, transport bindings
- Reference implementation (Python)

### `/v2/daemon`
- FastAPI server (Unix socket + WebSocket)
- Redis state engine (distributed budgets)
- Postgres audit logger (signed, immutable logs)
- Thin client library (drop-in for V1 `@Guard`)

### `/v2/compliance`
- Commercial license server
- AI Agent Liability Shield (SOC2 mapping PDF)
- Audit export API
- Enterprise onboarding docs

---

## When to Use V2

**Stick with V1 if:**
- You're developing locally
- Single-process agents
- CLI workflows with human supervision
- Don't need audit trail compliance

**Upgrade to V2 if:**
- Multi-process (serverless, Lambda, K8s)
- Need audit logs for compliance (SOC2, ISO27001, HIPAA)
- Remote agent control (headless APIs)
- Closed-source commercial product (need commercial license)

---

## Quick Start (V2 Daemon)

```bash
# 1. Install dependencies
cd v2/daemon
pip install -r requirements.txt

# 2. Start infrastructure (Redis + Postgres)
docker-compose up -d

# 3. Start daemon
python daemon.py &

# 4. Verify
./verify.sh
```

### Use V2 in Your Code (Drop-in Replacement)

```python
# V1 (still works)
from shackle import Guard

@Guard(budget=0.25)
def run():
    return crew.kickoff()

# V2 (distributed state)
from v2.daemon.client import ShackleClient, shackled

client = ShackleClient(session_id="my-app", budget_limit=10.00)

@shackled(tool_name="kickoff", estimate_cost=lambda: 0.50, client=client)
def run():
    return crew.kickoff()
```

**Difference:** V2 tracks budget across **all processes sharing the same `session_id`**. Perfect for serverless.

---

## Pricing

**V1 (Open-source):** Free for open-source projects (AGPLv3)

**V2 Commercial License:**
- Custom pricing based on team size, deployment scale, and requirements
- Available for startups, enterprises, and framework partnerships
- Includes architecture audit, integration support, and SLA-backed support

**Implementation Service:** $2,500 (V1 or V2 setup + architecture audit)

📧 **Contact for pricing:** docspoc101@gmail.com

---

## Documentation

- **[Protocol Spec](protocol/PROTOCOL.md)** — Wire format, message schemas
- **[Daemon Guide](daemon/README.md)** — Deployment, configuration
- **[Compliance Pack](compliance/AI-Agent-Liability-Shield.pdf)** — SOC2 mapping for CISOs

---

**Built by Dante Bullock, Sovereign Logic**  
No VC. No corporate sponsors. Just code that works.
