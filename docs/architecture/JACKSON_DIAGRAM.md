# Jackson's Original Architecture Diagram

This is Jackson's hand-drawn / initial mental model of the system architecture, drawn during the early planning phase.

## Image

> **TODO:** Add the original diagram image here.
>
> To add the image:
> 1. Export the diagram as PNG/JPG (from whichever tool was used — could be a whiteboard photo, Excalidraw export, Figma export, etc.)
> 2. Save it to this directory as `jackson-diagram.png`
> 3. Replace this section with: `![Jackson's Architecture Diagram](./jackson-diagram.png)`

## Text representation of original diagram

(Reconstructed from screenshot shared during planning):

```
┌──────────────┐
│  Jackson's   │
│  Computer    │
└──┬───┬───┬───┘
   │   │   │
   │   │   │ SSH
   │   │   ▼
   │   │   ┌─────────────────┐
   │   │   │  Talk to IBKR   │
   │   │   └─────────────────┘
   │   │           ▲
   │   │           │ SSH
   │   │           │
   │   ▼           │
   │   ┌────────────────────┐
   │   │  Engine -          │
   │   │  Trading Logic     │◄───────┐
   │   │  Engine            │        │
   │   │  EC2, SSH          │        │ IAM
   │   └─┬──────┬───────────┘        │
   │     │      │                    │
   │     │      │ DB I/O             │
   │     │      ▼                    │
   │     │   ┌─────────┐             │
   │     │   │Database │             │
   │     │   └─────┬───┘             │
   │     │         │                 │
   │     │         ▼                 │
   │     │   ┌────────────────┐      │
   │     └──►│ Front End      │──────┘
   │         │ AppRunner      │
   ▼         └────────────────┘
   (HTTPS)

Note (right margin):
IAM
- Who/what the things is
- What they are allowed to do
```

## What this diagram got right

- ✅ Jackson's computer connects to multiple things
- ✅ Database is shared between Engine and Front End
- ✅ Engine talks to IBKR via SSH
- ✅ IAM controls who/what can do what
- ✅ Engine is the trading logic core

## Corrections made (see SYSTEM_DIAGRAM.md for the corrected version)

1. **App Runner has BOTH frontend and backend** (not separate as drawn). The "Front End AppRunner" box should encompass a FastAPI backend too.

2. **IB Gateway lives INSIDE the EC2 instance** (not external). "Talk to IBKR" is the IB Gateway process, and only the EC2 backend (on the same machine) can connect to it via localhost.

3. **App Runner ↔ EC2 communicate via S3, not IAM directly.** IAM is just permissions that allow the S3 access — the actual data flow is through S3 snapshot files.

4. **Missing components:**
   - Phone (for daily IBKR 2FA)
   - GitHub (code source)
   - CodeBuild (build pipeline)
   - ECR (Docker image registry)
   - IBKR servers (the actual broker, separate from "Talk to IBKR")
   - S3 bucket (the cross-pollination layer)

## See also

- [SYSTEM_DIAGRAM.md](./SYSTEM_DIAGRAM.md) — Corrected and expanded ASCII diagram
- [ARCHITECTURE.md](./ARCHITECTURE.md) — Component breakdown with rationale
