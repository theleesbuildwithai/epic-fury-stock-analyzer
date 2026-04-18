# Epic Fury IBKR Mirror — Cloud Setup Guide

Run the IBKR mirror system on AWS EC2 24/7 so your MacBook doesn't have to stay on.

**What this sets up:**
- AWS EC2 VM (t3.small, ~$15/mo) running Ubuntu 22.04
- IB Gateway (headless, via IBC auto-management)
- The mirror backend as a systemd service
- Daily auto-restart of Gateway through IBKR's midnight reset

**What you still do daily:**
- Tap "Approve" on the IBKR Mobile push notification that comes once per day (~10 sec)
- Nothing else

---

## 1. Cost breakdown

| Service | Monthly cost |
|---|---|
| AWS EC2 t3.small (2 vCPU, 2GB RAM, 24/7) | ~$15 |
| 30 GB gp3 EBS storage | ~$2.50 |
| Data transfer (minimal) | ~$0.50 |
| **Total** | **~$18/month** |

Paid via your existing AWS billing account (same one you use for App Runner + CodeBuild). Shows up on your monthly AWS bill — no separate payment to set up.

---

## 2. Create the EC2 instance (one-time, ~10 min)

### 2a. Log into AWS console
Go to https://console.aws.amazon.com/ec2/

### 2b. Launch Instance
- **Name**: `epic-fury-ibkr-mirror`
- **AMI**: `Ubuntu Server 22.04 LTS` (64-bit x86)
- **Instance type**: `t3.small` (2 vCPU, 2 GB RAM)
- **Key pair**: Create a new one (e.g., `ibkr-mirror-key`). **Download the `.pem` file** — you need it to SSH in.
- **Network**:
  - VPC: default
  - Auto-assign public IP: **Enable**
- **Security group**: Create new, allow:
  - **SSH (port 22)** from **Your IP** (AWS auto-detects). This is the ONLY port exposed.
  - Do NOT open port 8000 or 4001 to the internet — we tunnel via SSH.
- **Storage**: 30 GB gp3 root volume
- Click **Launch instance**

### 2c. Note the public IP
Wait 2 min for the instance to show "Running" state. Copy the **Public IPv4 address** (e.g., `54.123.45.67`).

### 2d. Prepare the SSH key locally
```bash
# One-time setup after you download the .pem file
chmod 400 ~/Downloads/ibkr-mirror-key.pem
```

### 2e. SSH in
```bash
ssh -i ~/Downloads/ibkr-mirror-key.pem ubuntu@<EC2-IP>
```

You should see the Ubuntu welcome message. You're in.

---

## 3. Install everything (one-time, ~10 min)

From inside the SSH session:

```bash
# Clone the repo
git clone https://github.com/theleesbuildwithai/epic-fury-stock-analyzer.git
cd epic-fury-stock-analyzer/infra

# Run setup (installs Java, Gateway, IBC, Python deps, systemd services)
bash setup_vps.sh
```

Watch for errors. Takes ~10 min (Gateway download is the slow part).

When finished, you'll see a box saying **"SETUP COMPLETE"**.

---

## 4. Configure IBKR credentials (one-time, 30 sec)

```bash
nano /opt/ibc/config.ini
```

Find these three lines and fill in your IBKR username and password:

```ini
IbLoginId=YOUR_IBKR_USERNAME
IbPassword=YOUR_IBKR_PASSWORD
TradingMode=live
```

Save: `Ctrl+O`, Enter, `Ctrl+X`.

The file is already chmod 600 (only readable by you), but still — **treat this like a password file**.

---

## 5. Start the services

```bash
sudo systemctl start ibkr-gateway
sudo systemctl start mirror-backend
```

Within 30-60 seconds, **your phone will buzz** with an IBKR Mobile 2FA request:

> *"Login attempt from Linux at <IP>. Approve?"*

**Tap Approve.** Gateway logs in and stays connected.

---

## 6. Verify everything is running

```bash
# Should say "active (running)"
sudo systemctl status ibkr-gateway
sudo systemctl status mirror-backend

# Should return JSON with "connected":true and your live_account_value
curl http://localhost:8000/api/ibkr/status | jq
```

