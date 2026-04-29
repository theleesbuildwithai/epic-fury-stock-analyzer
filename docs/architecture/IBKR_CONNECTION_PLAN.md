# IBKR Live Connection Plan

**Goal:** Connect the existing trading system to a real $10,000 IBKR account so trades fire automatically with daily one-tap 2FA.

**Date prepared:** April 28, 2026
**Estimated time:** 30–45 minutes (with engineer)
**Cost:** ~$15/month (AWS EC2 t3.small)

---

## Overview

We will set up an AWS EC2 virtual machine that runs IB Gateway 24/7 and mirrors paper trades from the existing App Runner system to your real IBKR account. The public dashboard will show real account data via S3 cross-pollination.

**Daily commitment after setup:** One push notification on your phone (~10 seconds).

**Mac requirements after setup:** None. Mac can be closed permanently.

---

## Safety limits (hardcoded)

These auto-scale with your account size:

- **Max position size:** 20% of account ($2,000 on $10K)
- **Max total exposure:** 100% of account ($10,000 on $10K)
- **Daily loss limit:** 3% of account ($300 on $10K) — auto-halts trading
- **Min trade size:** $100 (fixed)
- **Max orders per day:** 50 (fixed)
- **Market hours only:** Yes (9:30 AM – 4:00 PM ET)

Plus emergency kill switch endpoint that flattens all positions in 30 seconds.

---

## Pre-flight checklist

Confirm before starting:

- [ ] AWS Console access works (logged into Chrome browser)
- [ ] IBKR Mobile app installed on phone with notifications enabled
- [ ] IBKR live account credentials available (username + password)
- [ ] $10,000 funded in IBKR account
- [ ] Latest code deployed to App Runner (commit c2ff83d or later)
- [ ] Public dashboard loads at https://txyz3yv2up.us-east-1.awsapprunner.com
- [ ] IBKR section on dashboard shows "No IBKR connection here" (expected pre-EC2)
- [ ] Mac Terminal accessible
- [ ] ~30–45 minutes of focused time

---

## Phase 1 — Generate SSH key locally (3 min)

This avoids the Safari download bug we hit Saturday.

**Open Terminal on Mac. Paste:**

```
ssh-keygen -t rsa -b 2048 -f ~/Downloads/mirror-key -N "" -C "ibkr-mirror"
```

**Verify both files created:**

```
ls -la ~/Downloads/mirror-key*
```

Should show two files (~400 bytes each):
- `mirror-key` (private key — keep on Mac)
- `mirror-key.pub` (public key — upload to AWS)

