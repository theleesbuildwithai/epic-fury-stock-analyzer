# Emergency Playbook — IBKR Mirror System

If something goes wrong while real money is in flight, **find the symptom below
and execute the response immediately**. Don't hesitate, don't experiment — act.

> Replace `YOUR_EC2_IP` with your VM's IP throughout. Save this command on your
> phone for fastest access:
>
> ```
> ssh -i ~/Downloads/mirror-key ubuntu@YOUR_EC2_IP
> ```

---

## NUCLEAR — Stop everything immediately

If you see anything that scares you and you want everything to STOP RIGHT NOW:

```bash
# From your laptop (or phone Termius app)
ssh -i ~/Downloads/mirror-key ubuntu@YOUR_EC2_IP "curl -X POST http://localhost:8000/api/ibkr/kill-switch"
```

What this does:
- Cancels all open IBKR orders
- Submits market orders to flatten every IBKR position
- Sets `TRADING_HALTED=true` so no new orders fire
- Paper trader on App Runner keeps running (unaffected)

After this fires, your IBKR account is FLAT (cash only). Investigate the issue,
then `POST /api/ibkr/unhalt` to resume when ready.

---

## SYMPTOM TABLE

### 1. Paper says we hold 5 AAPL but IBKR says 3 (position drift)

**Symptom**: dashboard shows drift warning, or `/api/ibkr/drift` returns
`status: WARNING` or `CRITICAL`.

**Response**:
1. Check drift detail:
   ```bash
   ssh ... "curl http://localhost:8000/api/ibkr/drift | python3 -m json.tool"
   ```
2. If CRITICAL (high severity items): hit kill switch, manually reconcile in
   IBKR Client Portal, then resume
3. If WARNING (medium severity): let it ride one more cycle — partial fills
   often self-resolve

### 2. IBKR fills are way off paper fills (slippage > 1%)

**Symptom**: `/api/ibkr/slippage` shows `avg_slippage_pct < -1.0`

**Response**:
1. Check the worst fills:
   ```bash
   ssh ... "curl http://localhost:8000/api/ibkr/slippage"
   ```
2. If slippage is consistently < -2%, **disable IBKR**:
   ```bash
   ssh ... "curl -X POST http://localhost:8000/api/ibkr/toggle"
   ```
3. Investigate causes: low liquidity tickers? trading outside RTH? broken
   market data?

### 3. IB Gateway disconnected

**Symptom**: `/api/ibkr/status` shows `connected: false`, dashboard shows red

**Response**:
1. SSH into VM and check Gateway:
   ```bash
   ssh ... "sudo systemctl status ibkr-gateway"
   ```
2. If failed, restart:
   ```bash
   ssh ... "sudo systemctl restart ibkr-gateway"
   ```
3. Watch your phone — IBKR Mobile may push for re-2FA. Tap Approve.
4. Wait 60 sec, then verify:
   ```bash
   ssh ... "curl http://localhost:8000/api/ibkr/status"
   ```

### 4. Daily loss limit hit (account down 3% in a day)

**Symptom**: Logs show `DAILY LOSS LIMIT HIT`, no new trades firing

**Response**:
- This is BY DESIGN — the system protected you
- Do NOT immediately unhalt. Take the loss, review what happened.
- End-of-day review:
   ```bash
   ssh ... "curl http://localhost:8000/api/ibkr/reconcile | python3 -m json.tool"
   ```
- To resume next morning (only if you've reviewed):
   ```bash
   ssh ... "curl -X POST http://localhost:8000/api/ibkr/unhalt"
   ```

### 5. App Runner paper trader is down

**Symptom**: paper trades stop generating, IBKR mirror has nothing to mirror

**Response**:
1. Check App Runner status:
   - Visit https://us-east-1.console.aws.amazon.com/apprunner/home
2. If service is failing health checks, check CloudWatch logs
3. IBKR mirror keeps existing positions safe — no new trades fire, but stops
   and targets still trigger via the `_exit_checker` job that runs on the VM

### 6. EC2 VM ran out of disk

**Symptom**: SSH still works but services crash with "no space left on device"

**Response**:
1. Free up logs:
   ```bash
   ssh ... "sudo journalctl --vacuum-time=2d && sudo apt-get clean"
   ```
2. Check disk:
   ```bash
   ssh ... "df -h /"
   ```
3. If still tight, resize EBS volume in AWS console (live, no downtime).

### 7. EC2 VM completely unreachable (SSH fails)

**Symptom**: SSH times out or refuses connection

**Response**:
1. Check instance status in AWS console — is it Running?
2. If Stopped, Start it from console
3. If unreachable but Running:
   - From AWS console: Instance state → Reboot
   - Wait 2 min for full restart
   - SSH again
4. If still broken: hit kill switch via App Runner side (can't, they're separate
   systems) — call IBKR support (1-877-442-2757) and ask them to flatten your
   account manually

### 8. IBKR Mobile push not arriving for daily 2FA

**Symptom**: Gateway eventually drops, mirror stops, no trades fire

**Response**:
1. Open IBKR Mobile app → tap "Authenticate" manually
2. SSH into VM and restart Gateway:
   ```bash
   ssh ... "sudo systemctl restart ibkr-gateway"
   ```
3. Within 60 sec a fresh push arrives — approve it
4. Verify:
   ```bash
   ssh ... "curl http://localhost:8000/api/ibkr/status"
   ```

### 9. Pre-flight self-test failing

**Symptom**: `/api/ibkr/preflight` returns `overall: FAIL`

**Response**:
1. Look at which check failed:
   ```bash
   ssh ... "curl -X POST http://localhost:8000/api/ibkr/preflight | python3 -m json.tool"
   ```
2. Common causes:
   - `connection: false` → restart Gateway (see #3)
   - `market_data: false` → wait for market hours; data subscriptions may need IBKR funding
   - `order_path: false` → check if account has tradable funds, regulatory restrictions
3. **DO NOT enable trading** until pre-flight passes

### 10. Costs spiking on AWS bill

**Symptom**: Monthly AWS cost > $30

**Response**:
1. Check Billing Dashboard: https://console.aws.amazon.com/billing/
2. Common causes:
   - Forgot to terminate old test instances → terminate them
   - Excessive S3 storage → check bucket sizes
   - Snapshot accumulation → delete old snapshots
3. EC2 t3.small running 24/7 should cost ~$15/mo, App Runner ~$5/mo, S3 < $1/mo

---

## QUICK DIAGNOSTIC COMMAND

When something feels off and you don't know what:

```bash
ssh -i ~/Downloads/mirror-key ubuntu@YOUR_EC2_IP "bash ~/epic-fury-stock-analyzer/infra/health_check.sh"
```

This runs every safety check at once and prints a one-screen status.

---

## IBKR SUPPORT NUMBER

If you're locked out of your IBKR account or need a manual flatten:

**1-877-442-2757** (US toll-free, 24/7)

Have ready:
- Account number (in IBKR Mobile under Settings)
- Last 4 of SSN
- Birthday

Tell them: "I need you to cancel all open orders and flatten all positions in
account X immediately. I'm authorizing it now."
