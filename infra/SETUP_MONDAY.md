# Monday April 27 Setup — Bulletproof IBKR Mirror Launch

This guide uses **local key generation** instead of AWS browser-download to
avoid the Safari issues that wrecked the weekend attempt.

Time required: **~45 minutes** total (8:00 AM – 8:45 AM ET).
You'll be live for the 9:30 AM market open.

---

## PHASE 1 — Local SSH key generation (3 min)

Open Terminal on your Mac. Paste this single block:

```bash
ssh-keygen -t rsa -b 2048 -f ~/Downloads/mirror-key -N "" -C "ibkr-mirror"
ls -la ~/Downloads/mirror-key*
```

You should see TWO files:
- `mirror-key`         (private — stays on your Mac, never share)
- `mirror-key.pub`     (public — you'll upload to AWS next)

If only one file or none, retry the command. Don't proceed without both files.

---

## PHASE 2 — Import public key to AWS (2 min)

Open: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#KeyPairs:

1. Top right → click **Actions** → **Import key pair**
2. Name: `mirror-key` (or `mirror-key-2026` if old name still exists)
3. Click **Browse** → navigate to `~/Downloads/mirror-key.pub` → select it
4. Click **Import key pair**

You should see "Successfully imported key pair" and the new key in the list.

---

## PHASE 3 — Launch EC2 instance (5 min)

Open: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#LaunchInstances:

| Field                           | Value                                  |
|---------------------------------|----------------------------------------|
| Name                            | `ibkr-mirror-prod`                     |
| AMI (Quick Start → Ubuntu)      | Ubuntu Server 22.04 LTS (default)      |
| Architecture                    | 64-bit (x86)                           |
| **Instance type**               | **t3.small** (NOT t3.micro!)           |
| **Key pair**                    | **Use existing** → `mirror-key`        |
| Network → Auto-assign IP        | Enable                                 |
| Network → Firewall              | Create security group                  |
| Security group name             | `ibkr-mirror-sg`                       |
| Inbound rule                    | SSH from My IP (only)                  |
| Storage                         | 30 GiB gp3                             |
| File systems                    | Skip (don't check anything)            |

**REQUIRED for IBKR cross-pollination**: under **Advanced details → IAM instance profile**, attach a role with **AmazonS3FullAccess** (or scoped to `s3://epic-fury-portfolio-db/*`).
Without this:
  - VM can't sync portfolio DB to S3 (positions reset on restart)
  - VM can't push IBKR snapshots to S3 (App Runner dashboard won't show your real account)

How to create the role:
  1. Open AWS IAM Console → Roles → Create role
  2. Trusted entity: AWS service → EC2
  3. Permissions: search & attach `AmazonS3FullAccess`
  4. Name it `ec2-epic-fury-role`
  5. Back in EC2 launch, select it from the IAM instance profile dropdown

Click **Launch instance**. Wait 2 min for "Running" + "2/2 checks passed".

Copy the **Public IPv4 address** from the instance details panel.

---

## PHASE 4 — SSH in and install (10 min)

In Terminal (replace `YOUR_IP` with the actual IP):

```bash
chmod 400 ~/Downloads/mirror-key
ssh -i ~/Downloads/mirror-key ubuntu@YOUR_IP
```

Type **yes** at the host fingerprint prompt.

Once you see `ubuntu@ip-xx-xx-xx-xxx:~$`, paste:

```bash
git clone https://github.com/theleesbuildwithai/epic-fury-stock-analyzer.git
cd epic-fury-stock-analyzer/infra
bash setup_vps.sh
```

Wait until you see "SETUP COMPLETE" (~10 min).

---

## PHASE 5 — IBKR credentials + start services (3 min)

In the same SSH session:

```bash
sudo nano /opt/ibc/config.ini
```

Find these three lines, fill them in:
```
IbLoginId=YOUR_IBKR_USERNAME
IbPassword=YOUR_IBKR_PASSWORD
TradingMode=live
```

Save: `Ctrl+O`, `Enter`, `Ctrl+X`.

Start services:

```bash
sudo systemctl start ibkr-gateway
sudo systemctl start mirror-backend
```

Within 60 sec your phone gets a 2FA push from IBKR Mobile.
**Tap Approve** + check "Trust this device" if offered.

---

## PHASE 6 — Pre-flight self-test (5 min)

```bash
bash ~/epic-fury-stock-analyzer/infra/health_check.sh
```

Want to see all 8 checks pass before going further. If any FAIL:
- Re-read the failure reason
- Check `infra/EMERGENCY_PLAYBOOK.md` for the matching symptom
- Don't enable trading until everything is green

Then run the actual order-path test:

```bash
curl -X POST http://localhost:8000/api/ibkr/preflight | python3 -m json.tool
```

Want to see `"overall": "PASS"` with all 5 checks passing.

---

## PHASE 7 — Enable IBKR trading (1 min)

Until now, the mirror is connected but `IBKR_ENABLED=false`. Flip it on:

```bash
curl -X POST http://localhost:8000/api/ibkr/toggle
curl http://localhost:8000/api/ibkr/status | python3 -m json.tool
```

Look for:
- `"connected": true`
- `"enabled": true`
- `"mode": "LIVE"`
- `"trading_halted": false`

---

## PHASE 8 — Watch the first trade (until ~10 AM ET)

Markets open 9:30 AM ET. The paper trader will run its first scan within a few
minutes of open. When it opens a trade, the mirror fires in parallel.

Watch logs in real time:

```bash
sudo journalctl -u mirror-backend -f
```

(Press `Ctrl+C` to stop tailing.)

Look for log lines like:
```
IBKR MIRROR: scale=0.0820 (live_acct=$10000, paper=$122000)
IBKR ORDER LOG: {'action': 'SUBMIT_ENTRY', 'ticker': 'AAPL', 'shares': 5, ...}
IBKR ORDER LOG: {'action': 'FILL', 'ticker': 'AAPL', 'price': 178.42, ...}
```

When you see your first FILL log, **screenshot it** — that's your first
real-money mirrored trade.

---

## DONE. You can close your laptop now.

The system runs 24/7. You only need to:
- **Once per day**: tap Approve on IBKR Mobile when the 2FA push comes
- **Weekly**: glance at the dashboard to see how it's doing
- **If anything looks wrong**: see `EMERGENCY_PLAYBOOK.md`

---

## Daily morning ritual (forever)

After the first day, every morning you'll get an IBKR Mobile push at some point.
Tap Approve. That's it.

If for some reason the push doesn't arrive by 10 AM:

```bash
ssh -i ~/Downloads/mirror-key ubuntu@YOUR_IP "sudo systemctl restart ibkr-gateway"
```

Restarts Gateway → fresh push within 60 sec.

---

## Cost monitoring

EC2 t3.small running 24/7 = ~$15/mo
App Runner = ~$5/mo
S3 + bandwidth = < $1/mo
**Expected total: ~$20/mo**

Check monthly: https://console.aws.amazon.com/billing/

---

## What if Monday goes sideways?

If anything breaks before market open:
1. Hit kill switch: `curl -X POST http://localhost:8000/api/ibkr/kill-switch`
2. Disable IBKR: `curl -X POST http://localhost:8000/api/ibkr/toggle` (toggles off)
3. Take the day to investigate
4. Paper trader keeps running — no real money at risk

Markets re-open Tuesday. We have all week to fix anything.
