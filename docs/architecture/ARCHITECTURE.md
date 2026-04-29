# Epic Fury Stock Analyzer — System Architecture

**Prepared:** April 28, 2026
**Version:** 1.0
**Author:** System documentation

## Executive summary

A two-backend automated trading system that uses AWS App Runner for paper trading and public dashboard, plus an EC2 instance for real-money mirror trading via Interactive Brokers. The two backends share state through S3 and operate independently for resilience.

---

## Component-by-component breakdown

### 1. Jackson's Mac (Local development machine)

**What it does:** Code editing, AWS Console access, occasional SSH for EC2 admin.

**Why selected:** Existing computer.

**Connections:**
- → AWS Console (browser/HTTPS) for build/deploy clicks
- → App Runner dashboard (browser/HTTPS) for monitoring
- → EC2 (SSH with mirror-key) for admin
- → GitHub (HTTPS git auth) for pushing code

**When to consider replacement:**
- Mac unavailable for extended periods → use any browser-capable device
- Sharing access with team → setup IAM users for each person

**Considerations:**
- Don't store IBKR credentials on Mac (they live on EC2 only)
- Don't expose SSH key publicly (treat like a password)

---

### 2. Phone (IBKR Mobile App)

**What it does:** Daily 2FA approval push notification.

**Why selected:** IBKR mandates 2FA for all live accounts. Their mobile app is the most reliable second factor.

**Connections:**
- ← IBKR servers (push notification)
- → IBKR servers (approval response)

**When to consider replacement:**
- Don't want to use phone → physical IBKR security device (mailed by IBKR, free)
- Need true zero-touch → switch broker to Alpaca (no 2FA for trading)

**Considerations:**
- Enable Time Sensitive Notifications so 2FA push overrides Do Not Disturb
- Keep IBKR Mobile updated (auth flow changes occasionally)
- If phone is lost, you cannot trade until 2FA recovery

---

### 3. AWS Console + CodeBuild + ECR (Build pipeline)

**What it does:**
- AWS Console: web UI for managing AWS resources
- CodeBuild: builds Docker images from GitHub source
- ECR: stores built Docker images for App Runner to deploy

**Why selected:**
- Native AWS integration with App Runner
- Free tier covers our usage
- No need for separate CI service like CircleCI/GitHub Actions
- Manual trigger gives you control over deploys

**Connections:**
- ← GitHub `main` branch (CodeBuild pulls source)
- → ECR (CodeBuild pushes built image)
- → App Runner (ECR provides image to deploy)

**When to consider replacement:**
- Want auto-deploy on push → switch to GitHub Actions or AWS Amplify
- Multiple environments (dev/staging/prod) → switch to AWS CodePipeline
- Need rollback automation → AWS CodeDeploy with deployment groups

**Considerations:**
- Manual builds = you control when changes go live (safer for trading systems)
- Auto-builds = updates faster but riskier
- Build time: ~3–5 min currently

---

### 4. AWS App Runner (Public backend + frontend)

**What it does:**
- Runs the FastAPI backend 24/7
- Serves React frontend as static files
- Hosts public dashboard at txyz3yv2up.us-east-1.awsapprunner.com
- Runs paper trading engine (no real money)

**Why selected:**
- Fully managed (no server admin needed)
- Auto-scaling, auto-restart, auto-healing
- Built-in HTTPS with managed certs
- Pay-per-use (~$5–15/month for our load)
- Single command deploy
- No infrastructure to manage

**Connections:**
- ← Users (HTTPS via public URL)
- ← ECR (pulls Docker image)
- ↔ S3 (reads DB + IBKR snapshot)
- ← GitHub (indirectly via CodeBuild)

**When to consider replacement:**
- Cost > $50/month → migrate to ECS Fargate or self-managed EC2
- Need WebSockets/streaming → ECS or EC2
- Need GPU compute → SageMaker or EC2 with GPU
- AWS deprecates App Runner (already announced for new customers!) → migrate to ECS Express Mode within 12–18 months
- Need multi-region for global users → CloudFront + multi-region App Runner OR migrate to ECS

