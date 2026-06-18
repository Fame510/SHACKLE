# SHACKLE V2 Deployment Guide

## Quick Start (5 minutes)

```bash
# 1. Run quickstart
./quickstart.sh

# 2. Start daemon (in separate terminal)
python daemon.py

# 3. Run tests
python test_daemon.py

# 4. Try examples
python example_usage.py
```

## Production Deployment

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- 2GB RAM minimum
- 10GB disk space

### Step 1: Clone and Setup

```bash
cd /root/clawd/SHACKLE-V2-DAEMON
pip install -r requirements.txt
```

### Step 2: Configure Environment

```bash
cp .env.example .env
vim .env
```

Set:
- `REDIS_URL`: Redis connection string
- `POSTGRES_URL`: Postgres connection string
- `SHACKLE_SOCKET`: Unix socket path
- `SHACKLE_SIGNING_KEY`: Ed25519 key (generate once, keep secure)

### Step 3: Generate Signing Key

```bash
python -c "from nacl.signing import SigningKey; print(SigningKey.generate().encode().hex())" > signing_key.txt
chmod 600 signing_key.txt
export SHACKLE_SIGNING_KEY=$(cat signing_key.txt)
```

⚠️ **CRITICAL**: Back up `signing_key.txt` securely. Loss = cannot verify old logs.

### Step 4: Start Infrastructure

```bash
docker-compose up -d redis postgres
```

Wait for health checks:
```bash
docker-compose ps
```

### Step 5: Initialize Database

Database schema is auto-created on first connection. Verify:

```bash
docker exec -it shackle-postgres psql -U shackle -d shackle -c "\dt"
```

Should show `audit_log` table.

### Step 6: Start Daemon

**Option A: Foreground (development)**
```bash
python daemon.py
```

**Option B: Systemd Service (production)**

Create `/etc/systemd/system/shackle.service`:

```ini
[Unit]
Description=SHACKLE Sovereign Daemon
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=agent
WorkingDirectory=/root/clawd/SHACKLE-V2-DAEMON
EnvironmentFile=/root/clawd/SHACKLE-V2-DAEMON/.env
ExecStart=/usr/bin/python3 daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable shackle
sudo systemctl start shackle
sudo systemctl status shackle
```

**Option C: Docker Compose (all-in-one)**
```bash
docker-compose up -d
```

### Step 7: Verify

```bash
# Health check
curl --unix-socket /tmp/shackle.sock http://localhost/health

# CLI check
python cli.py health

# Run tests
python test_daemon.py
```

## Client Integration

### Python Application

```python
from client import ShackleClient, shackled

# Initialize client
client = ShackleClient(session_id="myapp_session")

# Wrap tool functions
@shackled(tool_name="my_tool", estimate_cost=lambda x: 0.01, client=client)
async def my_tool(arg: str):
    # Tool implementation
    return {"result": arg}

# Use normally - SHACKLE governs transparently
result = await my_tool("test")
```

### Environment Variables

```bash
export SHACKLE_SOCKET=/tmp/shackle.sock
export SHACKLE_SESSION=myapp_session_123
export REDIS_URL=redis://localhost:6379/0
export POSTGRES_URL=postgresql://shackle:shackle@localhost:5432/shackle
```

## Monitoring

### Health Checks

Add to monitoring system:

```bash
# Daemon health
curl --unix-socket /tmp/shackle.sock http://localhost/health

# Redis health
docker exec shackle-redis redis-cli ping

# Postgres health
docker exec shackle-postgres pg_isready -U shackle
```

### Logs

**Daemon logs:**
```bash
# If using systemd
journalctl -u shackle -f

# If using docker-compose
docker-compose logs -f daemon
```

**Application logs:**
```bash
# Filter SHACKLE logs
grep "SHACKLE" /var/log/myapp.log
```

### Metrics

Expose metrics endpoint (add to `daemon.py`):

```python
from prometheus_client import Counter, Histogram, generate_latest

pre_exec_requests = Counter('shackle_pre_exec_total', 'Total pre_exec requests', ['decision'])
pre_exec_latency = Histogram('shackle_pre_exec_duration_seconds', 'Pre-exec latency')

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type="text/plain")
```

### Grafana Dashboard

Example queries:

