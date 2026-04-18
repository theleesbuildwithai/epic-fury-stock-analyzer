#!/bin/bash
# Quick status check. Run on the VM anytime.
# Usage: bash status.sh

set -e

echo ""
echo "═══ Epic Fury IBKR Mirror — Status ═══"
echo ""

echo "Services:"
for svc in ibkr-gateway mirror-backend; do
    state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
    if [[ "$state" == "active" ]]; then
        echo "  ✓ $svc: $state"
    else
        echo "  ✗ $svc: $state"
    fi
done

echo ""
echo "Connection:"
if response=$(curl -s --max-time 5 http://localhost:8000/api/ibkr/status 2>/dev/null); then
    echo "$response" | jq '{
        connected: .status.connected,
        mode: .status.mode,
        trading_halted: .status.trading_halted,
        account_value: .status.mirror.live_account_value,
        cash: .account.cash,
        positions_value: .account.gross_position_value,
        daily_pnl: .account.daily_pnl,
        last_error: .status.last_error
    }' 2>/dev/null || echo "$response"
else
    echo "  Backend not responding on localhost:8000"
fi

echo ""
echo "Recent mirror activity (last 20 lines):"
sudo journalctl -u mirror-backend -n 20 --no-pager 2>/dev/null | tail -20 || echo "  (no logs yet)"

echo ""