If `connected: true` → you're done. System is trading for you 24/7.

---

## 7. Daily routine

Every ~24 hours (IBKR requires daily re-auth):

1. You get **one push notification** on IBKR Mobile
2. Tap **Approve**
3. That's it — system keeps trading

If you miss it (travel, phone off), Gateway will disconnect and the next scan fails until you approve next time. **Paper trader on App Runner keeps running regardless** so no trading logic is lost.

---

## 8. Checking in on the system

### See live logs
```bash
# Mirror backend logs
sudo journalctl -u mirror-backend -f

# Gateway logs
sudo journalctl -u ibkr-gateway -f
```

### Check status via API (from the VM)
```bash
curl http://localhost:8000/api/ibkr/status | jq
```

### Access the dashboard from your Mac (SSH tunnel)
On your Mac:
```bash
ssh -i ~/Downloads/ibkr-mirror-key.pem -L 8000:localhost:8000 ubuntu@<EC2-IP>
```
Then open **http://localhost:8000** in your browser on the Mac — you'll see the dashboard.

---

## 9. Safety levers

### Emergency kill switch (flatten all IBKR positions)
```bash
curl -X POST http://localhost:8000/api/ibkr/kill-switch
```

### Pause IBKR mirror (paper keeps running)
```bash
sudo systemctl stop mirror-backend
```

### Pause Gateway (disconnects API)
```bash
sudo systemctl stop ibkr-gateway
```

### Resume
```bash
sudo systemctl start ibkr-gateway mirror-backend
```

### Full shutdown (stop VM to stop AWS billing temporarily)
In AWS console: EC2 → select instance → Instance state → **Stop**.
Restart with **Start** later. Paper trader on App Runner is unaffected.

---

## 10. Updating the code after I push new commits

```bash
# SSH into the VM
ssh -i ~/Downloads/ibkr-mirror-key.pem ubuntu@<EC2-IP>

# Pull and restart
cd epic-fury-stock-analyzer
git pull
sudo systemctl restart mirror-backend

# Verify
curl http://localhost:8000/api/ibkr/status | jq
```

IBKR Gateway doesn't need restarting for code changes — only `mirror-backend`.

---

## 11. Troubleshooting

### "connected": false after starting services

```bash
# Check Gateway is running
sudo systemctl status ibkr-gateway

# Check recent Gateway logs
sudo journalctl -u ibkr-gateway -n 100 --no-pager

# Most common cause: you haven't approved the 2FA push yet.
# Look at your phone's IBKR Mobile app notifications.
```

### "2FA prompt never came to my phone"

```bash
# Ensure IBKR Mobile is logged in with the SAME account as the one in config.ini
# Restart the gateway service:
sudo systemctl restart ibkr-gateway
# Wait 60 seconds, phone should buzz.
```

### "I don't see the dashboard at localhost:8000 from my Mac"

You need the SSH tunnel running:
```bash
ssh -i ~/Downloads/ibkr-mirror-key.pem -L 8000:localhost:8000 ubuntu@<EC2-IP>
```
Keep this SSH window open while the browser is open. Close it to disconnect.

### "Port 4001 connection refused in backend logs"

Gateway isn't up yet. Wait 60 sec after `systemctl start ibkr-gateway` or check `journalctl -u ibkr-gateway`.

---

## 12. One-line status check (add to your bashrc for easy monitoring)

After you SSH in, any time:
```bash
curl -s http://localhost:8000/api/ibkr/status | jq '{connected:.status.connected, mode:.status.mode, balance:.status.mirror.live_account_value, last_error:.status.last_error}'
```

Gives you a clean 4-line summary.

---

## Summary: Your entire ongoing commitment

- **Daily**: Tap "Approve" on one IBKR Mobile push (~10 sec)
- **Monthly**: AWS bill hits your card (~$18, automatic)
- **When I push code**: SSH in, `git pull`, `sudo systemctl restart mirror-backend` (30 sec)

That's it. System runs 24/7, mirrors every paper trade to your real IBKR account, scales to whatever balance you have.
