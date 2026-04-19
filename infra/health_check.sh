#!/usr/bin/env bash
# End-to-end health check for the IBKR mirror system.
# Runs every safety check at once and prints a one-screen status.
# Run on the EC2 VM. Exit code 0 = all healthy, 1 = warnings, 2 = critical.

set -u

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No color

BASE_URL="${HEALTH_CHECK_URL:-http://localhost:8000}"
EXIT_CODE=0

echo ""
echo "========================================================================"
echo "  IBKR MIRROR — HEALTH CHECK $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================================================"
echo ""

# ─── 1. systemd services ────────────────────────────────────────────────────
echo -e "${BLUE}[1/8] systemd services${NC}"
for svc in ibkr-gateway mirror-backend; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo -e "  ${GREEN}✓${NC} $svc: active"
    else
        echo -e "  ${RED}✗${NC} $svc: NOT RUNNING"
        EXIT_CODE=2
    fi
done

# ─── 2. backend reachable ──────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[2/8] Backend HTTP${NC}"
if curl -sf -o /dev/null --max-time 5 "$BASE_URL/api/portfolio"; then
    echo -e "  ${GREEN}✓${NC} backend responding on $BASE_URL"
else
    echo -e "  ${RED}✗${NC} backend NOT responding on $BASE_URL"
    EXIT_CODE=2
    echo ""
    echo "  Cannot continue without backend. Try: sudo systemctl restart mirror-backend"
    exit $EXIT_CODE
fi

# ─── 3. IBKR connection ────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[3/8] IBKR connection${NC}"
ibkr_status=$(curl -sf --max-time 5 "$BASE_URL/api/ibkr/status" 2>/dev/null || echo "{}")
connected=$(echo "$ibkr_status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('connected',False))" 2>/dev/null || echo "False")
mode=$(echo "$ibkr_status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('mode','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
account_value=$(echo "$ibkr_status" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('account',{}).get('net_liquidation',0))" 2>/dev/null || echo "0")

if [ "$connected" = "True" ]; then
    echo -e "  ${GREEN}✓${NC} IBKR connected ($mode mode)"
    echo -e "  ${GREEN}✓${NC} Account value: \$${account_value}"
else
    echo -e "  ${RED}✗${NC} IBKR NOT connected"
    EXIT_CODE=2
fi

# ─── 4. Market hours ───────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[4/8] Market timing${NC}"
hour_et=$(TZ='America/New_York' date +%H)
weekday=$(TZ='America/New_York' date +%u)
if [ "$weekday" -ge 6 ]; then
    echo -e "  ${YELLOW}!${NC} Weekend — markets closed"
elif [ "$hour_et" -ge 9 ] && [ "$hour_et" -lt 16 ]; then
    echo -e "  ${GREEN}✓${NC} Inside RTH (current hour ET: $hour_et)"
else
    echo -e "  ${YELLOW}!${NC} Outside RTH (current hour ET: $hour_et)"
fi

# ─── 5. Position drift ─────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[5/8] Position drift${NC}"
if [ "$connected" = "True" ]; then
    drift_status=$(curl -sf --max-time 10 "$BASE_URL/api/ibkr/drift" 2>/dev/null | \
        python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('current',{}).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
    case "$drift_status" in
        OK)
            echo -e "  ${GREEN}✓${NC} No drift detected"
            ;;
        WARNING)
            echo -e "  ${YELLOW}!${NC} Drift WARNING — investigate after market close"
            [ $EXIT_CODE -lt 1 ] && EXIT_CODE=1
            ;;
        CRITICAL)
            echo -e "  ${RED}✗${NC} Drift CRITICAL — block new trades, manual review"
            EXIT_CODE=2
            ;;
        *)
            echo -e "  ${YELLOW}!${NC} Drift status: $drift_status"
            ;;
    esac
else
    echo -e "  ${YELLOW}!${NC} Skipped (IBKR not connected)"
fi

# ─── 6. Slippage ───────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[6/8] Slippage (24h)${NC}"
slippage_data=$(curl -sf --max-time 5 "$BASE_URL/api/ibkr/slippage" 2>/dev/null || echo "{}")
fills=$(echo "$slippage_data" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('summary_24h',{}).get('fills',0))" 2>/dev/null || echo "0")
avg_slip=$(echo "$slippage_data" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('summary_24h',{}).get('avg_slippage_pct',0))" 2>/dev/null || echo "0")
alerts=$(echo "$slippage_data" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('summary_24h',{}).get('alerts',0))" 2>/dev/null || echo "0")
echo -e "  ${GREEN}✓${NC} Fills: $fills, avg slippage: ${avg_slip}%, alerts: $alerts"
if [ "$alerts" != "0" ] && [ "$alerts" != "" ]; then
    [ $EXIT_CODE -lt 1 ] && EXIT_CODE=1
fi

# ─── 7. Disk space ─────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[7/8] Disk space${NC}"
disk_pct=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
if [ "$disk_pct" -lt 80 ]; then
    echo -e "  ${GREEN}✓${NC} Disk usage: ${disk_pct}%"
elif [ "$disk_pct" -lt 90 ]; then
    echo -e "  ${YELLOW}!${NC} Disk usage: ${disk_pct}% (clean up soon)"
    [ $EXIT_CODE -lt 1 ] && EXIT_CODE=1
else
    echo -e "  ${RED}✗${NC} Disk usage: ${disk_pct}% — CRITICAL, free space NOW"
    EXIT_CODE=2
fi

# ─── 8. Memory ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}[8/8] Memory${NC}"
if command -v free >/dev/null 2>&1; then
    mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2*100}')
    if [ "$mem_pct" -lt 80 ]; then
        echo -e "  ${GREEN}✓${NC} Memory usage: ${mem_pct}%"
    elif [ "$mem_pct" -lt 95 ]; then
        echo -e "  ${YELLOW}!${NC} Memory usage: ${mem_pct}%"
        [ $EXIT_CODE -lt 1 ] && EXIT_CODE=1
    else
        echo -e "  ${RED}✗${NC} Memory usage: ${mem_pct}% — risk of OOM kill"
        EXIT_CODE=2
    fi
fi

# ─── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "========================================================================"
case $EXIT_CODE in
    0) echo -e "  ${GREEN}OVERALL: HEALTHY${NC} — system is good to trade" ;;
    1) echo -e "  ${YELLOW}OVERALL: WARNING${NC} — investigate the items marked above" ;;
    2) echo -e "  ${RED}OVERALL: CRITICAL${NC} — see EMERGENCY_PLAYBOOK.md" ;;
esac
echo "========================================================================"
echo ""

exit $EXIT_CODE