**Considerations:**
- App Runner is being phased out for new customers (existing services keep running but no new features)
- Migration to ECS is doable but takes effort (~1–2 days)
- For now, stable enough for personal use

---

### 5. AWS S3 (Shared storage)

**What it does:**
- Stores SQLite database backup (`predictions.db`)
- Stores IBKR snapshot (`ibkr_snapshot.json`)
- Stores cash adjustment flags
- Acts as the "shared truth" between App Runner and EC2

**Why selected:**
- Cheapest reliable storage (~$0.50/month for our data volume)
- 99.999999999% durability (eleven 9s)
- Native AWS integration with IAM roles
- Versioning available if we need history
- Zero admin overhead

**Connections:**
- ← App Runner (reads snapshot for dashboard)
- ↔ EC2 (writes snapshot, writes DB backup)
- ← Both backends on startup (restore DB)

**When to consider replacement:**
- Need real-time pub/sub between backends → Redis or AWS SQS
- Need transactional writes from multiple writers → Postgres (RDS)
- Need queryable structured data → DynamoDB
- Snapshot frequency < 5 sec → switch to Redis or DynamoDB streams

**Considerations:**
- 30-second push interval means dashboard data is up to 30s stale
- That's fine for IBKR trading (positions update slowly)
- For higher frequency needs, consider DynamoDB or SQS FIFO queue

---

### 6. EC2 Instance (IBKR mirror server)

**What it does:**
- Runs the same FastAPI backend as App Runner
- Runs IB Gateway in headless mode
- Runs IBC to manage Gateway auto-restart
- Mirrors paper trades to real IBKR account
- Pushes account snapshots to S3 every 30s

**Why selected:**
- IB Gateway requires a persistent socket connection (no managed service supports this)
- Need full Linux access to install Java + Gateway + IBC
- Need 24/7 uptime for daily trading
- t3.small ($15/month) is the cheapest size that fits 2GB RAM requirement
- IAM instance profile makes S3 access clean (no credentials in code)

**Connections:**
- ← Jackson's Mac (SSH for admin)
- ↔ S3 (reads/writes via IAM role)
- ↔ IB Gateway (localhost:4001)
- ← GitHub (manual `git pull`)

**When to consider replacement:**
- Cost-sensitive → t3.micro ($7/month) — RISKY, only 1GB RAM, will crash Gateway
- Need higher reliability → t3.medium ($30/month) — 4GB RAM, more headroom
- Want to eliminate VM admin → Lambda + IBKR Web API (different architecture, big rewrite)
- Multiple accounts/users → bigger EC2 or fleet of EC2s
- Need 99.99% uptime → Auto Scaling Group with health checks
- AWS region outage concern → multi-region setup with failover

**Considerations:**
- Single point of failure currently (one VM)
- If EC2 crashes, paper trading on App Runner continues but real trading pauses
- Can be mitigated with Auto Scaling Group (1 min, 1 max) for auto-replacement

---

### 7. IB Gateway (Java client for Interactive Brokers)

**What it does:**
- Authenticates to IBKR with username + password + 2FA
- Provides socket API on localhost:4001 for our backend
- Translates our orders into IBKR's TWS API protocol
- Holds the 24h authenticated session

**Why selected:**
- Official IBKR-supported client
- Lightest footprint (vs full TWS — Trader Workstation)
- Free
- Most stable for headless / server use
- Battle-tested by quant industry

**Connections:**
- ← EC2 backend (via TCP localhost:4001)
- → IBKR servers (TLS over internet)
- ← IBC (manages process lifecycle)

**When to consider replacement:**
- Want pure REST API → IBKR Client Portal API (no Gateway, but has session expiry)
- Want OAuth-based auth → IBKR Web API (newer, beta in some regions)
- Switch broker entirely → Alpaca (pure API key, no Gateway needed)

