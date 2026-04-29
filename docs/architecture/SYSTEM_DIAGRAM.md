# System Architecture Diagram

**Version:** 1.0
**Last updated:** April 28, 2026

## Full system diagram

```
═══════════════════════════════════════════════════════════════════════════════
                            EXTERNAL USER LAYER
═══════════════════════════════════════════════════════════════════════════════

    ┌────────────────────┐                         ┌────────────────────┐
    │  Jackson's Mac     │                         │  Jackson's Phone   │
    │                    │                         │                    │
    │  - Chrome browser  │                         │  - IBKR Mobile App │
    │  - Terminal        │                         │  - Daily 2FA tap   │
    │  - SSH key (1)     │                         │                    │
    └─────┬──────┬───────┘                         └──────────┬─────────┘
          │      │                                            │
          │      │ HTTPS                                      │ Push notification
          │      │                                            │
          │      └──────────┐                                 │
          │                 │                                 │
═══════════│═════════════════│═════════════════════════════════│═════════════════
                            CLOUD INFRASTRUCTURE LAYER
═══════════│═════════════════│═════════════════════════════════│═════════════════
          │                 │                                 │
          ▼                 ▼                                 │
    ┌──────────────┐   ┌──────────────────────────────┐       │
    │ AWS Console  │   │  AWS App Runner              │       │
    │              │   │  (us-east-1)                 │       │
    │ - CodeBuild  │   │                              │       │
    │ - App Runner │   │  ┌────────────────────────┐  │       │
    │ - EC2        │   │  │ React Frontend         │  │       │
    │ - IAM        │   │  │ (built static files)   │  │       │
    │ - S3         │   │  └────────────────────────┘  │       │
    └──────┬───────┘   │  ┌────────────────────────┐  │       │
           │           │  │ FastAPI Backend        │  │       │
           │ Build     │  │ - Quant engine         │  │       │
           │ trigger   │  │ - Paper trader         │  │       │
           │           │  │ - Public API           │  │       │
           ▼           │  │ - WAF, rate limit, auth│  │       │
    ┌──────────────┐   │  └──────┬─────────────────┘  │       │
    │ AWS CodeBuild│   │         │                    │       │
    │              │   └─────────┼────────────────────┘       │
    │ - Pulls main │             │                            │
    │ - Builds     │             │ S3 read (snapshot + DB)    │
    │   Docker     │             │                            │
    │ - Pushes ECR │             ▼                            │
    └──────┬───────┘   ┌─────────────────────────────────┐    │
           │           │ AWS S3                          │    │
           │ Image     │ Bucket: epic-fury-portfolio-db  │    │
           │           │                                 │    │
           ▼           │ Files:                          │    │
    ┌──────────────┐   │  - predictions.db (SQLite)      │    │
    │ AWS ECR      │   │  - ibkr_snapshot.json           │    │
    │ (Image       │   │  - cash_adjustment.flag         │    │
    │  Registry)   │   └─────────────────────────────────┘    │
    └──────────────┘             ▲                            │
                                 │ S3 write                   │
                                 │                            │
═════════════════════════════════│════════════════════════════│════════════════
                            EC2 INSTANCE LAYER (us-east-1)    │
═════════════════════════════════│════════════════════════════│════════════════
                                 │                            │
                       ┌─────────┴────────────────────────────┘
                       │ IAM role: ec2-epic-fury-role
                       │ (S3 read/write permissions)
                       │
                       ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │ EC2 Instance: epic-fury-ibkr-mirror (t3.small Ubuntu 22.04)      │
    │                                                                   │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │ Mirror Backend (FastAPI, identical code to App Runner)     │  │
    │  │                                                             │  │
    │  │  IBKR_ENABLED=true                                          │  │
    │  │  IBKR_LIVE_TRADING=true                                     │  │
    │  │  IBKR_PUSH_SNAPSHOT=true                                    │  │
    │  │  IBKR_LIVE_PORT=4001                                        │  │
    │  │                                                             │  │
    │  │  - Quant engine (same as App Runner)                        │  │
    │  │  - Paper trader (same as App Runner)                        │  │
    │  │  - IBKR mirror (sends scaled trades to Gateway)             │  │
    │  │  - Snapshot pusher thread (S3 every 30s)                    │  │
    │  │  - Local SQLite DB (predictions.db, synced to S3)           │  │
    │  └─────────────┬───────────────────────────────────────────────┘  │
    │                │ TCP localhost:4001                                │
    │                ▼                                                   │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │ IB Gateway (Java, headless via Xvfb)                        │  │
    │  │                                                             │  │
    │  │  - Wrapped by IBC (auto-restart manager)                    │  │
    │  │  - Holds 24h IBKR auth session                              │  │
    │  │  - Listens on 127.0.0.1:4001                                │  │
    │  │  - TLS connection to IBKR servers                           │  │
    │  └─────────────┬───────────────────────────────────────────────┘  │
    │                │ TLS over internet                                │
    └────────────────┼──────────────────────────────────────────────────┘
                     │
═════════════════════│══════════════════════════════════════════════════════
                            BROKER LAYER (External)
═════════════════════│══════════════════════════════════════════════════════
                     │
                     ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │ Interactive Brokers Servers                                       │
    │                                                                   │
    │  - Receives orders from IB Gateway                                │
    │  - Sends 2FA push to phone (daily)                                │
    │  - Holds your $10,000 account                                     │
    │  - Executes orders on stock/options exchanges                     │
    │  - Routes to NYSE, NASDAQ, ARCA, BATS, etc.                       │
    └──────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
                            CODE SOURCE LAYER
═══════════════════════════════════════════════════════════════════════════════

    ┌──────────────────────────────────────────────────────────────────┐
    │ GitHub: theleesbuildwithai/epic-fury-stock-analyzer              │
    │                                                                   │
    │  Branches:                                                        │
    │   - dev (active development, where Claude pushes)                 │
    │   - main (verified-stable, what App Runner builds from)           │
    │                                                                   │
    │  Pulled by:                                                       │
    │   - CodeBuild on every build (for App Runner)                     │
    │   - EC2 via manual `git pull` after merging dev → main            │
    └──────────────────────────────────────────────────────────────────┘
```