```promql
# Decision rate by type
rate(shackle_pre_exec_total[5m])

# HITL queue depth
shackle_hitl_pending_total

# Budget utilization
shackle_budget_percentage

# Error rate
rate(shackle_errors_total[5m])
```

## Scaling

### Horizontal Scaling (Multiple Daemons)

SHACKLE daemons are stateless - scale horizontally:

```yaml
# docker-compose.yml
services:
  daemon:
    deploy:
      replicas: 3
    # Shared Redis/Postgres
```

Load balance with HAProxy or nginx:

```nginx
upstream shackle_daemons {
    server unix:/tmp/shackle1.sock;
    server unix:/tmp/shackle2.sock;
    server unix:/tmp/shackle3.sock;
}
```

### Vertical Scaling (Resource Limits)

```yaml
services:
  redis:
    deploy:
      resources:
        limits:
          memory: 1G
  
  postgres:
    deploy:
      resources:
        limits:
          memory: 2G
```

### Database Partitioning

For high volume (>10M logs/day), partition audit table:

```sql
CREATE TABLE audit_log_2024_01 PARTITION OF audit_log
FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

CREATE TABLE audit_log_2024_02 PARTITION OF audit_log
FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');
```

## Backup & Recovery

### Redis Backup

Redis persistence enabled (AOF):

```bash
# Backup
docker exec shackle-redis redis-cli BGSAVE
docker cp shackle-redis:/data/dump.rdb ./backup/

# Restore
docker cp ./backup/dump.rdb shackle-redis:/data/
docker-compose restart redis
```

### Postgres Backup

```bash
# Backup
docker exec shackle-postgres pg_dump -U shackle shackle > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i shackle-postgres psql -U shackle shackle < backup_20240115.sql
```

### Automated Backups

Add to cron:

```bash
#!/bin/bash
# /root/clawd/SHACKLE-V2-DAEMON/backup.sh

BACKUP_DIR=/backup/shackle
DATE=$(date +%Y%m%d_%H%M%S)

# Backup Postgres
docker exec shackle-postgres pg_dump -U shackle shackle | gzip > $BACKUP_DIR/postgres_$DATE.sql.gz

# Backup Redis
docker exec shackle-redis redis-cli BGSAVE
sleep 5
docker cp shackle-redis:/data/dump.rdb $BACKUP_DIR/redis_$DATE.rdb

# Cleanup old backups (keep 30 days)
find $BACKUP_DIR -name "*.gz" -mtime +30 -delete
find $BACKUP_DIR -name "*.rdb" -mtime +30 -delete

echo "Backup completed: $DATE"
```

```bash
# Add to cron (daily at 2 AM)
0 2 * * * /root/clawd/SHACKLE-V2-DAEMON/backup.sh
```

## Security Hardening

### 1. Socket Permissions

```bash
chmod 0600 /tmp/shackle.sock
chown agent:agent /tmp/shackle.sock
```

### 2. Database Encryption

Enable SSL for Postgres:

```yaml
postgres:
  environment:
    POSTGRES_SSL_MODE: require
  volumes:
    - ./certs/server.crt:/var/lib/postgresql/server.crt:ro
    - ./certs/server.key:/var/lib/postgresql/server.key:ro
```

### 3. Network Isolation

```yaml
networks:
  internal:
    driver: bridge
    internal: true  # No external access

services:
  redis:
    networks:
      - internal
  postgres:
    networks:
      - internal
```

### 4. Secrets Management

Use Docker secrets:

```yaml
services:
  daemon:
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
      SHACKLE_SIGNING_KEY_FILE: /run/secrets/signing_key
    secrets:
      - postgres_password
      - signing_key

secrets:
  postgres_password:
    file: ./secrets/postgres_password.txt
  signing_key:
    file: ./secrets/signing_key.txt
```

### 5. Rate Limiting

Add to `daemon.py`:

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/pre_exec")
@limiter.limit("100/minute")
async def pre_exec(request: Request, req: PreExecRequest):
    ...
```

## Troubleshooting

### Daemon Won't Start

**Symptom**: `daemon.py` exits immediately

**Check**:
```bash
# Socket path writable?
touch /tmp/shackle.sock

# Redis accessible?
docker exec shackle-redis redis-cli ping