**Considerations:**
- Java is heavyweight (uses ~500MB RAM)
- Needs Xvfb (virtual display) since Gateway has GUI even in headless mode
- IBC handles auto-restart but daily 2FA from phone still required
- Updates ~quarterly from IBKR; we use stable channel

---

### 8. IBC (Interactive Brokers Controller)

**What it does:**
- Open-source Python wrapper that auto-launches IB Gateway
- Handles credential injection from config file
- Manages restart on crashes
- Reads `/opt/ibc/config.ini` for IBKR username/password

**Why selected:**
- Industry-standard for headless IBKR setups
- Free, open-source, well-maintained
- Solves the "Gateway must be manually started" problem
- Integrates with systemd cleanly

**Connections:**
- → IB Gateway (launches/restarts process)
- ← /opt/ibc/config.ini (credentials)
- ← systemd (lifecycle management)

**When to consider replacement:**
- Need different broker → IBC is IBKR-specific, switch broker = drop IBC
- Want to eliminate Java entirely → switch to IBKR Web API + REST (no Gateway, no IBC)

**Considerations:**
- Daily 2FA still required (IBC can't bypass this — no software can)
- Credentials in plaintext on EC2 disk (acceptable since EC2 is locked down with IAM + SSH)

---

### 9. Interactive Brokers Servers

**What it does:**
- Receives orders from IB Gateway
- Routes to actual stock/options exchanges
- Holds your $10,000 cash + positions
- Sends 2FA push to phone for daily login

**Why selected:**
- Lowest commissions ($0.005/share, $1 minimum)
- Best options chain access (full strikes, expiries)
- Real-time market data available ($10/month)
- Trusted broker, regulated by SEC + FINRA
- Robust API ecosystem

**Connections:**
- ← IB Gateway (TLS connection)
- → Phone (push notifications for 2FA)
- → Stock exchanges (NYSE, NASDAQ, etc.)

**When to consider replacement:**

| Reason | Alternative |
|---|---|
| Want pure API key (no 2FA) | Alpaca |
| Want better mobile UX | TD Ameritrade thinkorswim |
| Want crypto + stocks | Robinhood (limited API) |
| Higher fund minimums | Goldman Sachs Marquee (institutional) |
| International markets | Saxo Bank, IBKR Pro Global |

**Considerations:**
- IBKR is best for serious quant trading
- Switch only if a specific feature/cost is dealbreaker
- Migration cost: ~1–2 weeks of code rewrite + funds transfer

---

### 10. GitHub (Source code repository)

**What it does:**
- Stores all code (Python backend, React frontend, infra scripts)
- Provides version history
- Acts as deployment source (CodeBuild + EC2 pull from here)
- Branches: `dev` (active) and `main` (verified-live)

**Why selected:**
- Industry standard
- Free for public/private repos at our scale
- Built-in code review via PRs
- Reliable, fast pulls
- Native integration with CodeBuild

**Connections:**
- ← Jackson's Mac (push code)
- → CodeBuild (pull for builds)
- → EC2 (manual pull for updates)

**When to consider replacement:**
- Want self-hosted → Gitea, GitLab CE
- Concerned about confidentiality → AWS CodeCommit (less features, more private)
- Want better PR review tools → GitLab CI/CD

**Considerations:**
- If repo is public, anyone can read your code (potentially exposing API endpoints)
- Recommendation: keep repo private for trading code
- 2FA on GitHub account is essential

---

## Why this architecture

### Two-backend design rationale

**Why not just one backend?**

App Runner cannot run IB Gateway because:
- App Runner is stateless and can spin down/scale
- IB Gateway requires persistent socket connections
- IB Gateway needs filesystem access for IBC config
- App Runner has no SSH or shell access

EC2 alone could work, but:
- EC2 requires manual scaling/maintenance
- App Runner gives free auto-scaling for the public-facing dashboard
- Splitting concerns: App Runner = display, EC2 = real money

**The split is a feature, not a bug:**
- If App Runner is hacked, your real money is safe (different backend)
- If EC2 crashes, dashboard still works (different backend)
- Each backend can be redeployed/restarted independently

### Why SQLite instead of Postgres

- Free vs $20/month for RDS
- Single-user system (no concurrent write conflicts)
- File-based, easy to back up to S3
- Switch to Postgres if you ever need multi-user or complex queries

### Why IB Gateway instead of IBKR Web API

- IB Gateway has full feature set (every order type, every options strategy)
- Web API is newer, less mature, has fewer features
- Web API has session expiry issues (similar to Gateway, no real win)
- Switch to Web API if/when it adds: long-lived refresh tokens + full options support

---

## Scaling considerations

### Current scale (single user, $10K account)

- App Runner: 1 instance, ~$10/month
- EC2: 1 t3.small, ~$15/month
- S3: <$1/month
- ECR: <$1/month
- CodeBuild: ~$0.50/month
- Total: ~$25–30/month

### When to scale UP (more capital, more sophistication)

| Trigger | Action |
|---|---|
| $100K account | Same architecture, just bigger position limits — no changes needed |
| $1M account | Consider RDS for better data integrity, multi-region failover |
| 10+ users (sharing) | Add user auth, switch to Postgres, dedicated backend |
| Need <100ms latency | Move EC2 closer to broker (NY1 datacenter, paid colo) |
| Need 99.99% uptime | Auto Scaling Group, multi-AZ, Route53 health checks |
| Trading >100 trades/sec | Switch to ECS or EKS, Redis pub/sub, dedicated infrastructure |

### When to scale DOWN

| Trigger | Action |
|---|---|
| Just want to test paper | Shut down EC2, App Runner alone is enough (~$10/month) |
| Long break from trading | Pause EC2 (saves $15/month), keep App Runner |
| Permanent retirement | Terminate everything, archive code on GitHub |

---

## Security model

### Trust boundaries

```
┌─────────────────────────────────────────────────┐
│ TRUSTED ZONE                                     │
│  - Jackson's Mac                                 │
│  - SSH key (mirror-key)                          │
│  - ADMIN_API_KEY (saved in Notes)                │
│  - IBKR credentials (in /opt/ibc/config.ini)     │
└─────────────────────────────────────────────────┘
                       │ HTTPS / SSH
                       ▼
┌─────────────────────────────────────────────────┐
│ AWS BOUNDARY                                     │
│  - IAM authentication                            │
│  - Security groups (firewall)                    │
│  - VPC isolation                                 │
│  - At-rest encryption                            │
└─────────────────────────────────────────────────┘
                       │ public HTTPS
                       ▼
┌─────────────────────────────────────────────────┐
│ PUBLIC INTERNET                                   │
│  - App Runner dashboard (read-only public)       │
│  - Admin endpoints (require X-Admin-Key)         │
│  - WAF blocks attack patterns                    │
└─────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│ INTERACTIVE BROKERS BOUNDARY                     │
│  - 2FA required for all sessions                 │
│  - Server-side risk limits                       │
│  - Account isolation                             │
└─────────────────────────────────────────────────┘
```

### Defense layers

1. **AWS IAM** — controls who/what can access AWS resources
2. **Security groups** — firewall rules (only SSH from your IP)
3. **App Runner WAF** — blocks SQL injection, XSS, path traversal
4. **API key auth** — protects destructive endpoints
5. **IBKR 2FA** — prevents unauthorized account access
6. **EC2 binds to localhost** — backend not exposed to internet
7. **Server-side trading limits** — auto-halt on losses
8. **Audit log** — tracks all admin endpoint access

---

## Disaster recovery

### Recovery time objectives

| Failure | Recovery time | Procedure |
|---|---|---|
| App Runner container crash | 1–2 min | Auto-recovers |
| App Runner deploy failure | 5 min | Roll back to previous ECR image |
| EC2 backend crash | 15 sec | systemd auto-restarts |
| IB Gateway crash | 30–60 sec | IBC auto-restarts |
| EC2 instance crash | 5–10 min | Launch replacement, restore from S3 |
| AWS us-east-1 outage | hours–days | Wait for AWS, positions held safely |
| GitHub outage | hours | Manual deploy from local Mac copy |
| IBKR servers down | minutes–hours | Wait for IBKR, no orders fire |

### Backup strategy

- **Code:** GitHub (off-site, free, redundant)
- **Database:** S3 (every trade cycle, ~30 sec)
- **IBKR snapshot:** S3 (every 30 sec)
- **Configs:** Stored in repo (`infra/`)
- **Secrets:** Mac Notes + Apple iCloud (use a password manager for production)

### Single points of failure

| Component | Risk level | Mitigation |
|---|---|---|
| EC2 instance | Medium | Auto Scaling Group can replace |
| IB Gateway process | Low | IBC auto-restarts |
| App Runner | Low | AWS-managed, auto-recovers |
| S3 | Very Low | 11 nines durability |
| IBKR account | Low | IBKR's own redundancy |
| Your phone | Medium | Set up backup 2FA via security key |
| GitHub repo | Low | Multiple Mac clones + branches |

---

## Future considerations

### Things to evaluate in 3 months

1. **Performance monitoring:** Add Datadog/CloudWatch dashboards for latency, error rates
2. **Alerting:** SNS topics for trade failures, daily loss alerts
3. **Bracket orders:** Server-side stop losses (orders held by IBKR even if EC2 dies)
4. **A/B testing:** Run paper-only vs IBKR-mirror returns, compare alpha
5. **More brokers:** Add Alpaca as backup or for crypto

### Things to evaluate in 6–12 months

1. **App Runner deprecation:** Migrate to ECS Express Mode (announced, no firm date)
2. **Multi-region:** Replicate to us-west-2 for disaster recovery
3. **Database upgrade:** SQLite → Postgres if data grows >1GB or need queries
4. **Web API migration:** Re-evaluate if IBKR's Web API becomes more mature
5. **Compliance:** If trading other people's money, register as RIA + add audit trails

### Things you'd never want to add

- Public API key auth — defeats security model
- Auto-deploy on every push — too risky for trading systems
- Self-hosted Gateway — IBKR doesn't allow it
- Public order endpoints — anyone could trade on your account

---

## Decision log

Major architectural choices and their reasoning:

| Decision | Date | Reasoning |
|---|---|---|
| App Runner over Lambda | Initial | Persistent FastAPI process needed, Lambda has 15-min limit |
| SQLite over Postgres | Initial | Single-user, simple, cheap |
| EC2 over Fargate | April 2026 | Need persistent IB Gateway socket |
| t3.small over t3.micro | April 2026 | Gateway needs 2GB RAM |
| Two backends over one | April 2026 | Public dashboard isolation from real-money trading |
| S3 cross-pollination over direct API | April 2026 | App Runner can't reach EC2 directly |
| Manual deploys over auto | Initial | Safety: human review before live |
| `dev` + `main` branches | April 2026 | Stage code before live |
| API key over OAuth | April 2026 | Simpler for solo project |
| IB Gateway over Web API | April 2026 | More features, more mature |

---

## Glossary

- **App Runner:** AWS managed container service
- **CodeBuild:** AWS build service
- **EC2:** AWS virtual machine (Elastic Compute Cloud)
- **ECR:** AWS Docker image registry (Elastic Container Registry)
- **IAM:** AWS identity & access management
- **IB Gateway:** Java-based IBKR client for headless trading
- **IBC:** Interactive Brokers Controller (manages Gateway lifecycle)
- **IBKR:** Interactive Brokers
- **S3:** AWS object storage (Simple Storage Service)
- **SQLite:** File-based SQL database
- **Systemd:** Linux service manager
- **TWS:** Trader Workstation (full IBKR GUI client, alternative to Gateway)
- **VPC:** Virtual Private Cloud (AWS network)
- **Xvfb:** Virtual display server (X virtual framebuffer)

---

**Document version:** 1.0
**Owner:** Jackson Lee
**Last review:** April 28, 2026
**Next review:** July 2026 (or after major architecture change)