**Display the public key (we'll paste this into AWS):**

```
cat ~/Downloads/mirror-key.pub
```

Copy the output — starts with "ssh-rsa".

---

## Phase 2 — Create IAM role (5 min)

Required so EC2 can write IBKR snapshots to S3 (otherwise dashboard won't show real account).

1. Open Chrome → AWS Console → IAM
   URL: https://console.aws.amazon.com/iam/home

2. Click **Roles** in left sidebar → **Create role**

3. **Trusted entity type:** AWS service

4. **Use case:** EC2

5. Click **Next**

6. **Permissions:** search and check `AmazonS3FullAccess`

7. Click **Next**

8. **Role name:** `ec2-epic-fury-role`

9. **Description:** "EC2 instance role for Epic Fury IBKR mirror — S3 read/write"

10. Click **Create role**

Confirm role appears in the Roles list.

---

## Phase 3 — Import SSH public key to AWS (3 min)

1. AWS Console → EC2
   URL: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1

2. Left sidebar → **Key Pairs** (under Network & Security)

3. Top right → **Actions** dropdown → **Import key pair**

4. **Name:** `mirror-key`

5. **Public key contents:** paste the output from Phase 1 step "Display the public key"

6. Click **Import key pair**

Confirm key pair appears in the list.

---

## Phase 4 — Launch EC2 instance (10 min)

1. AWS Console → EC2 → **Launch instance** (orange button, top right)

2. **Name:** `epic-fury-ibkr-mirror`

3. **Application and OS Images:** Ubuntu Server 22.04 LTS (Quick Start tab)

4. **Architecture:** 64-bit (x86)

5. **Instance type:** **t3.small** (NOT t3.micro — 2GB RAM required)

6. **Key pair (login):** Use existing key pair → select `mirror-key`

7. **Network settings → Edit:**
   - Auto-assign public IP: **Enable**
   - Firewall (security groups): **Create security group**
   - Security group name: `ibkr-mirror-sg`
   - Description: "SSH-only from my IP"
   - Inbound rules: keep ONE rule (SSH, source: My IP)

8. **Configure storage:** 30 GiB gp3 (default is fine)

9. **File systems:** SKIP (don't check S3 Files / EFS / FSx — leave unchecked)

10. **Advanced details → IAM instance profile:** select `ec2-epic-fury-role` (CRITICAL — without this, no S3 cross-pollination)

11. Click **Launch instance** (orange button, bottom right)

Wait 2 minutes. Status changes from "Pending" → "Running" → "2/2 checks passed".

12. Click on the instance → copy **Public IPv4 address** (e.g., 54.123.45.67)

---

## Phase 5 — SSH into EC2 (2 min)

In Mac Terminal (replace YOUR_EC2_IP with the actual IP):

```
chmod 400 ~/Downloads/mirror-key
ssh -i ~/Downloads/mirror-key ubuntu@YOUR_EC2_IP
```

When asked "Are you sure you want to continue connecting?" type **yes** and press Enter.

You should see:
```
Welcome to Ubuntu 22.04.x LTS
ubuntu@ip-xxx-xx-xx-xxx:~$
```

---

## Phase 6 — Run installer script (10–15 min wait)

While SSHed into EC2, paste:

```
git clone https://github.com/theleesbuildwithai/epic-fury-stock-analyzer.git
cd epic-fury-stock-analyzer/infra
bash setup_vps.sh
```

The script will install (logs scroll for 10–15 min):
- OpenJDK 11 (Java for IB Gateway)
- Xvfb (virtual display for headless Gateway)
- Python 3.11 + pip
- IB Gateway (latest stable)
- IBC (auto-restart manager)
- Project Python dependencies
- Systemd services

Wait until you see "SETUP COMPLETE" message.

---

## Phase 7 — Verify S3 pipeline works (no IBKR creds yet) (3 min)

Test that EC2 can push to S3 and App Runner can read it BEFORE adding real money.

**On EC2 (still SSHed in):**

```
sudo systemctl start mirror-backend
sleep 30
curl localhost:8000/api/ibkr/snapshot
```

Should return JSON with `"available": true` (snapshot pushed even though Gateway disconnected).

**Then refresh public dashboard at https://txyz3yv2up.us-east-1.awsapprunner.com → IBKR page**

Should show:
- "Live mirror via S3 snapshot (Xs old)" cyan indicator
- Account values all $0 (Gateway not connected yet — expected)

This confirms cross-pollination works. Stop here if not adding IBKR creds tonight.

---

## Phase 8 — Add IBKR credentials (3 min)

**On EC2:**

```
sudo nano /opt/ibc/config.ini
```

Find these lines near the top and fill in:

```
IbLoginId=YOUR_IBKR_USERNAME
IbPassword=YOUR_IBKR_PASSWORD
TradingMode=live
```

**Save and exit:** Ctrl+O, Enter, Ctrl+X

---

## Phase 9 — Start IB Gateway (2 min)

**On EC2:**

```
sudo systemctl start ibkr-gateway
```

Within 30–60 seconds: **Phone buzzes with IBKR Mobile push notification**.

Notification: "Login attempt from Linux. Approve?"

**Tap "Approve" on phone (10 seconds, Face ID).**

Wait 30 more seconds for Gateway to fully connect.

---

## Phase 10 — Restart backend to pick up connection (1 min)

**On EC2:**

```
sudo systemctl restart mirror-backend
sleep 20
curl localhost:8000/api/ibkr/status
```

Should return JSON showing:
- `"connected": true`
- `"mode": "LIVE"`
- `"net_liquidation": 10000` (or whatever's actually in your account)

---

## Phase 11 — Verify cross-pollination on public dashboard (1 min)

In Chrome, refresh public dashboard:
https://txyz3yv2up.us-east-1.awsapprunner.com

Click IBKR page.

Should now show:
- Status: **Connected (LIVE)** in green
- "Live mirror via S3 snapshot (Xs old)" cyan
- Net Liquidation: ~$10,000 (real value)
- Cash, Buying Power, etc. all populated
- Empty positions table (no trades fired yet — markets closed or none triggered)

---

## Phase 12 — Post-setup verification (5 min)

Run these checks before walking away:

**On EC2:**

```
sudo systemctl status ibkr-gateway
sudo systemctl status mirror-backend
journalctl -u mirror-backend -n 50 --no-pager
journalctl -u ibkr-gateway -n 50 --no-pager
```

Confirm both services say "active (running)" in green.
Confirm logs show no critical errors.

**Run health check script:**

```
bash ~/epic-fury-stock-analyzer/infra/status.sh
```

Should print all green checks.

**Save EC2 IP somewhere safe** (Mac Notes, password manager, etc.) — needed for future SSH.

**Exit SSH:**

```
exit
```

---

## Daily operation (after setup)

| Time | Action | Duration |
|---|---|---|
| 9:00 AM ET | Phone buzzes with IBKR 2FA push | — |
| 9:00 AM ET | Tap "Approve" on IBKR Mobile | 10 sec |
| 9:30 AM ET | System fires trades automatically | — |
| 9:30–4:00 PM ET | System trades autonomously | — |
| Anytime | Check dashboard if curious | optional |

**Mac can stay closed all day.** Only requires phone for the daily 2FA tap.

---

## Emergency procedures

### Kill switch (flatten all IBKR positions immediately)

From any device with internet:

```
curl -X POST -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  https://txyz3yv2up.us-east-1.awsapprunner.com/api/ibkr/kill-switch
```

(Where YOUR_ADMIN_KEY is the API key saved in Mac Notes from earlier setup.)

All positions close at market within 30 seconds. Trading auto-halts until manual unhalt.

### Pause trading without flattening

```
curl -X POST -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  https://txyz3yv2up.us-east-1.awsapprunner.com/api/ibkr/toggle
```

Disables IBKR mirror. Existing positions stay open. Toggle again to resume.

### Resume after auto-halt (after daily loss limit hit)

```
curl -X POST -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  https://txyz3yv2up.us-east-1.awsapprunner.com/api/ibkr/unhalt
```

### Restart Gateway if connection drops

SSH into EC2:

```
sudo systemctl restart ibkr-gateway
sleep 30
sudo systemctl restart mirror-backend
```

May require 2FA tap on phone.

### Check why no trades are firing

```
curl https://txyz3yv2up.us-east-1.awsapprunner.com/api/auto-trading-status
```

Look at `current_window` field and `would_trade_now.reasons`.

---

## Credentials checklist (save these securely)

After setup, you'll have these credentials. Store in password manager:

- [ ] AWS Console login (existing)
- [ ] IBKR Live username + password
- [ ] IBKR Mobile 2FA app installed on phone
- [ ] EC2 SSH key file: `~/Downloads/mirror-key`
- [ ] EC2 Public IPv4 address
- [ ] ADMIN_API_KEY (for kill switch and admin endpoints)

---

## Rollback procedures

If something goes wrong and you need to undo:

### Disable IBKR mirror but keep paper trading

SSH to EC2:

```
sudo systemctl stop mirror-backend
sudo systemctl stop ibkr-gateway
```

Real trading stops. Paper trading on App Runner continues.

### Terminate EC2 entirely

AWS Console → EC2 → Instances → select `epic-fury-ibkr-mirror` → Instance state → Terminate.

Gateway stops, mirror stops, billing stops. Re-launch anytime by repeating Phases 4–11.

### Revert to previous code version

```
git checkout main  # or whichever branch was working
git push origin main
```

Then build + deploy from main. App Runner picks up the previous working version.

---

## Known costs

- EC2 t3.small running 24/7: ~$15/month
- S3 storage + bandwidth: <$1/month
- App Runner (existing): unchanged
- IBKR commissions: ~$0.005/share, $1 minimum per trade
- IBKR data fees: $0 for delayed, ~$10/month for real-time

**Total new infrastructure cost: ~$16/month**

---

## Troubleshooting

### "Permission denied (publickey)" when SSHing

Wrong key file or wrong username. Verify:
- File path: `~/Downloads/mirror-key` (no .pem extension)
- Permissions: `chmod 400 ~/Downloads/mirror-key`
- Username: `ubuntu` (NOT ec2-user, NOT root)

### "Connection refused" on SSH

Instance not ready yet. Wait 60 seconds and retry. Or check security group allows SSH from your IP.

### IBKR snapshot returns `available: false` on dashboard

EC2 hasn't pushed yet. Wait 60 seconds. If still false, SSH in and check:
```
journalctl -u mirror-backend -f
```

Look for "IBKR snapshot pushed" log lines. If missing, check IBKR_PUSH_SNAPSHOT env var is set.

### IBKR connection keeps dropping

Daily 2FA session may have expired. Restart Gateway:
```
sudo systemctl restart ibkr-gateway
```

Then approve push notification on phone again.

### Trades not firing during market hours

Check `/api/auto-trading-status` endpoint for `current_window` and `would_trade_now` fields. Common causes:
- Outside market hours (9:30–4:00 ET only)
- Below minimum confidence threshold
- Sector concentration limit hit
- Daily loss limit triggered (auto-halt)

### EC2 ran out of disk space

```
df -h
```

If >80% used, clean Docker/cache:
```
sudo apt clean
sudo journalctl --vacuum-size=100M
```

Or upgrade to 50GB volume in EC2 console.

---

## Contact points

- AWS Console: https://console.aws.amazon.com
- App Runner Dashboard: https://us-east-1.console.aws.amazon.com/apprunner
- EC2 Console: https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1
- IBKR Client Portal: https://www.interactivebrokers.com/sso/Login
- IBKR API Status: https://www.interactivebrokers.com/en/index.php?f=2225
- Public Dashboard: https://txyz3yv2up.us-east-1.awsapprunner.com
- GitHub Repo: https://github.com/theleesbuildwithai/epic-fury-stock-analyzer

---

## Success criteria

Setup is complete when ALL of these are true:

- [ ] EC2 instance running with IAM role attached
- [ ] SSH access from Mac works
- [ ] `mirror-backend.service` shows "active (running)"
- [ ] `ibkr-gateway.service` shows "active (running)"
- [ ] `curl localhost:8000/api/ibkr/status` shows `connected: true`
- [ ] Public dashboard shows real account value
- [ ] Real positions appear in dashboard within 15 minutes of market open
- [ ] No critical errors in logs
- [ ] Phone receives 2FA push notifications
- [ ] Kill switch endpoint tested and confirmed working

---

**Document version:** 1.0
**Last updated:** April 28, 2026
**Prepared for:** Jackson Lee with data engineer setup session