# Postgres accessible?
docker exec shackle-postgres pg_isready -U shackle

# Check logs
python daemon.py 2>&1 | tee daemon.log
```

### Client Can't Connect

**Symptom**: `ShackleClient.check_daemon()` returns False

**Check**:
```bash
# Socket exists?
ls -la /tmp/shackle.sock

# Daemon running?
curl --unix-socket /tmp/shackle.sock http://localhost/health

# Permissions?
sudo chmod 666 /tmp/shackle.sock  # For testing only!
```

### HITL Timeout

**Symptom**: Client waits 5 minutes then fails

**Fix**:
1. Increase timeout in `daemon.py`:
   ```python
   resp = await asyncio.wait_for(future, timeout=600.0)  # 10 min
   ```

2. Use WebSocket for async notification:
   ```javascript
   const ws = new WebSocket('ws://unix:/tmp/shackle.sock:/ws');
   ws.onmessage = handleHITL;
   ```

### Budget Not Enforcing

**Symptom**: Calls allowed despite budget exceeded

**Check**:
```bash
# Check Redis keys
docker exec shackle-redis redis-cli --scan --pattern "shackle:budget:*"

# Check budget status
python cli.py budget mysession

# Set explicit limit
python cli.py set-budget mysession 10.0
```

### Audit Logs Not Appearing

**Symptom**: No entries in Postgres

**Check**:
```bash
# Table exists?
docker exec -it shackle-postgres psql -U shackle -d shackle -c "\dt"

# Permissions?
docker exec -it shackle-postgres psql -U shackle -d shackle -c "INSERT INTO audit_log (timestamp, event_type, session_id, tool_name, signature) VALUES (NOW(), 'test', 'test', 'test', 'test');"

# Check daemon logs for errors
docker-compose logs daemon | grep audit
```

## Migration

### From V1 to V2

V2 is a complete rewrite - no direct migration path.

**Strategy**:
1. Run V2 alongside V1
2. Gradually move tools to V2 client
3. Export V1 logs if needed:
   ```bash
   # Export V1 data (format-specific)
   ```
4. Decommission V1 after full migration

### Database Schema Updates

For schema changes, use migrations:

```python
# migrations/001_add_metadata_column.py
async def upgrade(conn):
    await conn.execute("""
        ALTER TABLE audit_log 
        ADD COLUMN IF NOT EXISTS metadata JSONB
    """)

async def downgrade(conn):
    await conn.execute("""
        ALTER TABLE audit_log 
        DROP COLUMN IF EXISTS metadata
    """)
```

Run:
```bash
python run_migrations.py
```

## Performance Tuning

### Redis

```conf
# redis.conf
maxmemory 512mb
maxmemory-policy allkeys-lru
appendonly yes
appendfsync everysec
```

### Postgres

```conf
# postgresql.conf
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
work_mem = 4MB
min_wal_size = 1GB
max_wal_size = 4GB
```

### Application

```python
# Increase connection pools
state_manager = StateManager(redis_url, pool_size=20)
audit_logger = AuditLogger(postgres_url, pool_min_size=5, pool_max_size=20)
```

## Cost Analysis

### Infrastructure Costs

**AWS Example (us-east-1):**
- EC2 t3.medium (2 vCPU, 4GB RAM): $30/month
- ElastiCache Redis (cache.t3.micro): $12/month
- RDS Postgres (db.t3.micro): $15/month
- EBS storage (50GB): $5/month

**Total**: ~$62/month

### Operational Costs

- Monitoring (CloudWatch): $10/month
- Backups (S3): $2/month
- Total: ~$74/month

### Cost Savings

SHACKLE prevents runaway costs:
- Budget limits prevent overspend
- Repeat detection stops infinite loops
- HITL catches expensive operations

**ROI**: Pays for itself if it prevents 1 accidental $100+ API call per month.

## Support

- **Documentation**: See README.md, ARCHITECTURE.md
- **Issues**: File on GitHub
- **Community**: Discord/Slack
- **Email**: support@shackle.dev

## License

**AGPLv3** — GNU Affero General Public License v3.0.
See the root [LICENSE](../../LICENSE) file for full terms.

Commercial licensing available for closed-source deployments.
Contact: docspoc101@gmail.com
