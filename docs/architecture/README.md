# Architecture Documentation

This directory contains system architecture documentation for the Epic Fury Stock Analyzer.

## Contents

| File | Purpose |
|---|---|
| [SYSTEM_DIAGRAM.md](./SYSTEM_DIAGRAM.md) | Full ASCII system architecture diagram with all components and connections |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Detailed architecture document — component breakdown, decisions, scaling considerations |
| [IBKR_CONNECTION_PLAN.md](./IBKR_CONNECTION_PLAN.md) | Step-by-step plan to connect to Interactive Brokers (real-money mirror setup) |
| [JACKSON_DIAGRAM.md](./JACKSON_DIAGRAM.md) | Jackson's hand-drawn architecture diagram (placeholder — add image when ready) |

## Reading order

If you're new to the system:
1. Start with **SYSTEM_DIAGRAM.md** for the high-level visual
2. Then read **ARCHITECTURE.md** for the why behind each component
3. Read **IBKR_CONNECTION_PLAN.md** when you're ready to set up real-money mirroring

## When to update these docs

- Adding/removing a major component (new service, new data flow)
- Changing the security model (auth, IAM, secrets)
- Major architectural decisions (broker switch, database upgrade, multi-region)
- After any incident or near-miss (lessons learned)

## Document versions

- v1.0 — April 28, 2026 — Initial documentation