## Key relationships at a glance

| From | To | How | Frequency |
|---|---|---|---|
| Jackson's Mac | App Runner dashboard | Browser HTTPS | Anytime |
| Jackson's Mac | AWS Console | Browser + AWS login + MFA | When deploying |
| Jackson's Mac | EC2 | SSH with `mirror-key` | When admin needed |
| Jackson's Phone | IBKR servers | 2FA push notification | Once per day |
| App Runner | S3 | IAM role (read snapshot) | Every 10 sec |
| EC2 backend | S3 | IAM role (write snapshot + db) | Every 30 sec |
| EC2 backend | IB Gateway | TCP localhost:4001 | Continuous |
| IB Gateway | IBKR servers | TLS over internet | Continuous |
| GitHub `main` | App Runner | CodeBuild auto-pulls | On manual build |
| GitHub `main` | EC2 | Manual `git pull` after merge | When updating |

## Mental model — three layers, two backends

```
THREE LAYERS:
  - Display layer: Frontend (React) — just a window
  - Logic layer: Backend (FastAPI) — runs in BOTH App Runner AND EC2
  - Hardware layer: EC2 + IB Gateway — connects to the broker

TWO BACKENDS (running same code, different env vars):
  - App Runner backend → public dashboard, paper-only
  - EC2 backend → IBKR mirror, real money

THREE STORAGE:
  - SQLite on disk (each backend has its own copy)
  - S3 (shared truth between both)
  - IBKR servers (real positions)

ONE SSH KEY:
  - Mac → EC2 only

ONE DAILY ACTION:
  - Phone tap for IBKR 2FA
```

## Why each layer exists

- **Frontend** — visual representation of what the system is doing. Crash here = no impact on trading.
- **Backend (App Runner)** — public API + paper trading. Hack here = no impact on real money (separate backend).
- **Backend (EC2)** — same code, but with IBKR connectivity. This is where real money flows.
- **IB Gateway** — required by IBKR for socket-based API access.
- **IBKR servers** — the actual broker that holds your money and executes orders.
- **S3** — shared storage so both backends can sync without direct connection.
- **GitHub** — source of truth for code.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for full component-by-component analysis.
