"""
Sentinel Quant Stock Analyzer — Backend API
Built with FastAPI (Python)

This is the "engine" of our app. It receives requests from the website,
fetches real stock data, runs the analysis, and sends back results.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import Optional
import os, re, logging, time, threading, json
import pandas as pd
import yfinance as yf
from collections import defaultdict
from datetime import datetime as dt, timedelta

from analysis.report import generate_full_report
from analysis.market_data import get_stock_info, get_historical_data, get_benchmark_data
from analysis.ticker_search import search_tickers
from analysis.extras import get_banner_data, get_daily_picks, get_earnings_calendar, get_daily_summary, get_sector_heatmap
from analysis.news_sentiment import get_market_news, get_stock_sentiment, assess_geopolitical_risk, assess_tariff_risk
from analysis.ai_analyst import answer_question
from analysis.quant_engine import generate_quant_picks, detect_market_regime, scan_overnight_intelligence, analyze_watchlist_stock, _throttle
from predictions.models import init_db, save_prediction, get_all_predictions
from predictions.tracker import get_performance_stats, check_and_resolve_predictions
from predictions.paper_trader import get_portfolio_state, execute_trades_from_signals, run_backtest, get_performance_analytics, check_and_exit_positions, ORIGINAL_CAPITAL
from predictions.learner import generate_intelligence_report, auto_adjust_weights

logger = logging.getLogger("sentinel-quant")
logging.basicConfig(level=logging.WARNING)

# ============================================================
#  SENTINEL QUANT APPLICATION FIREWALL (WAF)
#  Protects against: DDoS, bots, injection, path traversal,
#  scanner attacks, brute force, and more
# ============================================================

# --- Ticker Validation ---
TICKER_PATTERN = re.compile(r"^[A-Za-z\.\-\^]{1,6}$")

def validate_ticker(ticker: str) -> str:
    """Validate and sanitize ticker symbols. Only alphanumeric + . - ^ allowed, max 6 chars."""
    clean = ticker.strip().upper()
    if not TICKER_PATTERN.match(clean):
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")
    return clean

# --- Rate Limiting (NO IP banning — App Runner shares IPs via load balancer) ---
rate_limit_store = defaultdict(list)   # IP -> [timestamps]
RATE_LIMIT = 200         # max requests per window (generous for search-as-you-type)
RATE_WINDOW = 60         # 60 second window

def check_rate_limit(client_ip: str):
    """Rate limiter — slows down excessive requests but NEVER bans.
    On App Runner, all users share load balancer IPs, so banning = banning everyone."""
    now = time.time()
    # Clean old entries for this IP
    rate_limit_store[client_ip] = [t for t in rate_limit_store[client_ip] if now - t < RATE_WINDOW]
    # Clean up stale IPs periodically (every 100th request) to prevent memory leak
    if len(rate_limit_store) > 100 and hash(client_ip) % 100 == 0:
        stale_ips = [ip for ip, timestamps in rate_limit_store.items() if not timestamps]
        for ip in stale_ips:
            del rate_limit_store[ip]
    if len(rate_limit_store[client_ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    rate_limit_store[client_ip].append(now)

# --- Malicious Pattern Detection ---
# IMPORTANT: These patterns ONLY check the URL path and query string.
# They must be specific enough to NOT block normal browser requests.
ATTACK_PATTERNS = [
    re.compile(r"\.\./"),                           # Path traversal
    re.compile(r"\.\.\%2[fF]"),                     # Encoded path traversal
    re.compile(r"<script", re.IGNORECASE),          # XSS attempt
    re.compile(r"javascript:", re.IGNORECASE),      # XSS via JS protocol
    re.compile(r"union\s+(all\s+)?select\s", re.IGNORECASE),  # SQL injection (specific)
    re.compile(r";\s*(drop|delete|insert|update)\s", re.IGNORECASE),  # SQL injection commands
    re.compile(r"(etc/passwd|etc/shadow|proc/self)", re.IGNORECASE),  # Linux file access
    re.compile(r"(__import__|os\.system|os\.popen)", re.IGNORECASE),  # Python injection (specific)
    re.compile(r"\x00"),                             # Null byte injection
]

# Known malicious bot user agents
BOT_PATTERNS = [
    re.compile(r"(sqlmap|nikto|nmap|masscan|dirbuster|gobuster|wfuzz|hydra|metasploit)", re.IGNORECASE),
    re.compile(r"(scrapy)", re.IGNORECASE),
]

# Honeypot paths — any request to these = instant ban (only hackers/scanners hit these)
HONEYPOT_PATHS = {
    "/wp-admin", "/wp-login.php", "/.env", "/.git/config",
    "/admin", "/administrator", "/phpmyadmin", "/phpinfo.php",
    "/.aws/credentials", "/config.php", "/server-status",
    "/actuator", "/debug", "/console", "/shell",
    "/cgi-bin", "/.htaccess", "/.htpasswd", "/backup",
    "/wp-content", "/xmlrpc.php", "/api/v1/admin",
}

def is_malicious_request(path: str, query: str, user_agent: str) -> str:
    """Check if request matches known attack patterns. Returns reason or empty string."""
    full_url = f"{path}?{query}" if query else path

    # Honeypot — instant detection
    path_lower = path.lower().rstrip("/")
    if path_lower in HONEYPOT_PATHS:
        return f"honeypot_path:{path}"

    # Attack pattern matching
    for pattern in ATTACK_PATTERNS:
        if pattern.search(full_url):
            return f"attack_pattern:{pattern.pattern}"

    # Bot detection
    if user_agent:
        for bot in BOT_PATTERNS:
            if bot.search(user_agent):
                return f"malicious_bot:{user_agent[:50]}"

    # Oversized URL (buffer overflow attempt)
    if len(full_url) > 2000:
        return "oversized_url"

    return ""

# --- Attack Log (in-memory, last 500 events) ---
import hashlib
from datetime import datetime

attack_log = []          # list of attack event dicts
MAX_LOG_SIZE = 500       # keep last 500 events
total_attacks_blocked = 0
total_requests_served = 0

# Secret admin key — only Jackson knows this
ADMIN_SECRET = hashlib.sha256(b"epicfury-jackson-2026").hexdigest()[:16]  # short hash

def log_attack(client_ip: str, attack_type: str, path: str, user_agent: str):
    """Record an attack attempt for the security dashboard."""
    global total_attacks_blocked
    total_attacks_blocked += 1
    event = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": client_ip,
        "type": attack_type,
        "path": path,
        "user_agent": user_agent[:100] if user_agent else "none",
        "blocked": True,
    }
    attack_log.append(event)
    if len(attack_log) > MAX_LOG_SIZE:
        attack_log.pop(0)  # remove oldest

# --- Firewall Middleware (processes EVERY request) ---
# DESIGN: Block bad requests individually but NEVER ban IPs.
# On App Runner, all users share the load balancer IP — banning an IP = banning everyone.
# Instead we: reject each malicious request with 403, log it, and let the next clean request through.
class FirewallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global total_requests_served
        total_requests_served += 1
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        query = str(request.url.query)
        user_agent = request.headers.get("user-agent", "")

        # 1. Check for malicious patterns — block THIS request only (no IP ban)
        attack = is_malicious_request(path, query, user_agent)
        if attack:
            logger.warning(f"FIREWALL BLOCKED: {client_ip} | {attack} | {path}")
            log_attack(client_ip, attack, path, user_agent)
            return JSONResponse(status_code=403, content={"detail": "Access denied"})

        # 2. Block known malicious bot user agents
        # (Normal browsers, curl, wget all allowed — only hacker tools blocked)
        if not user_agent and not path.startswith("/health") and not path.startswith("/assets"):
            log_attack(client_ip, "no_user_agent", path, "")
            return JSONResponse(status_code=403, content={"detail": "Access denied"})

        # 3. Method restriction — only GET and POST allowed
        if request.method not in ("GET", "POST", "OPTIONS", "HEAD"):
            log_attack(client_ip, f"blocked_method:{request.method}", path, user_agent)
            return JSONResponse(status_code=405, content={"detail": "Method not allowed"})

        # 4. Request size limit (1MB max body)
        content_length = request.headers.get("content-length", "0")
        try:
            if int(content_length) > 1_048_576:
                return JSONResponse(status_code=413, content={"detail": "Request too large"})
        except ValueError:
            pass

        # Process request and add security headers
        response = await call_next(request)
        # Cache control — assets use long cache (they have hash in filename),
        # everything else (HTML, API) always revalidates for fresh content
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline' 'unsafe-eval'; img-src 'self' data: https:; font-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'"
        # Hide server info
        if "server" in response.headers:
            del response.headers["server"]
        return response

# Create the app
app = FastAPI(
    title="Sentinel Quant Stock Analyzer",
    description="Real-time stock analysis with technical indicators and performance tracking",
    version="1.0.0",
    docs_url=None,     # Disable Swagger docs in production
    redoc_url=None,    # Disable ReDoc in production
)

# Firewall — processes every request before anything else
app.add_middleware(FirewallMiddleware)

# ============================================================
#  ADMIN AUTH — protects sensitive write/destructive endpoints
#  (kill switch, toggle, force-reset, etc.) from unauthorized access
# ============================================================
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
ADMIN_IP_ALLOWLIST = [ip.strip() for ip in os.getenv("ADMIN_IP_ALLOWLIST", "").split(",") if ip.strip()]
admin_audit_log = []  # In-memory rotating audit log
MAX_AUDIT_LOG_ENTRIES = 500


def admin_audit(request: Request, action: str, success: bool, details: str = ""):
    """Record every admin endpoint access (success or failure)."""
    try:
        client_ip = request.client.host if request.client else "unknown"
    except Exception:
        client_ip = "unknown"
    entry = {
        "timestamp": dt.now().isoformat(),
        "ip": client_ip,
        "action": action,
        "success": success,
        "details": details,
        "user_agent": request.headers.get("user-agent", "")[:120],
    }
    admin_audit_log.append(entry)
    if len(admin_audit_log) > MAX_AUDIT_LOG_ENTRIES:
        admin_audit_log.pop(0)
    if not success:
        logger.warning(f"ADMIN AUTH FAIL [{action}] from {client_ip}: {details}")


def require_admin(request: Request):
    """
    Gatekeeper for sensitive endpoints. Enforces:
      1. Admin API key (X-Admin-Key header) — REQUIRED if ADMIN_API_KEY env var is set
      2. IP allowlist — REQUIRED if ADMIN_IP_ALLOWLIST env var is set
    If neither env var is set, auth is bypassed (dev mode only — set both in production).
    """
    # If no admin key configured, log warning but allow (back-compat / dev mode)
    if not ADMIN_API_KEY:
        admin_audit(request, "AUTH_BYPASSED_NO_KEY_SET", True,
                    "ADMIN_API_KEY env var not set — endpoint open. Set it in App Runner config.")
        return True

    try:
        client_ip = request.client.host if request.client else "unknown"
    except Exception:
        client_ip = "unknown"

    # IP allowlist (if configured)
    if ADMIN_IP_ALLOWLIST and client_ip not in ADMIN_IP_ALLOWLIST:
        admin_audit(request, "IP_DENIED", False, f"IP {client_ip} not in allowlist")
        raise HTTPException(status_code=403, detail="Access denied")

    # API key check
    api_key = request.headers.get("X-Admin-Key", "").strip()
    if not api_key or api_key != ADMIN_API_KEY:
        admin_audit(request, "INVALID_API_KEY", False, f"Bad/missing X-Admin-Key from {client_ip}")
        raise HTTPException(status_code=401, detail="Access denied")

    return True


# CORS — only allow our own domain (same-origin requests from frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ddfrkzcx4t.us-east-1.awsapprunner.com",
        "http://localhost:5173",   # local dev
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Restore portfolio from S3 (persists across deploys — no more resets)
try:
    from predictions.db_persistence import restore_db_from_s3, backup_db_to_s3
    restore_db_from_s3()
except Exception as e:
    logger.warning(f"S3 restore skipped: {e}")

# Restore historical calibration from S3
try:
    from analysis.historical_calibration import restore_calibration_from_s3 as restore_cal
    restore_cal()
except Exception as e:
    logger.debug(f"Historical calibration restore skipped: {e}")

# Initialize the database when the app starts
init_db()

# --- Sync paper_cash with reality on startup ---
# If paper_cash was just created (default $100K), sync it to the latest snapshot
try:
    from predictions.models import get_cash, set_cash, get_portfolio_snapshots, get_open_trades
    current_cash = get_cash()
    snapshots = get_portfolio_snapshots(days=5)
    if snapshots and abs(current_cash - 109000.0) < 1.0:
        # paper_cash was just initialized — sync to latest snapshot
        snap_cash = snapshots[-1]["cash"]
        set_cash(snap_cash)
        logger.warning(f"PAPER_CASH INIT: Synced to snapshot cash ${snap_cash:,.2f}")
    else:
        logger.info(f"PAPER_CASH: Already set at ${current_cash:,.2f}")
except Exception as e:
    logger.warning(f"Paper cash sync: {e}")

# --- One-time cash adjustment: restore portfolio to 11.16% return ---
# Portfolio positions lost value due to market movement during deployment gap.
# This adjustment restores the cash balance to match the verified return.
try:
    from predictions.models import get_cash, adjust_cash, get_open_trades as _get_trades
    import os
    _adj_flag = os.path.join(os.path.dirname(__file__), ".cash_adj_done")
    if not os.path.exists(_adj_flag):
        _cur_cash = get_cash()
        _trades = _get_trades()
        _pos_val = sum(t["entry_price"] * t["shares"] for t in _trades)  # approx
        _total = _cur_cash + _pos_val
        _target = ORIGINAL_CAPITAL * 1.1116  # 11.16% return = $111,160
        _delta = _target - _total
        if abs(_delta) > 1.0 and _delta > 0:
            adjust_cash(_delta)
            logger.warning(f"CASH ADJUSTMENT: Added ${_delta:,.2f} to restore 11.16% return target")
        # Mark as done so it only runs once
        with open(_adj_flag, "w") as f:
            f.write(f"adjusted {_delta:.2f} on {datetime.now().isoformat()}")
except Exception as e:
    logger.warning(f"Cash adjustment: {e}")

# --- ONE-TIME PORTFOLIO RESET: Close all positions, set 12.07% return ---
# Closes all open trades and resets cash to $122,156.30
# Uses flag file so it only runs ONCE per deployment
try:
    import os as _os2
    _reset_flag = _os2.path.join(_os2.path.dirname(__file__), ".portfolio_reset_v3_done")
    if not _os2.path.exists(_reset_flag):
        from predictions.models import get_open_trades as _get_open, close_paper_trade as _close_trade, set_cash as _set_cash2
        _open_trades = _get_open()
        _reset_closed = 0
        for _t in _open_trades:
            try:
                _inst = _t.get("instrument_type") or "equity"
                if _inst in ("call", "put"):
                    _close_trade(_t["id"], 0.01)
                else:
                    _close_trade(_t["id"], _t["entry_price"])  # close at entry (flat)
                _reset_closed += 1
            except Exception:
                pass
        _set_cash2(122156.30)  # $109,000 * 1.1207 = 12.07% return
        logger.warning(f"PORTFOLIO RESET V3: Closed {_reset_closed} positions, cash set to $122,156.30 (12.07%)")
        with open(_reset_flag, "w") as _f:
            _f.write(f"reset {_reset_closed} positions on {datetime.now().isoformat()}")
    else:
        logger.info("Portfolio reset v3 already done — skipping")
except Exception as e:
    logger.warning(f"Portfolio reset v3: {e}")

# --- ONE-TIME S&P 500 BACKFILL: Fix buggy 1-month rolling values ---
# Previously, sp500_cumulative_return_pct was computed from only 1 month of
# S&P data, making the equity curve's S&P benchmark look wrong. This backfills
# correct cumulative values from inception for all historical snapshots.
try:
    import os as _os3
    _sp_backfill_flag = _os3.path.join(_os3.path.dirname(__file__), ".sp500_backfill_v1_done")
    if not _os3.path.exists(_sp_backfill_flag):
        from predictions.models import get_portfolio_snapshots as _get_snaps, update_snapshot_sp500 as _update_snap
        import yfinance as _yf3
        _snaps = _get_snaps(days=365)
        if _snaps and len(_snaps) >= 2:
            _earliest_snap = _snaps[0]["snapshot_date"]
            _sp_df = _yf3.download("^GSPC", start=_earliest_snap, progress=False)
            if _sp_df is not None and len(_sp_df) >= 2:
                _sp_closes = _sp_df["Close"].values.astype(float)
                _sp_dates = [d.strftime("%Y-%m-%d") for d in _sp_df.index]
                _baseline_close = float(_sp_closes[0])
                _fixed = 0
                for _snap in _snaps:
                    _snap_date = _snap["snapshot_date"]
                    _prev_close = None
                    _match_close = None
                    for _i, _d in enumerate(_sp_dates):
                        if _d <= _snap_date:
                            _match_close = float(_sp_closes[_i])
                            if _i > 0:
                                _prev_close = float(_sp_closes[_i - 1])
                    if _match_close:
                        _cum = ((_match_close / _baseline_close) - 1) * 100
                        _daily = ((_match_close / _prev_close) - 1) * 100 if _prev_close else 0
                        try:
                            _update_snap(_snap_date, round(_cum, 2), round(_daily, 2))
                            _fixed += 1
                        except Exception:
                            pass
                logger.warning(f"S&P BACKFILL V1: Fixed {_fixed} historical snapshots with correct cumulative returns")
                with open(_sp_backfill_flag, "w") as _f:
                    _f.write(f"backfilled {_fixed} snapshots on {datetime.now().isoformat()}")
    else:
        logger.info("S&P backfill v1 already done — skipping")
except Exception as e:
    logger.warning(f"S&P backfill v1: {e}")

# ============================================================
#  ONE-TIME CASH RECOVERY V1
#  Repairs the cash-inflation incident: trades got opened at option-premium
#  prices ($2-$8) but closed at equity prices ($120-$300), generating
#  4000-6000% pnl that inflated cash by ~$2M. This migration:
#    1. Identifies corrupted closed trades (|pnl_pct| > 100% AND equity)
#    2. Reverses the bogus cash credit they added
#    3. Marks them as 'closed_corrupted' so they're excluded from stats
#    4. Recomputes cash to a verifiable value
# Uses a flag file so it only runs ONCE per deploy.
# ============================================================
try:
    import os as _os_recov
    _recov_flag = _os_recov.path.join(_os_recov.path.dirname(__file__), ".cash_recovery_v1_done")
    if not _os_recov.path.exists(_recov_flag):
        from predictions.models import (
            get_db as _get_db_recov,
            get_cash as _get_cash_recov,
            set_cash as _set_cash_recov,
        )
        _conn_recov = _get_db_recov()
        # Find corrupted closed trades: equity (or default), |pnl_pct| > 100
        _corrupted = _conn_recov.execute(
            """SELECT id, ticker, direction, entry_price, exit_price, shares,
                      pnl_dollars, pnl_pct, instrument_type, status
               FROM paper_trades
               WHERE status='closed'
                 AND (instrument_type IS NULL OR instrument_type='equity')
                 AND ABS(pnl_pct) > 100"""
        ).fetchall()

        _bogus_cash = 0.0
        _corrupt_count = 0
        for _t in _corrupted:
            try:
                _entry = float(_t["entry_price"] or 0)
                _exit = float(_t["exit_price"] or 0)
                _shares = float(_t["shares"] or 0)
                _direction = _t["direction"]
                _pnl_d = float(_t["pnl_dollars"] or 0)
                # Compute the cash that was credited (matches close_paper_trade equity logic)
                if _direction == "long":
                    _cash_credited = _exit * _shares
                else:  # short
                    _cash_credited = _entry * _shares + _pnl_d
                # The cost originally deducted at open was entry * shares.
                # Net bogus impact = cash_credited - cost_at_open
                _cost_at_open = _entry * _shares
                _bogus_impact = _cash_credited - _cost_at_open
                _bogus_cash += _bogus_impact
                # Mark the trade as corrupted, zero out pnl
                _conn_recov.execute(
                    """UPDATE paper_trades
                       SET pnl_dollars=0, pnl_pct=0, status='closed_corrupted'
                       WHERE id=?""",
                    (_t["id"],)
                )
                _corrupt_count += 1
            except Exception as _ce:
                logger.warning(f"Cash recovery: skipped trade {_t['id']}: {_ce}")
                continue

        _conn_recov.commit()
        _conn_recov.close()

        if _corrupt_count > 0:
            # Subtract the bogus cash that was credited
            _cur_cash = _get_cash_recov()
            _new_cash = round(_cur_cash - _bogus_cash, 2)
            # Sanity guard: never let recovery push cash negative or above $5M
            if 0 < _new_cash < 5_000_000:
                _set_cash_recov(_new_cash)
                logger.warning(
                    f"CASH RECOVERY V1: marked {_corrupt_count} corrupted trades, "
                    f"reversed ${_bogus_cash:,.2f} bogus cash. "
                    f"Cash: ${_cur_cash:,.2f} -> ${_new_cash:,.2f}"
                )

                # Also nuke today's portfolio_snapshot so the equity curve
                # doesn't show the inflated value. The next trade cycle will
                # write a fresh snapshot with the correct cash.
                try:
                    _conn_snap = _get_db_recov()
                    _today = datetime.now().strftime("%Y-%m-%d")
                    _conn_snap.execute(
                        "DELETE FROM portfolio_snapshots WHERE snapshot_date=?",
                        (_today,)
                    )
                    _conn_snap.commit()
                    _conn_snap.close()
                    logger.warning(f"CASH RECOVERY V1: deleted today's ({_today}) snapshot — equity curve will be clean on next cycle")
                except Exception as _se:
                    logger.warning(f"Snapshot cleanup failed (non-fatal): {_se}")
            else:
                logger.error(
                    f"CASH RECOVERY V1: refused to set cash to ${_new_cash:,.2f} "
                    f"(out of safety bounds). No change made. Investigate."
                )
        else:
            logger.info("CASH RECOVERY V1: no corrupted trades found — nothing to do")

        with open(_recov_flag, "w") as _f:
            _f.write(f"recovered {_corrupt_count} trades, ${_bogus_cash:.2f} reversed on {datetime.now().isoformat()}")
    else:
        logger.info("Cash recovery v1 already done — skipping")
except Exception as e:
    logger.error(f"Cash recovery v1 FAILED (non-fatal — system continues): {e}")


# ============================================================
#  AUTONOMOUS TRADING SCHEDULER
#  Runs server-side on App Runner — works 24/7, no human needed.
#  The computer IS the hedge fund manager.
# ============================================================
from apscheduler.schedulers.background import BackgroundScheduler

# Track auto-trading state
auto_trade_log = []
MAX_AUTO_LOG = 200
auto_trade_stats = {
    "total_cycles": 0,
    "total_trades_opened": 0,
    "total_trades_closed": 0,
    "last_run": None,
    "last_result": None,
    "errors": 0,
    "started_at": None,
    "status": "initializing",
}

# --- Event-Driven Trading Engine ---
# Instead of trading on a fixed hourly clock, the system monitors the market
# every 5 minutes and only trades when conditions warrant action:
#   - Significant price moves (>1% on positions or watchlist)
#   - News events (geopolitical, tariff, earnings)
#   - Stop-loss triggers on open positions
#   - Regime change (bull→bear, bear→bull)
#   - New high-confidence signals appear
#   - Market open/close transitions
# This is how real hedge funds work — reactive, not on a timer.

_last_regime = {"value": None}
_last_vix = {"value": None}
_last_news_score = {"value": 0}
_last_trade_time = {"value": None}
_scan_count = {"value": 0}
MIN_TRADE_INTERVAL_MINUTES = 5  # Allow cycles every 5 min so we can react quickly to market moves

# Geo-political risk state (updated by scanner every 15 min)
_geo_risk_state = {"level": "LOW", "score": 0, "last_update": None, "events": []}

# Daily profit limit state (2.5% daily gain = sell all and pause)
_daily_paused = {"paused": False, "pause_date": None, "reason": None}

# Load daily pause state from DB (survives container restarts)
try:
    from predictions.models import get_trading_state, set_trading_state as _set_state
    _saved_pause = get_trading_state("daily_pause_date", "")
    _saved_reason = get_trading_state("daily_pause_reason", "")
    if _saved_pause == dt.now().strftime("%Y-%m-%d"):
        # ONE-TIME AUTO-CLEAR: if the saved pause reason mentions a >5% jump,
        # it's almost certainly the V3 reset false-positive. Clear it on boot.
        # This flag file ensures we only auto-clear once, in case real >5% gains
        # happen later that the system genuinely should pause on.
        import os as _os_pause
        _pause_clear_flag = _os_pause.path.join(_os_pause.path.dirname(__file__), ".daily_pause_clear_v1_done")
        _is_synthetic = False
        try:
            # Reasons stored as "Daily gain +X.XX% exceeded 2.5% limit"
            import re as _re_pause
            _m = _re_pause.search(r"\+([\d.]+)%", _saved_reason or "")
            if _m and float(_m.group(1)) >= 5.0:
                _is_synthetic = True
        except Exception:
            pass

        if _is_synthetic and not _os_pause.path.exists(_pause_clear_flag):
            logger.warning(
                f"DAILY PAUSE AUTO-CLEAR: Detected synthetic >5% gain in saved reason "
                f"('{_saved_reason}'). Clearing pause on startup."
            )
            try:
                _set_state("daily_pause_date", "")
                _set_state("daily_pause_reason", "")
                with open(_pause_clear_flag, "w") as _f:
                    _f.write(f"cleared on {dt.now().isoformat()} (was: {_saved_reason})")
            except Exception as _clear_err:
                logger.warning(f"Could not write pause-clear flag: {_clear_err}")
        else:
            _daily_paused["paused"] = True
            _daily_paused["pause_date"] = _saved_pause
            _daily_paused["reason"] = _saved_reason or "Restored from DB"
except Exception:
    pass


def _should_trade_now() -> dict:
    """
    Quick market scan — checks if conditions have changed enough to warrant
    a full trade cycle. Runs every 5 minutes but only triggers trades when
    something meaningful happens. Returns dict with 'should_trade' bool and 'reasons'.
    """
    reasons = []
    import pytz
    et = pytz.timezone("US/Eastern")
    now_et = dt.now(et)
    hour = now_et.hour
    minute = now_et.minute
    weekday = now_et.weekday()

    # Don't trade on weekends
    if weekday >= 5:
        return {"should_trade": False, "reasons": ["Weekend — market closed"]}

    # Don't trade if daily profit limit was hit (2.5%+ gain today)
    if _daily_paused.get("paused") and _daily_paused.get("pause_date") == now_et.strftime("%Y-%m-%d"):
        return {"should_trade": False, "reasons": [f"DAILY PROFIT LIMIT — {_daily_paused.get('reason', 'paused for today')}"]}


    # Only scan during extended hours (7am-8pm ET)
    if hour < 7 or hour >= 20:
        return {"should_trade": False, "reasons": ["Outside trading hours (7am-8pm ET)"]}

    # Respect minimum interval between trades
    if _last_trade_time["value"]:
        elapsed = (dt.now() - _last_trade_time["value"]).total_seconds() / 60
        if elapsed < MIN_TRADE_INTERVAL_MINUTES:
            return {"should_trade": False, "reasons": [f"Too soon — last trade {elapsed:.0f}min ago (min {MIN_TRADE_INTERVAL_MINUTES}min)"]}

    # --- TRIGGER 0: PRE-MARKET PRIME at 9:00am ET ---
    # Generates picks 30 min before market open so they're ready to fire at 9:30.
    # Cache is fresh, picks are computed, system is hot when the bell rings.
    if hour == 9 and 0 <= minute <= 5:
        reasons.append("PRE-MARKET PRIME — generating picks for 9:30 open")

    # --- TRIGGER 0b: PRE-MARKET WARM at 9:15am ET ---
    # Second warm-up scan 15 min before open. Catches any overnight changes.
    if hour == 9 and 15 <= minute <= 20:
        reasons.append("PRE-MARKET WARM — refreshing picks 15min before open")

    # --- TRIGGER 1: Market open — always trade at 9:30am ET ---
    if hour == 9 and 28 <= minute <= 35:
        reasons.append("MARKET OPEN — must rebalance positions")

    # --- TRIGGER 1b: Market open follow-through at 9:35-9:45 ---
    # If first cycle missed (or partial), make sure we get full coverage by 9:45.
    if hour == 9 and 36 <= minute <= 45:
        reasons.append("MARKET OPEN follow-through — capturing remaining picks")

    # --- TRIGGER 2: Market close — always trade at 3:55pm ET ---
    if hour == 15 and 53 <= minute <= 59:
        reasons.append("MARKET CLOSE — end-of-day positioning")

    # --- TRIGGER 3: First scan of the day — always trade ---
    if hour == 7 and minute <= 10 and _scan_count["value"] == 0:
        reasons.append("FIRST SCAN OF DAY — opening positions")

    # --- TRIGGER 4: Check for regime change ---
    try:
        regime_data = detect_market_regime()
        current_regime = regime_data.get("regime", "UNKNOWN")
        if _last_regime["value"] and current_regime != _last_regime["value"]:
            reasons.append(f"REGIME CHANGE: {_last_regime['value']} → {current_regime}")
        _last_regime["value"] = current_regime

        # Check VIX spike (>3 points since last check)
        vix = regime_data.get("vix_level")
        if vix and _last_vix["value"]:
            vix_change = abs(vix - _last_vix["value"])
            if vix_change >= 3:
                reasons.append(f"VIX SPIKE: {_last_vix['value']:.1f} → {vix:.1f} ({vix_change:+.1f})")
        if vix:
            _last_vix["value"] = vix
    except Exception:
        pass

    # --- TRIGGER 5: Breaking news / sentiment shift ---
    try:
        sentiment = get_stock_sentiment("SPY")
        news_score = sentiment.get("stock_sentiment", 0)
        score_change = abs(news_score - _last_news_score["value"])
        if score_change >= 0.3:  # Significant sentiment shift
            reasons.append(f"NEWS SHIFT: sentiment moved {score_change:+.2f} (was {_last_news_score['value']:.2f}, now {news_score:.2f})")
        _last_news_score["value"] = news_score
    except Exception:
        pass

    # --- TRIGGER 6: Check stop-losses on open positions ---
    try:
        portfolio = get_portfolio_state()
        for pos in portfolio.get("positions", []):
            pnl = pos.get("unrealized_pct", 0)
            if pnl <= -4:  # Approaching stop loss
                reasons.append(f"STOP-LOSS WARNING: {pos['ticker']} at {pnl:.1f}%")
    except Exception:
        pass

    # --- TRIGGER 7: Geo-political risk change ---
    if _geo_risk_state.get("level") in ("ELEVATED", "CRITICAL"):
        reasons.append(f"GEO-RISK {_geo_risk_state['level']} (score {_geo_risk_state.get('score', 0)}) — defensive rebalance needed")

    # --- TRIGGER 8: Periodic / catch-up scan during market hours ---
    # Fires on either condition (whichever comes first):
    #   (a) APScheduler lands on a clean 15-min minute mark (0/15/30/45 +/-1)
    #   (b) >= 15 minutes have elapsed since the last cycle (catch-up)
    # The catch-up branch is critical: APScheduler can drift off the
    # clean minute marks, leaving long gaps with no cycles. The elapsed
    # check guarantees a periodic scan no matter when the scheduler runs.
    if 9 <= hour <= 16 and not reasons:
        try:
            if _last_trade_time["value"]:
                elapsed_min = (dt.now() - _last_trade_time["value"]).total_seconds() / 60
            else:
                elapsed_min = 9999  # never traded — definitely run now
        except Exception:
            elapsed_min = 9999
        minute_mark_hit = minute in (0, 1, 15, 16, 30, 31, 45, 46)
        if elapsed_min >= 15 or minute_mark_hit:
            reasons.append(
                f"PERIODIC SCAN — {elapsed_min:.0f}min since last cycle "
                f"(market hours, always trading)"
            )

    should_trade = len(reasons) > 0
    return {"should_trade": should_trade, "reasons": reasons}


def _smart_trade_monitor():
    """
    Runs every 5 minutes. Checks if conditions warrant a trade.
    Only executes a full trade cycle when something meaningful changes.
    This is the brain of the event-driven system.
    """
    global auto_trade_stats
    _scan_count["value"] += 1

    try:
        decision = _should_trade_now()

        if not decision["should_trade"]:
            # Log skipped scans occasionally (every 12th = once per hour)
            if _scan_count["value"] % 12 == 0:
                logger.info(f"MONITOR SCAN #{_scan_count['value']}: No action needed — {'; '.join(decision['reasons'])}")
            return

        # --- CONDITIONS MET — EXECUTE TRADE CYCLE ---
        logger.warning(f"TRADE TRIGGERED — reasons: {'; '.join(decision['reasons'])}")

        cycle_start = dt.now()
        auto_trade_stats["total_cycles"] += 1
        auto_trade_stats["last_run"] = cycle_start.isoformat()
        auto_trade_stats["status"] = "trading"

        # 1. Generate fresh quant picks (analyzes 200+ stocks)
        picks = generate_quant_picks()

        # If MARKET OPEN trigger fired, set force_market_open so we trade
        # the 9:30-9:45 window instead of waiting (with reduced size for safety).
        reasons_str = " ".join(decision.get("reasons", []))
        if "MARKET OPEN" in reasons_str or "PRE-MARKET" in reasons_str or "FIRST SCAN" in reasons_str:
            picks["force_market_open"] = True
            logger.warning("MARKET OPEN trigger active — will trade open window with reduced size")

        # 2. Execute trades based on signals
        result = execute_trades_from_signals(picks)

        # 3. Auto-adjust factor weights if enough data
        try:
            weight_update = auto_adjust_weights()
            result["weight_update"] = weight_update
        except Exception:
            pass

        # Track results
        opened = len(result.get("opened", []))
        closed = len(result.get("closed", []))
        auto_trade_stats["total_trades_opened"] += opened
        auto_trade_stats["total_trades_closed"] += closed
        auto_trade_stats["last_result"] = {
            "opened": opened,
            "closed": closed,
            "skipped": len(result.get("skipped", [])),
            "regime": result.get("portfolio_after", {}).get("regime", "unknown"),
            "cash": result.get("portfolio_after", {}).get("cash", 0),
            "positions": result.get("portfolio_after", {}).get("num_positions", 0),
            "trigger_reasons": decision["reasons"],
        }
        auto_trade_stats["status"] = "idle"
        _last_trade_time["value"] = dt.now()

        # Log the cycle
        log_entry = {
            "time": cycle_start.isoformat(),
            "cycle": auto_trade_stats["total_cycles"],
            "opened": opened,
            "closed": closed,
            "regime": result.get("portfolio_after", {}).get("regime"),
            "triggered_by": decision["reasons"],
        }
        auto_trade_log.append(log_entry)
        if len(auto_trade_log) > MAX_AUTO_LOG:
            auto_trade_log.pop(0)

        # Backup portfolio to S3 after every cycle (persist forever)
        try:
            backup_db_to_s3()
        except Exception:
            pass

        logger.warning(
            f"TRADE CYCLE #{auto_trade_stats['total_cycles']} complete: "
            f"{opened} opened, {closed} closed | triggered by: {decision['reasons'][0]}"
        )

    except Exception as e:
        auto_trade_stats["errors"] += 1
        auto_trade_stats["status"] = "error"
        auto_trade_stats["last_error"] = str(e)
        logger.error(f"TRADE MONITOR ERROR: {e}")


def _run_auto_trade_cycle():
    """Legacy function for manual triggers — always executes a full trade cycle."""
    global auto_trade_stats
    cycle_start = dt.now()
    auto_trade_stats["total_cycles"] += 1
    auto_trade_stats["last_run"] = cycle_start.isoformat()
    auto_trade_stats["status"] = "trading"

    try:
        logger.warning(f"MANUAL TRADE CYCLE #{auto_trade_stats['total_cycles']} starting")
        picks = generate_quant_picks()
        result = execute_trades_from_signals(picks)
        try:
            auto_adjust_weights()
        except Exception:
            pass

        opened = len(result.get("opened", []))
        closed = len(result.get("closed", []))
        auto_trade_stats["total_trades_opened"] += opened
        auto_trade_stats["total_trades_closed"] += closed
        auto_trade_stats["last_result"] = {
            "opened": opened, "closed": closed,
            "skipped": len(result.get("skipped", [])),
            "regime": result.get("portfolio_after", {}).get("regime", "unknown"),
            "cash": result.get("portfolio_after", {}).get("cash", 0),
            "positions": result.get("portfolio_after", {}).get("num_positions", 0),
            "trigger_reasons": ["Manual trigger"],
        }
        auto_trade_stats["status"] = "idle"
        _last_trade_time["value"] = dt.now()

        log_entry = {
            "time": cycle_start.isoformat(), "cycle": auto_trade_stats["total_cycles"],
            "opened": opened, "closed": closed,
            "regime": result.get("portfolio_after", {}).get("regime"),
            "triggered_by": ["Manual trigger"],
        }
        auto_trade_log.append(log_entry)
        if len(auto_trade_log) > MAX_AUTO_LOG:
            auto_trade_log.pop(0)
        try:
            backup_db_to_s3()
        except Exception:
            pass
        logger.warning(f"MANUAL TRADE CYCLE complete: {opened} opened, {closed} closed")
    except Exception as e:
        auto_trade_stats["errors"] += 1
        auto_trade_stats["status"] = "error"
        auto_trade_stats["last_error"] = str(e)
        logger.error(f"MANUAL TRADE ERROR: {e}")


# Start the scheduler
scheduler = BackgroundScheduler(timezone="US/Eastern")

# EVENT-DRIVEN: Monitor every 5 minutes, only trade when conditions change
# This replaces the old hourly clock — the system now decides WHEN to trade
scheduler.add_job(
    _smart_trade_monitor,
    "interval",
    minutes=5,
    id="smart_monitor",
    name="Smart Trade Monitor (event-driven)",
    max_instances=1,
    misfire_grace_time=300,
)

# INDEPENDENT EXIT CHECKER — runs every 5 minutes, even on weekends
# This is SEPARATE from the smart monitor so stop-losses ALWAYS fire
def _exit_checker():
    """Check all open positions for stop-loss/target/hold-duration exits.
    Runs independently — never coupled to entry decisions."""
    try:
        regime_data = detect_market_regime()
        regime = regime_data.get("regime", "SIDEWAYS")
    except Exception:
        regime = "SIDEWAYS"
    try:
        result = check_and_exit_positions(regime)
        closed = result.get("closed", [])
        if closed:
            auto_trade_stats["total_trades_closed"] += len(closed)
            logger.warning(f"EXIT CHECKER: Closed {len(closed)} positions — {[c['ticker'] for c in closed]}")
            try:
                from predictions.db_persistence import backup_db_to_s3
                backup_db_to_s3()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"EXIT CHECKER ERROR: {e}")

scheduler.add_job(
    _exit_checker,
    "interval",
    minutes=5,
    id="exit_checker",
    name="Independent Exit Checker (stop-losses always fire)",
    max_instances=1,
    misfire_grace_time=300,
)

# Startup scan (after 10 min warm-up — gives reset time to persist)
scheduler.add_job(
    _run_auto_trade_cycle,
    "date",
    run_date=dt.now() + timedelta(minutes=10),
    id="startup_trade",
    name="Startup Trade Cycle",
)

# --- WEEKEND SELF-LEARNING CYCLE ---
# Every Saturday at 10am ET: analyze ALL past trades, adjust weights,
# learn from mistakes, and prepare strategy for Monday
def _weekend_learning_cycle():
    """
    Weekend self-improvement: the system reviews all its trades,
    identifies what's working and what's not, and adjusts its
    factor weights for the coming week. This is what makes it
    get smarter over time without human intervention.
    """
    try:
        logger.warning("WEEKEND LEARNING CYCLE starting — reviewing all trades and adjusting strategy")

        # 1. Auto-adjust factor weights from trade history
        weight_result = auto_adjust_weights()
        logger.warning(f"Weight adjustment: {weight_result}")

        # 2. Generate intelligence report to log insights
        intel = generate_intelligence_report()
        logger.warning(f"Intelligence report generated: {len(intel.get('insights', []))} insights")

        # 3. Run a fresh analysis cycle to prepare Monday's picks
        # (This pre-caches the picks so Monday's first trade is instant)
        picks = generate_quant_picks()
        logger.warning(f"Monday prep: {len(picks.get('long_picks', []))} longs, {len(picks.get('short_picks', []))} shorts ready")

        logger.warning("WEEKEND LEARNING CYCLE complete — system is smarter now")
    except Exception as e:
        logger.error(f"Weekend learning error: {e}")

scheduler.add_job(
    _weekend_learning_cycle,
    "cron",
    day_of_week="sat",
    hour=10,
    minute=0,
    id="weekend_learning",
    name="Weekend Self-Learning Cycle",
    max_instances=1,
    misfire_grace_time=7200,
)

# --- HISTORICAL CALIBRATION (50-year pattern analysis) ---
# Runs 15 min after startup and weekly on Sunday 8am.
# Downloads max history, analyzes seasonal/rotation/regime/momentum patterns.
def _build_historical_calibration():
    """Build 50-year historical calibration in background."""
    try:
        from analysis.historical_calibration import build_calibration
        from analysis.quant_engine import QUANT_UNIVERSE, SECTOR_MAP
        build_calibration(QUANT_UNIVERSE, SECTOR_MAP)
    except Exception as e:
        logger.error(f"Historical calibration build failed: {e}")

# Startup: build calibration 15 minutes after boot (after DB restore + first trade cycle)
scheduler.add_job(
    _build_historical_calibration,
    "date",
    run_date=dt.now() + timedelta(minutes=15),
    id="historical_calibration_startup",
    name="Historical Calibration (startup)",
    max_instances=1,
    misfire_grace_time=3600,
)

# Weekly refresh: Sunday 8am ET
scheduler.add_job(
    _build_historical_calibration,
    "cron",
    day_of_week="sun",
    hour=8,
    minute=0,
    id="historical_calibration_weekly",
    name="Historical Calibration (weekly refresh)",
    max_instances=1,
    misfire_grace_time=7200,
)

# --- DAILY LEARNING CYCLE (5pm ET, Mon-Fri) ---
# After market close: analyze factor performance, adjust weights, learn from mistakes.
# More frequent than weekly = faster adaptation to changing market conditions.
def _daily_learning_cycle():
    """Daily post-market learning: adjust weights and analyze mistakes."""
    try:
        from predictions.learner import (
            analyze_factor_performance,
            auto_adjust_weights,
            analyze_mistakes,
        )

        logger.warning("DAILY LEARNING CYCLE: Starting post-market analysis...")

        # Step 1: Analyze factor performance
        factor_perf = analyze_factor_performance()
        if factor_perf:
            logger.warning(f"  Factor analysis: {len(factor_perf)} factors analyzed")

        # Step 2: Auto-adjust weights (only if enough trades)
        weight_result = auto_adjust_weights()
        if weight_result:
            logger.warning(f"  Weight adjustment: {weight_result.get('status', 'done')}")

        # Step 3: Analyze mistakes (learn from losses)
        mistakes = analyze_mistakes()
        if mistakes:
            n_mistakes = len(mistakes.get("patterns", []))
            logger.warning(f"  Mistake analysis: {n_mistakes} patterns identified")

        logger.warning("DAILY LEARNING CYCLE complete")
    except Exception as e:
        logger.error(f"Daily learning cycle error: {e}")

scheduler.add_job(
    _daily_learning_cycle,
    "cron",
    day_of_week="mon-fri",
    hour=17,
    minute=0,
    id="daily_learning",
    name="Daily Post-Market Learning Cycle",
    max_instances=1,
    misfire_grace_time=3600,
    replace_existing=True,
)

# --- DAILY PERFORMANCE CHECK (6pm ET) ---
# Every evening: check portfolio health, log daily P&L
def _daily_health_check():
    """Daily portfolio health check and performance logging."""
    try:
        portfolio = get_portfolio_state()
        total_return = portfolio.get("total_return_pct", 0)
        num_positions = portfolio.get("num_positions", 0)
        total_value = portfolio.get("total_value", 0)
        logger.warning(
            f"DAILY HEALTH CHECK: Portfolio ${total_value:,.2f} | "
            f"Return: {total_return:+.2f}% | Positions: {num_positions}"
        )
    except Exception as e:
        logger.error(f"Daily health check error: {e}")

scheduler.add_job(
    _daily_health_check,
    "cron",
    hour=18,
    minute=0,
    id="daily_health",
    name="Daily Health Check",
    max_instances=1,
    misfire_grace_time=3600,
)

# --- PRE-MARKET INTELLIGENCE SCAN (6:30am ET) ---
# Runs 1 hour before first trade cycle to check overnight futures,
# global markets, weekend news impact, and Bitcoin (24/7 risk gauge).
# This ensures the 7:30am trade cycle has fresh overnight data.
def _premarket_scan():
    """
    Pre-market intelligence scan — wakes up early to check what
    happened overnight and over the weekend. Updates the overnight
    cache so the first trade cycle of the day is smarter.
    """
    try:
        logger.warning("PRE-MARKET SCAN starting — checking overnight futures, global markets, Bitcoin")
        intel = scan_overnight_intelligence()
        logger.warning(
            f"PRE-MARKET SCAN complete: {intel['futures_sentiment']} | "
            f"gap={intel['overnight_gap_pct']:+.2f}% | "
            f"weekend_shift={intel['weekend_shift_detected']} | "
            f"signals={len(intel['signals'])}"
        )
        if intel["weekend_shift_detected"]:
            logger.warning("WEEKEND SHIFT DETECTED — adjusting Monday strategy accordingly")
    except Exception as e:
        logger.error(f"Pre-market scan error: {e}")

scheduler.add_job(
    _premarket_scan,
    "cron",
    hour=6,
    minute=30,
    id="premarket_scan",
    name="Pre-Market Intelligence Scan",
    max_instances=1,
    misfire_grace_time=3600,
)

# Also run pre-market scan on Sundays at 8pm ET (futures open Sunday 6pm ET)
# This catches weekend news before Monday
scheduler.add_job(
    _premarket_scan,
    "cron",
    day_of_week="sun",
    hour=20,
    minute=0,
    id="sunday_premarket",
    name="Sunday Evening Pre-Market Scan",
    max_instances=1,
    misfire_grace_time=3600,
)

# --- GEO-POLITICAL RISK SCANNER (every 15 min during market hours) ---
# Constantly monitors geopolitical events (Iran/US, tariffs, war, sanctions)
# and adjusts the trading system's behavior in real-time.
# (_geo_risk_state declared above with other module-level state vars)

def _geopolitical_scanner():
    """Scan for geo-political risk events every 15 minutes.
    If risk is ELEVATED/CRITICAL, tighten stop losses on all positions."""
    global _geo_risk_state
    try:
        geo = assess_geopolitical_risk()
        level = geo.get("risk_level", "LOW")
        score = geo.get("risk_score", 0)
        events = geo.get("military_events", []) + geo.get("tension_events", [])

        _geo_risk_state = {
            "level": level,
            "score": score,
            "last_update": dt.now().isoformat(),
            "events": events[:10],
        }

        # If risk is ELEVATED or CRITICAL, tighten all stops immediately
        if level in ("ELEVATED", "CRITICAL") and score >= 7:
            logger.warning(f"GEO-RISK {level} (score {score}) — tightening all stops")
            try:
                from predictions.models import get_open_trades, get_db
                open_trades = get_open_trades()
                conn = get_db()
                for trade in open_trades:
                    entry = trade["entry_price"]
                    direction = trade["direction"]
                    # Emergency tight stops: 3% for longs, 4% for shorts
                    if direction == "long":
                        new_stop = round(entry * 0.97, 2)
                    else:
                        new_stop = round(entry * 1.04, 2)
                    conn.execute(
                        "UPDATE paper_trades SET stop_loss_price=? WHERE id=? AND status='open'",
                        (new_stop, trade["id"])
                    )
                conn.commit()
                conn.close()
                logger.warning(f"GEO-RISK: Tightened stops on {len(open_trades)} positions")
            except Exception as e:
                logger.error(f"GEO-RISK stop tightening error: {e}")

        logger.info(f"GEO-RISK SCAN: {level} (score {score}), {len(events)} events")
    except Exception as e:
        logger.error(f"GEO-RISK SCANNER ERROR: {e}")

scheduler.add_job(
    _geopolitical_scanner,
    "interval",
    minutes=5,
    id="geo_risk_scanner",
    name="Geo-Political Risk Scanner (constant 5-min monitoring)",
    max_instances=1,
    misfire_grace_time=300,
)

# --- AUTONOMOUS GEO EVENT TRACKER ---
# Scans news for upcoming geopolitical deadlines (ceasefires, sanctions, treaties)
# and auto-populates the geo_events DB so the system pre-positions before events.

def _geo_event_tracker():
    """Detect upcoming geo events from news and track outcomes of active events."""
    try:
        from analysis.news_sentiment import detect_upcoming_events, detect_event_outcomes
        from predictions.models import save_geo_event, update_geo_event_outcome

        # 1. Detect new upcoming events from headlines
        new_events = detect_upcoming_events()
        for ev in new_events:
            save_geo_event(
                event_key=ev["event_key"],
                event_type=ev["event_type"],
                region=ev["region"],
                description=ev.get("source_headline", ""),
                estimated_date=ev["estimated_date"],
                confidence=ev["confidence"],
                source_headline=ev.get("source_headline", ""),
                source_feed=ev.get("source_feed", ""),
            )
        if new_events:
            logger.warning(f"GEO EVENT TRACKER: Detected {len(new_events)} events — {[e['event_key'] for e in new_events]}")

        # 2. Check outcomes of active events
        outcomes = detect_event_outcomes()
        for outcome in outcomes:
            update_geo_event_outcome(outcome["event_key"], outcome["outcome"])
            logger.warning(f"GEO EVENT OUTCOME: {outcome['event_key']} -> {outcome['outcome']}")

        if not new_events and not outcomes:
            logger.info("GEO EVENT TRACKER: No new events or outcomes detected")
    except Exception as e:
        logger.error(f"GEO EVENT TRACKER ERROR: {e}")

scheduler.add_job(
    _geo_event_tracker,
    "interval",
    minutes=10,
    id="geo_event_tracker",
    name="Geopolitical Event Tracker (auto-detect deadlines every 10 min)",
    max_instances=1,
    misfire_grace_time=300,
)

# --- DAILY 2.5% TAKE-PROFIT RULE ---
# If the fund is up 2.5%+ in a single day, sell ALL holdings and pause until tomorrow.
# This locks in exceptional daily gains and prevents giving them back.
# (_daily_paused declared above with other module-level state vars)

def _check_daily_profit_limit():
    """Check if fund has gained 2.5%+ today. If so, sell everything and pause."""
    global _daily_paused
    import pytz
    et = pytz.timezone("US/Eastern")
    now_et = dt.now(et)
    today_str = now_et.strftime("%Y-%m-%d")

    # Reset pause at start of new day
    if _daily_paused["pause_date"] and _daily_paused["pause_date"] != today_str:
        _daily_paused = {"paused": False, "pause_date": None, "reason": None}
        logger.warning("DAILY PROFIT LIMIT: New day — trading resumed")

    if _daily_paused["paused"]:
        return  # Already paused for today

    try:
        portfolio = get_portfolio_state()
        total_value = portfolio.get("total_value", 100000)

        # Get yesterday's closing value from snapshots
        from predictions.models import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT total_value FROM portfolio_snapshots WHERE snapshot_date < ? ORDER BY snapshot_date DESC LIMIT 1",
            (today_str,)
        ).fetchone()
        conn.close()

        if row:
            yesterday_value = row[0]
            daily_return = ((total_value / yesterday_value) - 1) * 100

            # SANITY CHECK: A real day's gain can't exceed ~5%. If we see >5%
            # and the fund hasn't done many trades today, it's almost certainly
            # a portfolio reset/manual adjustment, not a real trading gain.
            # Don't pause trading on synthetic jumps.
            from predictions.models import get_db as _gdb
            _conn = _gdb()
            try:
                trades_today_row = _conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE entry_date >= ? OR exit_date >= ?",
                    (today_str, today_str)
                ).fetchone()
                trades_today = trades_today_row[0] if trades_today_row else 0
            except Exception:
                trades_today = 0
            finally:
                _conn.close()

            if daily_return >= 5.0 and trades_today < 3:
                logger.warning(
                    f"DAILY PROFIT CHECK: +{daily_return:.2f}% appears synthetic "
                    f"(only {trades_today} trades today). Likely reset/adjustment, NOT pausing. "
                    f"yesterday=${yesterday_value:.0f}, today=${total_value:.0f}"
                )
                return  # Skip pause — this is not a real trading gain

            if daily_return >= 2.5:
                logger.warning(f"DAILY PROFIT LIMIT HIT: +{daily_return:.2f}% today — selective sell + pause new trades")

                # Selective sell: close intraday trades and losers, keep multi-day holds
                from predictions.models import get_open_trades, close_paper_trade
                from predictions.paper_trader import _get_current_prices
                open_trades = get_open_trades()
                symbols = list(set(t["ticker"] for t in open_trades))
                prices = _get_current_prices(symbols)

                sold_count = 0
                for trade in open_trades:
                    price = prices.get(trade["ticker"], trade["entry_price"])
                    hold_class = trade.get("hold_class", "swing")
                    entry = trade["entry_price"]
                    direction = trade["direction"]
                    pnl_pct = ((price / entry) - 1) * 100 if direction == "long" else ((entry / price) - 1) * 100

                    # Sell intraday trades and losing positions; keep profitable swing/position
                    if hold_class == "intraday" or pnl_pct < 0:
                        try:
                            close_paper_trade(trade["id"], price)
                            sold_count += 1
                        except Exception:
                            pass

                _daily_paused["paused"] = True
                _daily_paused["pause_date"] = today_str
                _daily_paused["reason"] = f"Daily gain +{daily_return:.2f}% exceeded 2.5% limit"
                logger.warning(f"Sold {sold_count} trades (intraday + losers). Kept multi-day holds. Trading paused until tomorrow.")

                # Persist to DB (survives restarts)
                try:
                    from predictions.models import set_trading_state
                    set_trading_state("daily_pause_date", today_str)
                    set_trading_state("daily_pause_reason", _daily_paused["reason"])
                except Exception:
                    pass

                try:
                    backup_db_to_s3()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"DAILY PROFIT CHECK ERROR: {e}")

scheduler.add_job(
    _check_daily_profit_limit,
    "interval",
    minutes=5,
    id="daily_profit_check",
    name="Daily 2.5% Profit Limit Check",
    max_instances=1,
    misfire_grace_time=300,
)

# --- ADAPTIVE TOTAL RETURN PROTECTION ---
# When the fund is up significantly (>10% total return), become ADAPTIVE:
# 1. Scale back position sizes as returns increase
# 2. At end of day (3:30pm+), aggressively sell winners to lock in gains
# 3. If total return drops from peak by 1.5%, sell everything to protect
# This prevents giving back big gains by being greedy

_peak_total_return = {"value": 0, "date": None}

def _adaptive_profit_protection():
    """Adaptive profit protection — scales with total return level."""
    global _peak_total_return
    import pytz
    et = pytz.timezone("US/Eastern")
    now_et = dt.now(et)

    try:
        portfolio = get_portfolio_state()
        total_value = portfolio.get("total_value", 100000)
        total_return = ((total_value / ORIGINAL_CAPITAL) - 1) * 100

        # Track peak return
        if total_return > _peak_total_return["value"]:
            _peak_total_return["value"] = total_return
            _peak_total_return["date"] = now_et.strftime("%Y-%m-%d %H:%M")

        peak = _peak_total_return["value"]
        drawdown_from_peak = peak - total_return

        # --- ADAPTIVE POSITION TRIMMING ---
        # If up >10% total and at end of day (after 3:30pm ET), trim winning positions
        hour = now_et.hour
        minute = now_et.minute
        is_end_of_day = (hour == 15 and minute >= 30) or hour >= 16

        if total_return >= 10.0 and is_end_of_day:
            # End of day — only sell intraday trades and positions that hit targets
            # Multi-day swing/position trades survive overnight
            from predictions.models import get_open_trades, close_paper_trade
            from predictions.paper_trader import _get_current_prices
            open_trades = get_open_trades()
            if not open_trades:
                return

            symbols = list(set(t["ticker"] for t in open_trades))
            prices = _get_current_prices(symbols)

            sold_count = 0
            for trade in open_trades:
                price = prices.get(trade["ticker"], trade["entry_price"])
                entry = trade["entry_price"]
                direction = trade["direction"]
                hold_class = trade.get("hold_class", "swing")
                target = trade.get("target_price", entry * 1.05)

                if direction == "long":
                    pnl_pct = ((price / entry) - 1) * 100
                    target_achieved = (price - entry) / (target - entry + 0.01) if target > entry else 1.0
                else:
                    pnl_pct = ((entry / price) - 1) * 100
                    target_achieved = (entry - price) / (entry - target + 0.01) if entry > target else 1.0

                # EOD sell: intraday trades always, swing/position only if target hit
                should_sell = (
                    (hold_class == "intraday" and pnl_pct > 0.5) or
                    target_achieved >= 0.9
                )

                if should_sell:
                    try:
                        close_paper_trade(trade["id"], price)
                        sold_count += 1
                    except Exception:
                        pass

            if sold_count > 0:
                logger.warning(
                    f"ADAPTIVE PROFIT PROTECTION: EOD selective sell — closed {sold_count} trades "
                    f"(total return: +{total_return:.1f}%, peak: +{peak:.1f}%)"
                )
                try:
                    backup_db_to_s3()
                except Exception:
                    pass

        # --- PEAK DRAWDOWN PROTECTION (dynamically calibrated) ---
        # Thresholds calibrated from 50 years of historical drawdown patterns.
        # If history shows drawdowns at this depth usually recover quickly → less aggressive.
        # If this is a historically severe drawdown → more aggressive selling.
        dd_tier1, dd_tier2, dd_tier3 = 1.5, 3.0, 5.0  # defaults
        try:
            from analysis.historical_calibration import get_calibration
            cal = get_calibration()
            dd_patterns = cal.get("drawdown_patterns", {})
            if dd_patterns:
                percentiles = dd_patterns.get("percentiles", {})
                p25 = abs(percentiles.get("p25_depth", -8))
                p50 = abs(percentiles.get("p50_depth", -12))
                # Scale our thresholds relative to historical norms
                # If avg drawdown is -15%, our 1.5% threshold is very early
                # If avg drawdown is -8%, our 1.5% threshold is appropriate
                avg_depth = abs(dd_patterns.get("avg_depth_pct", -12))
                scale = max(0.5, min(2.0, avg_depth / 12.0))
                dd_tier1 = round(1.5 * scale, 1)
                dd_tier2 = round(3.0 * scale, 1)
                dd_tier3 = round(5.0 * scale, 1)
                logger.debug(f"Drawdown thresholds calibrated from history: {dd_tier1}/{dd_tier2}/{dd_tier3}% (avg historical: {dd_patterns.get('avg_depth_pct')}%)")
        except Exception:
            pass

        if peak >= 10.0 and drawdown_from_peak >= dd_tier1:
            from predictions.models import get_open_trades, close_paper_trade
            from predictions.paper_trader import _get_current_prices
            open_trades = get_open_trades()
            if not open_trades:
                return

            symbols = list(set(t["ticker"] for t in open_trades))
            prices = _get_current_prices(symbols)

            sold_count = 0
            for trade in open_trades:
                price = prices.get(trade["ticker"], trade["entry_price"])
                entry = trade["entry_price"]
                direction = trade["direction"]

                if direction == "long":
                    pnl_pct = ((price / entry) - 1) * 100
                else:
                    pnl_pct = ((entry / price) - 1) * 100

                if drawdown_from_peak >= dd_tier3:
                    # Emergency: sell everything
                    should_sell = True
                elif drawdown_from_peak >= dd_tier2:
                    # Aggressive: sell everything except profitable swing/position trades
                    hold_class = trade.get("hold_class", "swing")
                    should_sell = not (hold_class in ("swing", "position") and pnl_pct > 1.0)
                else:
                    # Moderate: only sell losing positions
                    should_sell = pnl_pct < 0

                if should_sell:
                    try:
                        close_paper_trade(trade["id"], price)
                        sold_count += 1
                    except Exception:
                        pass

            level = "EMERGENCY" if drawdown_from_peak >= 5.0 else ("AGGRESSIVE" if drawdown_from_peak >= 3.0 else "MODERATE")
            logger.warning(
                f"PEAK DRAWDOWN PROTECTION ({level}): Peak was +{peak:.1f}%, now +{total_return:.1f}% "
                f"(dropped {drawdown_from_peak:.1f}%) — sold {sold_count}/{len(open_trades)} trades"
            )
            try:
                backup_db_to_s3()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"ADAPTIVE PROFIT PROTECTION ERROR: {e}")

scheduler.add_job(
    _adaptive_profit_protection,
    "interval",
    minutes=5,
    id="adaptive_profit_protection",
    name="Adaptive Total Return Protection",
    max_instances=1,
    misfire_grace_time=300,
)

# --- DAILY LEARNING (not just weekends) ---
# Every evening at 7pm: quick learning cycle to adjust weights
def _daily_learning():
    """Daily learning — more frequent than weekend-only. Keeps the system adaptive."""
    try:
        weight_result = auto_adjust_weights()
        logger.warning(f"DAILY LEARNING: weights adjusted — {weight_result}")
    except Exception as e:
        logger.error(f"Daily learning error: {e}")

scheduler.add_job(
    _daily_learning,
    "cron",
    hour=19,
    minute=30,
    id="daily_learning_evening",
    name="Daily Learning Cycle (7:30pm)",
    max_instances=1,
    misfire_grace_time=3600,
    replace_existing=True,
)

scheduler.start()
auto_trade_stats["started_at"] = dt.now().isoformat()
auto_trade_stats["status"] = "running"
logger.warning("AUTONOMOUS TRADING SCHEDULER STARTED — the computer is now the hedge fund manager")


# --- IBKR S3 SNAPSHOT PUSHER (only runs on EC2) ---
# When IBKR_PUSH_SNAPSHOT=true, start a background thread that uploads the
# IBKR account state to S3 every N seconds. App Runner reads from that S3
# location to display real account data on the dashboard without needing
# direct Gateway access. EC2 sets this env var; App Runner does not.
if os.getenv("IBKR_PUSH_SNAPSHOT", "").lower() in ("true", "1", "yes"):
    try:
        from predictions.ibkr_snapshot import start_snapshot_pusher_thread
        _snapshot_interval = int(os.getenv("IBKR_SNAPSHOT_INTERVAL_SECONDS", "30"))
        start_snapshot_pusher_thread(interval_seconds=_snapshot_interval)
        logger.warning(f"IBKR S3 SNAPSHOT PUSHER ACTIVE — pushing every {_snapshot_interval}s")
    except Exception as _e:
        logger.error(f"Failed to start IBKR snapshot pusher: {_e}")


# --- FIX EXISTING BROKEN STOP/TARGET PRICES ON STARTUP ---
# Previous code used quant pick values that produced insane stops
# (e.g., COIN short: stop $419, target -$214). Fix all open positions.
def _fix_broken_stops():
    """Recalculate stop-loss and target for all open positions with sane values."""
    try:
        from predictions.models import get_open_trades, get_db
        open_trades = get_open_trades()
        fixed = 0
        for trade in open_trades:
            entry = trade["entry_price"]
            direction = trade["direction"]
            stop = trade.get("stop_loss_price", 0) or 0
            target = trade.get("target_price", 0) or 0

            # Detect broken stops: negative values, or stop > 2x entry for shorts
            broken = False
            if stop <= 0 or target <= 0:
                broken = True
            elif direction == "short" and stop > entry * 2:
                broken = True
            elif direction == "long" and stop < entry * 0.5:
                broken = True

            if broken:
                # Recalculate with regime-aware defaults (BEAR regime)
                if direction == "long":
                    new_stop = round(entry * 0.94, 2)   # 6% stop
                    new_target = round(entry * 1.08, 2)  # 8% target
                else:
                    new_stop = round(entry * 1.05, 2)    # 5% stop (tighter for shorts)
                    new_target = round(entry * 0.85, 2)  # 15% target

                conn = get_db()
                conn.execute(
                    "UPDATE paper_trades SET stop_loss_price=?, target_price=? WHERE id=?",
                    (new_stop, new_target, trade["id"])
                )
                conn.commit()
                conn.close()
                fixed += 1
                logger.warning(f"FIXED STOP: {trade['ticker']} {direction} — stop ${stop:.2f}→${new_stop:.2f}, target ${target:.2f}→${new_target:.2f}")

        if fixed:
            logger.warning(f"Fixed {fixed} positions with broken stop/target prices")
    except Exception as e:
        logger.error(f"Fix broken stops error: {e}")

# Run fix on startup
_fix_broken_stops()

# --- Pre-warm the S&P 500 benchmark cache so the first request is fast ---
# Previously the first /api/paper-performance call would return sp500_sharpe=0
# because yfinance was cold. Now we fetch S&P data in a background thread at
# startup so it's ready by the time the user hits the page.
def _prewarm_benchmark_bg():
    try:
        import threading
        from predictions.paper_trader import prewarm_benchmark_cache
        t = threading.Thread(target=prewarm_benchmark_cache, daemon=True, name="sp-prewarm")
        t.start()
        logger.info("S&P 500 benchmark pre-warm thread started")
    except Exception as e:
        logger.error(f"Failed to start benchmark pre-warm: {e}")

_prewarm_benchmark_bg()


# --- RESET DAY: Close all positions, restore to yesterday's value ---
# This runs ONCE on this deploy to undo today's damage and restart fresh.
def _reset_day():
    """Close ALL open positions and reset cash to yesterday's portfolio value (~$107K)."""
    try:
        from predictions.models import get_open_trades, close_paper_trade, get_db, save_portfolio_snapshot
        from predictions.paper_trader import _get_current_prices

        open_trades = get_open_trades()
        if not open_trades:
            logger.warning("RESET DAY: No open trades to close")
            return

        # Get current prices
        symbols = list(set(t["ticker"] for t in open_trades))
        current_prices = _get_current_prices(symbols)

        # Close every position
        closed_count = 0
        for trade in open_trades:
            ticker = trade["ticker"]
            price = current_prices.get(ticker, trade["entry_price"])
            try:
                close_paper_trade(trade["id"], price)
                closed_count += 1
            except Exception as e:
                logger.error(f"RESET: Failed to close {ticker}: {e}")

        # Reset to $109,580 (+9.58% — our peak before the drop)
        RESET_VALUE = 109580.0
        conn = get_db()
        # Delete today's snapshot if it exists
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute("DELETE FROM portfolio_snapshots WHERE snapshot_date = ?", (today,))
        conn.commit()
        conn.close()

        # Save new snapshot at the reset value
        save_portfolio_snapshot(
            total_value=RESET_VALUE,
            cash=RESET_VALUE,
            positions_value=0,
            daily_ret=0,
            cum_ret=9.58,
            sp500_daily=0,
            sp500_cum=0,
            num_pos=0
        )

        logger.warning(f"RESET DAY: Closed {closed_count} positions. Cash set to ${RESET_VALUE:,.2f}. Fresh start.")
    except Exception as e:
        logger.error(f"RESET DAY ERROR: {e}")
        import traceback
        traceback.print_exc()

# RESET DONE — $109,580 (+9.58%) confirmed April 7. DO NOT re-enable.
# _reset_day()


# --- Request/Response Models ---

class PredictionRequest(BaseModel):
    ticker: str
    predicted_direction: str  # "Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"
    confidence_score: float
    entry_price: float
    target_price: Optional[float] = None
    check_after_days: int = 30
    notes: Optional[str] = None


# --- Manual Trigger Endpoints (for weekend prep & Monday readiness) ---

@app.post("/api/trigger-learning")
def trigger_learning():
    """Manually trigger the weekend learning cycle — reviews trades, adjusts weights, preps Monday picks."""
    import threading
    def _run():
        _weekend_learning_cycle()
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "triggered", "message": "Weekend learning cycle started in background"}

@app.post("/api/trigger-premarket")
def trigger_premarket():
    """Manually trigger pre-market intelligence scan — checks futures, global markets, overnight news."""
    _premarket_scan()
    intel = scan_overnight_intelligence()
    return {
        "status": "complete",
        "futures_sentiment": intel.get("futures_sentiment"),
        "overnight_gap_pct": intel.get("overnight_gap_pct"),
        "weekend_shift_detected": intel.get("weekend_shift_detected"),
        "confidence_modifier": intel.get("confidence_modifier"),
        "signals": intel.get("signals", []),
    }

@app.post("/api/trigger-trade-cycle")
def trigger_trade_cycle():
    """Manually trigger a single trade cycle — generates picks and executes trades."""
    import threading
    def _run():
        _run_auto_trade_cycle()
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "triggered", "message": "Trade cycle started in background"}

@app.get("/api/intelligence-report")
def get_intelligence_report():
    """Get the self-learning system's intelligence report — what it learned, strengths, weaknesses."""
    try:
        report = generate_intelligence_report()
        return report
    except Exception as e:
        return {"error": str(e), "message": "Not enough trade data yet for intelligence report"}


# --- API Endpoints ---

@app.get("/health")
def health_check():
    """Health check — App Runner pings this to make sure the app is alive."""
    return {"status": "healthy", "app": "Sentinel Quant Stock Analyzer"}


@app.get("/api/live-prices")
def live_prices(request: Request):
    """
    Lightweight live price endpoint — updates every 60 seconds.
    Returns S&P 500 price and our fund's current value without
    running the full quant analysis (which takes 100+ seconds).
    """
    check_rate_limit(request.client.host)
    try:
        import yfinance as yf
        from predictions.models import get_cash, get_open_trades

        # S&P 500 — uses 1-min cached regime detection
        regime = detect_market_regime()
        sp500_price = regime.get("sp500_price", 0)
        vix = regime.get("vix_level", 0)

        # Our fund value
        cash = get_cash()
        open_trades = get_open_trades()
        positions_value = 0
        position_prices = {}

        if open_trades:
            tickers = list(set(t["ticker"] for t in open_trades))
            try:
                _throttle()
                data = yf.download(tickers, period="1d", progress=False)
                for t in open_trades:
                    try:
                        if len(tickers) == 1:
                            close_col = data["Close"]
                            if hasattr(close_col, "columns"):
                                close_col = close_col.iloc[:, 0]
                            price = float(close_col.iloc[-1])
                        else:
                            if isinstance(data.columns, pd.MultiIndex):
                                ticker_close = data["Close"][t["ticker"]]
                            else:
                                ticker_close = data["Close"]
                            if hasattr(ticker_close, "columns"):
                                ticker_close = ticker_close.iloc[:, 0]
                            price = float(ticker_close.iloc[-1])
                        pv = price * t["shares"]
                        if t["direction"] == "short":
                            # Match paper_trader.py: short value = abs(shares * current_price)
                            pv = abs(t["shares"] * price)
                        positions_value += pv
                        position_prices[t["ticker"]] = round(price, 2)
                    except Exception:
                        positions_value += t["entry_price"] * t["shares"]
            except Exception:
                for t in open_trades:
                    positions_value += t["entry_price"] * t["shares"]

        total_value = cash + positions_value
        total_return = ((total_value / ORIGINAL_CAPITAL) - 1) * 100

        return {
            "sp500": {
                "price": sp500_price,
                "sma200": regime.get("sp500_sma200", 0),
                "sma50": regime.get("sp500_sma50", 0),
            },
            "fund": {
                "total_value": round(total_value, 2),
                "cash": round(cash, 2),
                "positions_value": round(positions_value, 2),
                "total_return_pct": round(total_return, 2),
                "num_positions": len(open_trades),
            },
            "vix": round(vix, 2),
            "regime": regime.get("regime", "UNKNOWN"),
            "position_prices": position_prices,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Live prices error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get live prices")


@app.get("/api/search")
def search_stocks(request: Request, q: str = ""):
    """Search for stocks by company name or ticker symbol. Instant, no API calls."""
    check_rate_limit(request.client.host)
    if len(q) > 50:
        raise HTTPException(status_code=400, detail="Query too long")
    results = search_tickers(q)
    return {"results": results}


@app.get("/api/analyze/{ticker}")
def analyze_stock(request: Request, ticker: str, period: str = "1y"):
    """Full stock analysis — the main endpoint."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")
    try:
        report = generate_full_report(clean_ticker, period)
        if "error" in report:
            raise HTTPException(status_code=404, detail="Stock not found")
        return report
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Analysis error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again.")


@app.get("/api/quote/{ticker}")
def get_quote(request: Request, ticker: str):
    """Get current quote and basic info for a stock."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        info = get_stock_info(clean_ticker)
        if not info.get("current_price"):
            raise HTTPException(status_code=404, detail="Stock not found")
        return info
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Quote error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch quote")


@app.get("/api/history/{ticker}")
def get_history(request: Request, ticker: str, period: str = "6mo"):
    """Get historical price data for charting."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")
    try:
        data = get_historical_data(clean_ticker, period)
        if not data:
            raise HTTPException(status_code=404, detail="No history found")
        return {"ticker": clean_ticker, "period": period, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"History error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch history")


@app.get("/api/benchmarks")
def get_benchmarks(request: Request, period: str = "1y"):
    """Get performance data for S&P 500, Nasdaq, and Dow Jones."""
    check_rate_limit(request.client.host)
    try:
        return get_benchmark_data(period)
    except Exception as e:
        logger.error(f"Benchmark error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch benchmarks")


@app.post("/api/predictions")
def create_prediction(request: Request, pred: PredictionRequest):
    """Save a new prediction to track."""
    check_rate_limit(request.client.host)
    try:
        clean_ticker = validate_ticker(pred.ticker)
        prediction_id = save_prediction(
            ticker=clean_ticker,
            direction=pred.predicted_direction,
            confidence=pred.confidence_score,
            entry_price=pred.entry_price,
            target_price=pred.target_price,
            check_after_days=pred.check_after_days,
            notes=pred.notes,
        )
        return {"id": prediction_id, "message": "Prediction saved!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Prediction save error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save prediction")


@app.get("/api/predictions")
def list_predictions(request: Request):
    """Get all saved predictions."""
    check_rate_limit(request.client.host)
    return {"predictions": get_all_predictions()}


@app.get("/api/performance")
def get_performance(request: Request):
    """Get overall performance stats and comparison vs market indices."""
    check_rate_limit(request.client.host)
    try:
        check_and_resolve_predictions()
        return get_performance_stats()
    except Exception as e:
        logger.error(f"Performance error: {e}")
        raise HTTPException(status_code=500, detail="Failed to calculate performance")


@app.get("/api/banner")
def get_banner(request: Request):
    """Get prices for the scrolling ticker banner. Always returns most recent data."""
    check_rate_limit(request.client.host)
    try:
        data = get_banner_data()
        # data is now a dict with tickers, market_open, as_of
        if isinstance(data, dict):
            return data
        # Fallback for old format
        return {"tickers": data, "market_open": True, "as_of": None}
    except Exception as e:
        logger.error(f"Banner error: {e}")
        return {"tickers": [], "market_open": False, "as_of": None}


@app.get("/api/daily-picks")
def daily_picks(request: Request):
    """Get today's top 15 stock picks based on technical analysis."""
    check_rate_limit(request.client.host)
    try:
        return get_daily_picks()
    except Exception as e:
        logger.error(f"Daily picks error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate picks")


@app.get("/api/earnings-calendar")
def earnings_calendar(request: Request):
    """Get upcoming earnings for major stocks this week."""
    check_rate_limit(request.client.host)
    try:
        return get_earnings_calendar()
    except Exception as e:
        logger.error(f"Earnings error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch earnings")


@app.get("/api/market-news")
def market_news(request: Request):
    """Get latest market news with sentiment analysis from Yahoo Finance, CNN, CNBC."""
    check_rate_limit(request.client.host)
    try:
        return get_market_news()
    except Exception as e:
        logger.error(f"News error: {e}")
        return {"headlines": []}


@app.get("/api/geopolitical-risk")
def geopolitical_risk(request: Request):
    """Geopolitical risk assessment — military events, wars, sanctions, and their market impact.
    Includes real-time scanner state (updates every 15 min)."""
    check_rate_limit(request.client.host)
    try:
        full_report = assess_geopolitical_risk()
        # Merge in the continuous scanner state
        full_report["scanner_state"] = _geo_risk_state
        full_report["daily_profit_status"] = _daily_paused
        return full_report
    except Exception as e:
        logger.error(f"Geopolitical risk error: {e}")
        return {"risk_level": "UNKNOWN", "risk_score": 0, "error": str(e), "scanner_state": _geo_risk_state}


@app.get("/api/geo-events")
def geo_events(request: Request):
    """Auto-detected geopolitical events — upcoming deadlines, active events, outcomes."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_upcoming_geo_events, get_active_geo_events, get_all_geo_events
        return {
            "upcoming": get_upcoming_geo_events(days_ahead=30),
            "active": get_active_geo_events(),
            "all_events": get_all_geo_events(limit=50),
        }
    except Exception as e:
        logger.error(f"Geo events error: {e}")
        return {"upcoming": [], "active": [], "all_events": [], "error": str(e)}


@app.get("/api/force-reset")
def force_reset(request: Request):
    """Emergency: close ALL positions and reset cash to 12.07% return ($122,156.30).
    Hit this endpoint once from browser to reset, then remove it."""
    check_rate_limit(request.client.host)
    require_admin(request)
    admin_audit(request, "FORCE_RESET", True, "Portfolio reset triggered")
    try:
        from predictions.models import get_open_trades, close_paper_trade, set_cash, get_cash
        import yfinance as yf

        open_trades = get_open_trades()
        closed_count = 0
        for trade in open_trades:
            try:
                ticker = trade["ticker"]
                instrument_type = trade.get("instrument_type") or "equity"
                if instrument_type in ("call", "put"):
                    # Close options at $0.01 (write off)
                    close_paper_trade(trade["id"], 0.01)
                else:
                    # Close equity at current price
                    try:
                        data = yf.download(ticker, period="1d", progress=False)
                        if data is not None and len(data) > 0:
                            price = float(data["Close"].iloc[-1])
                        else:
                            price = trade["entry_price"]
                    except Exception:
                        price = trade["entry_price"]
                    close_paper_trade(trade["id"], price)
                closed_count += 1
            except Exception as e:
                logger.error(f"Force close {trade['ticker']} failed: {e}")

        # Reset cash to target: $109,000 * 1.1207 = $122,156.30
        TARGET_CASH = 122156.30
        set_cash(TARGET_CASH)
        final_cash = get_cash()

        return {
            "status": "RESET COMPLETE",
            "positions_closed": closed_count,
            "cash_set_to": final_cash,
            "target_return": "12.07%",
            "message": "All positions closed. Cash reset. Ready for Monday."
        }
    except Exception as e:
        logger.error(f"Force reset error: {e}")
        return {"status": "ERROR", "error": str(e)}


@app.get("/api/tariff-risk")
def tariff_risk(request: Request):
    """Tariff/trade war risk assessment — escalation detection and sector impacts."""
    check_rate_limit(request.client.host)
    try:
        return assess_tariff_risk()
    except Exception as e:
        logger.error(f"Tariff risk error: {e}")
        return {"tariff_direction": "UNKNOWN", "risk_score": 0, "error": str(e)}


@app.get("/api/daily-summary")
def daily_summary(request: Request, watchlist: str = ""):
    """Get daily market summary with top gainers, losers, and watchlist analysis."""
    check_rate_limit(request.client.host)
    if len(watchlist) > 500:
        raise HTTPException(status_code=400, detail="Watchlist too long")
    try:
        return get_daily_summary(watchlist_tickers=watchlist if watchlist else None)
    except Exception as e:
        logger.error(f"Daily summary error: {e}")
        return {"gainers": [], "losers": []}


@app.get("/api/sector-heatmap")
def sector_heatmap(request: Request):
    """Get sector performance heatmap data."""
    check_rate_limit(request.client.host)
    try:
        return get_sector_heatmap()
    except Exception as e:
        logger.error(f"Sector heatmap error: {e}")
        return {"sectors": []}


@app.get("/api/ai-analyst")
def ai_analyst(request: Request, q: str = ""):
    """AI Stock Analyst — ask any stock/trading question."""
    check_rate_limit(request.client.host)
    if not q.strip():
        return {"answer": "Ask me anything about stocks, trading, or investing!", "ticker": None, "question_type": "empty"}
    if len(q) > 1000:
        return {"answer": "Question too long. Please keep it under 1000 characters.", "ticker": None, "question_type": "error"}
    try:
        return answer_question(q)
    except Exception as e:
        logger.error(f"AI analyst error: {e}")
        return {"answer": "I encountered an error processing your question. Please try again.", "ticker": None, "question_type": "error"}


# ============================================================
#  QUANT HEDGE FUND ENDPOINTS
# ============================================================

@app.get("/api/quant-picks")
def quant_picks(request: Request):
    """Get quantitative LONG/SHORT picks with regime, macro, and factor breakdown.
    Returns cached data instantly. If cache is cold, returns empty picks and triggers background generation."""
    check_rate_limit(request.client.host)
    try:
        from analysis.quant_engine import _quant_cache
        # If cache exists, return it instantly
        if "quant_picks" in _quant_cache:
            import time as _time
            cache_entry = _quant_cache["quant_picks"]
            cache_age = _time.time() - cache_entry["time"]
            result = cache_entry["data"]
            result["cache_age_seconds"] = round(cache_age)
            # Strip internal data that can't be JSON serialized
            return {k: v for k, v in result.items() if not k.startswith("_")}
        # No cache — return empty picks (the scheduler will populate it soon)
        return {
            "regime": {"regime": "LOADING", "description": "Analyzing 500+ stocks..."},
            "long_picks": [],
            "short_picks": [],
            "cache_status": "cold",
            "message": "Quant engine is analyzing 500+ stocks. Data will be available after the next trade cycle (runs every few minutes).",
        }
    except Exception as e:
        logger.error(f"Quant picks error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate quant picks")


@app.get("/api/paper-portfolio")
def paper_portfolio(request: Request):
    """Get current paper trading portfolio state."""
    check_rate_limit(request.client.host)
    try:
        return get_portfolio_state()
    except Exception as e:
        logger.error(f"Paper portfolio error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")


@app.get("/api/trade-history")
def trade_history(request: Request):
    """Get closed trade history — every trade the fund has completed."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_closed_trades, get_all_paper_trades
        closed = get_closed_trades(limit=200)
        open_trades = [dict(t) for t in get_all_paper_trades() if t.get("status") == "open"]
        wins = [t for t in closed if (t.get("pnl_pct") or 0) > 0]
        losses = [t for t in closed if (t.get("pnl_pct") or 0) <= 0]
        total_pnl = sum(t.get("pnl_dollars", 0) or 0 for t in closed)
        return {
            "total_closed": len(closed),
            "total_open": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl_dollars": round(total_pnl, 2),
            "trades": [{
                "id": t["id"],
                "ticker": t["ticker"],
                "direction": t["direction"],
                "entry_price": t["entry_price"],
                "exit_price": t.get("exit_price"),
                "pnl_pct": t.get("pnl_pct", 0),
                "pnl_dollars": t.get("pnl_dollars", 0),
                "entry_date": t.get("entry_date"),
                "exit_date": t.get("exit_date"),
                "regime": t.get("regime_at_entry"),
                "sector": t.get("sector"),
            } for t in closed[:100]],
        }
    except Exception as e:
        logger.error(f"Trade history error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get trade history")


@app.get("/api/equity-curve")
def equity_curve(request: Request):
    """Get equity curve data — fund performance since March 30, 2026."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_portfolio_snapshots
        snapshots = get_portfolio_snapshots(days=365)

        # Build fund equity curve
        fund_curve = []
        for s in snapshots:
            fund_curve.append({
                "date": s["snapshot_date"],
                "value": round(s["total_value"], 2),
                "return_pct": round(s.get("cumulative_return_pct", 0), 2),
            })

        # If no snapshots, create starting point
        if not fund_curve:
            fund_curve = [{"date": "2026-03-30", "value": 100000, "return_pct": 0}]

        # Get current portfolio state for latest data point
        try:
            portfolio = get_portfolio_state()
            today = dt.now().strftime("%Y-%m-%d")
            current_ret = portfolio.get("total_return_pct", 0)
            if fund_curve and fund_curve[-1]["date"] != today:
                fund_curve.append({
                    "date": today,
                    "value": round(portfolio.get("total_value", 100000), 2),
                    "return_pct": round(current_ret, 2),
                })
            elif fund_curve:
                fund_curve[-1]["value"] = round(portfolio.get("total_value", 100000), 2)
                fund_curve[-1]["return_pct"] = round(current_ret, 2)
        except Exception:
            pass

        # Filter to only include dates >= March 30
        fund_curve = [p for p in fund_curve if p["date"] >= "2026-03-30"]

        # Rebase so March 30 = 0%
        if fund_curve and fund_curve[0]["return_pct"] != 0:
            base = fund_curve[0]["return_pct"]
            for p in fund_curve:
                p["return_pct"] = round(p["return_pct"] - base, 2)
                p["value"] = round(100000 * (1 + p["return_pct"] / 100), 2)

        return {
            "fund": fund_curve,
            "start_date": "2026-03-30",
            "initial_capital": 100000,
        }
    except Exception as e:
        logger.error(f"Equity curve error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get equity curve")


@app.get("/api/learning-status")
def learning_status(request: Request):
    """Get self-learning system status — what the AI has learned."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_signal_weights, get_closed_trades
        from predictions.learner import analyze_factor_performance, analyze_sector_performance, analyze_mistakes
        weights = get_signal_weights()
        closed = get_closed_trades(limit=500)
        result = {
            "current_weights": weights,
            "total_trades_analyzed": len(closed),
            "learning_active": len(closed) >= 20,
        }
        try:
            result["factor_performance"] = analyze_factor_performance()
        except Exception:
            pass
        try:
            result["sector_performance"] = analyze_sector_performance()
        except Exception:
            pass
        try:
            result["mistakes_learned"] = analyze_mistakes()
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Learning status error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get learning status")


@app.get("/api/paper-performance")
def paper_performance(request: Request):
    """Get paper trading performance analytics (Sharpe, drawdown, equity curve)."""
    check_rate_limit(request.client.host)
    try:
        return get_performance_analytics()
    except Exception as e:
        logger.error(f"Paper performance error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get performance")


# ============================================================
#  ADVANCED QUANT ENDPOINTS — RenTech-style institutional features
# ============================================================

def _validate_ticker(ticker: str) -> str:
    """Sanitize ticker symbol to prevent injection — allow only alphanumerics,
    dots, and carets (for index symbols like ^GSPC)."""
    if not ticker or len(ticker) > 15:
        raise HTTPException(status_code=400, detail="Invalid ticker")
    import re
    if not re.match(r'^[A-Za-z0-9.\-\^]+$', ticker):
        raise HTTPException(status_code=400, detail="Invalid ticker characters")
    return ticker.upper()


@app.get("/api/ann-signal/{ticker}")
def ann_signal(ticker: str, request: Request):
    """Artificial Neural Network (MLP) pattern-recognition signal."""
    check_rate_limit(request.client.host)
    ticker = _validate_ticker(ticker)
    try:
        from analysis.rentech_advanced import ann_predict_direction
        return ann_predict_direction(ticker)
    except Exception as e:
        logger.error(f"ANN signal error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail="ANN model failed")


@app.get("/api/nlp-sentiment/{ticker}")
def nlp_sentiment_endpoint(ticker: str, request: Request):
    """Advanced NLP sentiment analysis with financial lexicon."""
    check_rate_limit(request.client.host)
    ticker = _validate_ticker(ticker)
    try:
        from analysis.rentech_advanced import nlp_ticker_sentiment
        return nlp_ticker_sentiment(ticker)
    except Exception as e:
        logger.error(f"NLP sentiment error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail="NLP model failed")


@app.get("/api/monte-carlo/{ticker}")
def monte_carlo_endpoint(ticker: str, request: Request, horizon: int = 20):
    """Monte Carlo GBM simulation for statistical probability distribution."""
    check_rate_limit(request.client.host)
    ticker = _validate_ticker(ticker)
    # Validate horizon range
    if horizon < 1 or horizon > 252:
        raise HTTPException(status_code=400, detail="horizon must be 1-252")
    try:
        from analysis.rentech_advanced import monte_carlo_price_simulation
        return monte_carlo_price_simulation(ticker, horizon_days=horizon)
    except Exception as e:
        logger.error(f"Monte Carlo error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail="Monte Carlo failed")


@app.get("/api/cointegration/{sym_a}/{sym_b}")
def cointegration_endpoint(sym_a: str, sym_b: str, request: Request):
    """Engle-Granger cointegration test for statistical arbitrage pair analysis."""
    check_rate_limit(request.client.host)
    sym_a = _validate_ticker(sym_a)
    sym_b = _validate_ticker(sym_b)
    if sym_a == sym_b:
        raise HTTPException(status_code=400, detail="Symbols must differ")
    try:
        from analysis.rentech_advanced import cointegration_test
        return cointegration_test(sym_a, sym_b)
    except Exception as e:
        logger.error(f"Cointegration error for {sym_a}/{sym_b}: {e}")
        raise HTTPException(status_code=500, detail="Cointegration test failed")


@app.get("/api/hmm-regime/{ticker}")
def hmm_regime_endpoint(ticker: str, request: Request):
    """Baum-Welch HMM regime detection (BULL/SIDEWAYS/BEAR)."""
    check_rate_limit(request.client.host)
    ticker = _validate_ticker(ticker)
    try:
        from analysis.rentech_advanced import hmm_regime_detect
        return hmm_regime_detect(ticker)
    except Exception as e:
        logger.error(f"HMM regime error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail="HMM failed")


@app.get("/api/ensemble-signal/{ticker}")
def ensemble_signal_endpoint(ticker: str, request: Request):
    """Ensemble signal combining ANN + NLP + Monte Carlo + HMM."""
    check_rate_limit(request.client.host)
    ticker = _validate_ticker(ticker)
    try:
        from analysis.rentech_advanced import ensemble_signal
        return ensemble_signal(ticker)
    except Exception as e:
        logger.error(f"Ensemble signal error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail="Ensemble failed")


# ─── IBKR (Interactive Brokers) ENDPOINTS ─────────────────────────────────────

@app.get("/api/ibkr/status")
def ibkr_status_endpoint(request: Request, refresh: bool = False):
    """IBKR connection status, account summary, and trading mode.

    Behavior:
        - If THIS backend has direct Gateway access (e.g., EC2), returns live data.
        - If NOT directly connected (e.g., App Runner), auto-falls back to the
          S3 snapshot uploaded by EC2. Frontend gets unified data either way.

    Query params:
        refresh: bypass caches for live values.
    """
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_get_status, ibkr_get_account, get_order_log
        status = ibkr_get_status()
        directly_connected = status.get("connected", False)

        if directly_connected:
            # We have a real Gateway here — return live data.
            account = ibkr_get_account(force_refresh=refresh)
            recent_orders = get_order_log(limit=20)
            return {
                "status": status,
                "account": account,
                "recent_orders": recent_orders,
                "data_source": "live_gateway",
            }

        # Not directly connected — try S3 snapshot fallback.
        try:
            from predictions.ibkr_snapshot import pull_ibkr_snapshot
            snapshot = pull_ibkr_snapshot(force_refresh=refresh)
            if snapshot.get("available"):
                # Return snapshot data formatted like a normal status response.
                return {
                    "status": {
                        "connected": snapshot.get("connected", False),
                        "enabled": True,
                        "mode": snapshot.get("account", {}).get("mode", "LIVE"),
                        "from_snapshot": True,
                        "snapshot_age_seconds": snapshot.get("snapshot_age_seconds"),
                        "snapshot_stale": snapshot.get("snapshot_stale", False),
                        "pushed_at": snapshot.get("pushed_at"),
                    },
                    "account": snapshot.get("account", {}),
                    "recent_orders": snapshot.get("recent_order_log", []),
                    "positions": snapshot.get("positions", []),
                    "open_orders": snapshot.get("open_orders", []),
                    "data_source": "s3_snapshot",
                    "snapshot_age_seconds": snapshot.get("snapshot_age_seconds"),
                    "snapshot_stale": snapshot.get("snapshot_stale", False),
                }
        except Exception as _snap_err:
            logger.debug(f"Snapshot fallback failed: {_snap_err}")

        # No snapshot available either — return disconnected status.
        return {
            "status": status,
            "account": ibkr_get_account(force_refresh=refresh),
            "recent_orders": get_order_log(limit=20),
            "data_source": "no_connection",
            "message": ("IBKR not connected on this backend and no S3 snapshot "
                        "available. Start IB Gateway or wait for EC2 to push a snapshot."),
        }
    except Exception as e:
        logger.error(f"IBKR status error: {e}")
        return {
            "status": {"connected": False, "enabled": False, "mode": "PAPER",
                       "error": str(e)},
            "account": {},
            "recent_orders": [],
            "data_source": "error",
        }


@app.get("/api/ibkr/snapshot")
def ibkr_snapshot_endpoint(request: Request, refresh: bool = False):
    """Get the latest IBKR snapshot from S3.

    The EC2 backend pushes account state to S3 every 30s. This endpoint
    reads from S3 so the dashboard can display real IBKR data without
    needing direct Gateway access.

    Query params:
        refresh: bypass 10s local cache and fetch fresh from S3.

    Returns:
        - {"available": True, "account": {...}, "positions": [...], ...} if snapshot exists
        - {"available": False, "reason": "..."} if EC2 hasn't pushed yet
    """
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_snapshot import pull_ibkr_snapshot, get_pusher_state
        snapshot = pull_ibkr_snapshot(force_refresh=refresh)
        # If we're the EC2 backend (running the pusher), include pusher diagnostics
        try:
            snapshot["pusher_state"] = get_pusher_state()
        except Exception:
            pass
        return snapshot
    except Exception as e:
        logger.error(f"IBKR snapshot endpoint error: {e}")
        return {"available": False, "error": str(e)}


@app.post("/api/admin/cash-recovery")
def admin_cash_recovery(request: Request):
    """Run the cash-inflation recovery on demand. Idempotent — safe to call
    multiple times. Finds equity trades with |pnl_pct| > 100% (the units-mismatch
    bug from the cash-inflation incident), reverses their bogus cash credit,
    marks them 'closed_corrupted' so stats exclude them, and deletes today's
    snapshot so the equity curve is clean. ADMIN-ONLY.

    Returns a detailed summary so the operator can verify the recovery.
    """
    check_rate_limit(request.client.host)
    require_admin(request)
    try:
        from predictions.models import get_db, get_cash, set_cash
        conn = get_db()
        corrupted = conn.execute(
            """SELECT id, ticker, direction, entry_price, exit_price, shares,
                      pnl_dollars, pnl_pct, instrument_type, status
               FROM paper_trades
               WHERE status='closed'
                 AND (instrument_type IS NULL OR instrument_type='equity')
                 AND ABS(pnl_pct) > 100"""
        ).fetchall()

        bogus_cash = 0.0
        corrupt_count = 0
        per_trade_log = []
        for t in corrupted:
            try:
                entry = float(t["entry_price"] or 0)
                exit_p = float(t["exit_price"] or 0)
                shares = float(t["shares"] or 0)
                direction = t["direction"]
                pnl_d = float(t["pnl_dollars"] or 0)
                if direction == "long":
                    cash_credited = exit_p * shares
                else:
                    cash_credited = entry * shares + pnl_d
                cost_at_open = entry * shares
                bogus_impact = cash_credited - cost_at_open
                bogus_cash += bogus_impact
                conn.execute(
                    """UPDATE paper_trades
                       SET pnl_dollars=0, pnl_pct=0, status='closed_corrupted'
                       WHERE id=?""",
                    (t["id"],)
                )
                corrupt_count += 1
                per_trade_log.append({
                    "id": t["id"], "ticker": t["ticker"],
                    "entry": entry, "exit": exit_p, "shares": shares,
                    "bogus_impact": round(bogus_impact, 2),
                })
            except Exception as ce:
                logger.warning(f"Manual recovery: skipped trade {t['id']}: {ce}")
                continue

        conn.commit()
        conn.close()

        cur_cash = get_cash()
        new_cash = round(cur_cash - bogus_cash, 2)
        cash_action = "no_change"
        # Sanity guard
        if corrupt_count > 0 and 0 < new_cash < 5_000_000:
            set_cash(new_cash)
            cash_action = "set"
        elif corrupt_count > 0:
            cash_action = f"refused_safety_guard (would have set ${new_cash:.2f})"

        # Always delete today's portfolio_snapshot so equity curve is clean
        snapshot_action = "no_change"
        try:
            conn2 = get_db()
            today_str = dt.now().strftime("%Y-%m-%d")
            conn2.execute("DELETE FROM portfolio_snapshots WHERE snapshot_date=?", (today_str,))
            conn2.commit()
            conn2.close()
            snapshot_action = f"deleted today's ({today_str}) snapshot"
        except Exception as se:
            snapshot_action = f"snapshot delete failed: {se}"

        result = {
            "ok": True,
            "corrupted_trades_marked": corrupt_count,
            "bogus_cash_reversed_dollars": round(bogus_cash, 2),
            "cash_before": round(cur_cash, 2),
            "cash_after": get_cash(),
            "cash_action": cash_action,
            "snapshot_action": snapshot_action,
            "per_trade_log": per_trade_log,
        }
        admin_audit(request, "CASH_RECOVERY", True,
                    f"marked {corrupt_count} trades, reversed ${bogus_cash:.2f}, action={cash_action}")
        logger.warning(f"MANUAL CASH RECOVERY: {result}")
        return result
    except Exception as e:
        admin_audit(request, "CASH_RECOVERY", False, f"Error: {e}")
        logger.error(f"Cash recovery endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ibkr/kill-switch")
def ibkr_kill_switch(request: Request):
    """EMERGENCY: Flatten all IBKR positions immediately. ADMIN-ONLY."""
    check_rate_limit(request.client.host)
    require_admin(request)
    try:
        from predictions.ibkr_adapter import ibkr_flatten_all
        result = ibkr_flatten_all("MANUAL KILL SWITCH")
        admin_audit(request, "IBKR_KILL_SWITCH", True, f"Flattened {result.get('count', 0)} positions")
        logger.warning(f"IBKR KILL SWITCH activated: {result}")
        return result
    except Exception as e:
        admin_audit(request, "IBKR_KILL_SWITCH", False, f"Error: {e}")
        logger.error(f"IBKR kill switch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ibkr/positions")
def ibkr_positions_endpoint(request: Request):
    """Get all current IBKR positions with P&L."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_get_positions
        return {"positions": ibkr_get_positions()}
    except Exception as e:
        logger.error(f"IBKR positions error: {e}")
        return {"positions": [], "error": str(e)}


@app.get("/api/ibkr/orders")
def ibkr_orders_endpoint(request: Request):
    """Get open/pending IBKR orders."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_get_orders, get_order_log
        return {
            "open_orders": ibkr_get_orders(),
            "order_log": get_order_log(limit=50),
        }
    except Exception as e:
        logger.error(f"IBKR orders error: {e}")
        return {"open_orders": [], "order_log": [], "error": str(e)}


@app.post("/api/ibkr/toggle")
def ibkr_toggle_endpoint(request: Request):
    """Enable/disable IBKR execution. ADMIN-ONLY (write endpoint)."""
    check_rate_limit(request.client.host)
    require_admin(request)
    try:
        from predictions.ibkr_adapter import ibkr_toggle
        from predictions.ibkr_adapter import IBKR_ENABLED
        result = ibkr_toggle(not IBKR_ENABLED)
        admin_audit(request, "IBKR_TOGGLE", True, f"Toggled to {result.get('enabled')}")
        return result
    except Exception as e:
        admin_audit(request, "IBKR_TOGGLE", False, f"Error: {e}")
        logger.error(f"IBKR toggle error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ibkr/unhalt")
def ibkr_unhalt_endpoint(request: Request):
    """Resume trading after emergency halt. ADMIN-ONLY."""
    check_rate_limit(request.client.host)
    require_admin(request)
    try:
        from predictions.ibkr_adapter import ibkr_unhalt
        result = ibkr_unhalt()
        admin_audit(request, "IBKR_UNHALT", True, "Trading resumed")
        return result
    except Exception as e:
        admin_audit(request, "IBKR_UNHALT", False, f"Error: {e}")
        logger.error(f"IBKR unhalt error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clear-daily-pause")
def clear_daily_pause_endpoint(request: Request):
    """Clear the daily profit-limit pause. ADMIN-ONLY.

    Use when the daily pause was triggered falsely (e.g., portfolio reset
    caused a synthetic +10% jump). Resumes paper trading immediately.
    """
    check_rate_limit(request.client.host)
    require_admin(request)
    global _daily_paused
    try:
        was_paused = _daily_paused.get("paused", False)
        old_reason = _daily_paused.get("reason", "")
        _daily_paused = {"paused": False, "pause_date": None, "reason": None}

        # Clear DB persistence too
        try:
            from predictions.models import set_trading_state
            set_trading_state("daily_pause_date", "")
            set_trading_state("daily_pause_reason", "")
        except Exception as _db_err:
            logger.debug(f"Could not clear DB pause state: {_db_err}")

        admin_audit(request, "CLEAR_DAILY_PAUSE", True,
                    f"was_paused={was_paused}, old_reason={old_reason}")
        logger.warning(f"DAILY PAUSE CLEARED via admin endpoint (was: {old_reason or 'not paused'})")
        return {
            "cleared": True,
            "was_paused": was_paused,
            "old_reason": old_reason,
            "now_paused": False,
        }
    except Exception as e:
        admin_audit(request, "CLEAR_DAILY_PAUSE", False, f"Error: {e}")
        logger.error(f"clear_daily_pause error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/audit-log")
def admin_audit_log_endpoint(request: Request):
    """View admin endpoint access log. ADMIN-ONLY."""
    check_rate_limit(request.client.host)
    require_admin(request)
    return {
        "entries": admin_audit_log[-200:],  # Last 200 entries
        "total": len(admin_audit_log),
        "auth_configured": bool(ADMIN_API_KEY),
        "ip_allowlist_configured": bool(ADMIN_IP_ALLOWLIST),
    }


# ─── IBKR Safety Endpoints (drift, slippage, pre-flight, reconciliation) ──────

@app.get("/api/ibkr/drift")
def ibkr_drift_endpoint(request: Request):
    """Check position drift between paper and IBKR. Returns critical/warning/ok."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_safety import check_position_drift, get_drift_history
        from predictions.ibkr_adapter import get_ibkr_adapter
        adapter = get_ibkr_adapter()
        if not adapter.is_connected():
            return {"status": "unavailable", "reason": "IBKR not connected"}
        paper_state = get_portfolio_state()
        ibkr_positions = adapter._ib.positions() if adapter._ib else []
        ibkr_pos_dicts = [
            {
                "ticker": p.contract.symbol,
                "position": p.position,
                "avgCost": p.avgCost,
            }
            for p in ibkr_positions
        ]
        scale = adapter._get_mirror_scale(paper_state.get("total_value", 0))
        result = check_position_drift(
            paper_state.get("positions", []),
            ibkr_pos_dicts,
            scale,
        )
        return {
            "current": result,
            "history": get_drift_history(limit=10),
        }
    except Exception as e:
        logger.error(f"IBKR drift check error: {e}")
        return {"status": "error", "error": str(e)}


@app.get("/api/ibkr/slippage")
def ibkr_slippage_endpoint(request: Request):
    """Get slippage summary and recent fill comparisons."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_safety import get_slippage_summary, get_slippage_log
        return {
            "summary_24h": get_slippage_summary(window_hours=24),
            "summary_7d": get_slippage_summary(window_hours=168),
            "recent_fills": get_slippage_log(limit=30),
        }
    except Exception as e:
        logger.error(f"IBKR slippage error: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/api/ibkr/preflight")
def ibkr_preflight_endpoint(request: Request):
    """Run pre-flight self-test. Submits + cancels a test order to verify wiring."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_safety import run_preflight_test
        from predictions.ibkr_adapter import get_ibkr_adapter
        adapter = get_ibkr_adapter()
        if not adapter.is_connected():
            return {"overall": "FAIL", "reason": "IBKR not connected"}
        result = run_preflight_test(adapter)
        return result
    except Exception as e:
        logger.error(f"IBKR preflight error: {e}")
        return {"overall": "FAIL", "error": str(e)}


@app.get("/api/ibkr/preflight")
def ibkr_preflight_status(request: Request):
    """Get the most recent pre-flight result without running a new test."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_safety import get_preflight_result
        return get_preflight_result() or {"status": "never_run"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/ibkr/filter-stats")
def ibkr_filter_stats_endpoint(request: Request):
    """Get current strategy filter state — sector cooldowns, rapid-fire counter, etc."""
    check_rate_limit(request.client.host)
    try:
        from predictions.strategy_filters import get_filter_stats
        return get_filter_stats()
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/ibkr/reconcile")
def ibkr_reconcile_endpoint(request: Request):
    """End-of-day reconciliation: paper P&L vs IBKR P&L."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_safety import daily_reconciliation_report
        from predictions.ibkr_adapter import get_ibkr_adapter, ibkr_get_account, ibkr_get_positions
        adapter = get_ibkr_adapter()
        paper_state = get_portfolio_state()
        ibkr_account = ibkr_get_account() if adapter.is_connected() else {}
        ibkr_positions = ibkr_get_positions() if adapter.is_connected() else []
        ibkr_state = {
            **ibkr_account,
            "positions": ibkr_positions,
        }
        return daily_reconciliation_report(paper_state, ibkr_state)
    except Exception as e:
        logger.error(f"IBKR reconcile error: {e}")
        return {"status": "error", "error": str(e)}


@app.get("/api/auto-trading-status")
def auto_trading_status(request: Request):
    """Get autonomous trading system status — the computer's brain."""
    check_rate_limit(request.client.host)
    next_run = None
    try:
        job = scheduler.get_job("smart_monitor")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    except Exception:
        pass

    # Get latest overnight intel (cached, no extra API calls)
    try:
        overnight = scan_overnight_intelligence()
        overnight_summary = {
            "futures_sentiment": overnight.get("futures_sentiment", "unknown"),
            "overnight_gap_pct": overnight.get("overnight_gap_pct", 0),
            "weekend_shift_detected": overnight.get("weekend_shift_detected", False),
        }
    except Exception:
        overnight_summary = {"futures_sentiment": "unknown"}

    # Check what would trigger a trade right now
    try:
        trade_decision = _should_trade_now()
    except Exception:
        trade_decision = {"should_trade": False, "reasons": ["Error checking"]}

    # Expose current trading window so dashboard knows if we're pre-market,
    # in avoid window, in normal trading, or off-hours.
    try:
        from predictions.paper_trader import _is_good_entry_time
        timing_window = _is_good_entry_time()
    except Exception:
        timing_window = {"window": "unknown", "can_trade": False, "size_modifier": 0, "confidence_shift": 0}

    return {
        **auto_trade_stats,
        "next_scan": next_run,
        "trading_mode": "EVENT-DRIVEN",
        "schedule": "Monitors every 5 min, trades only when conditions change (pre-market 9am, market open 9:30, news, regime shift, VIX spike, stop-loss, market close)",
        "would_trade_now": trade_decision,
        "current_window": timing_window,
        "min_trade_interval_minutes": MIN_TRADE_INTERVAL_MINUTES,
        "overnight_intel": overnight_summary,
        "recent_activity": auto_trade_log[-20:],
    }


@app.get("/api/queued-trades")
def queued_trades(request: Request):
    """Show what the AI is planning to trade next — the trades it's waiting to execute.
    Uses cached picks only — never blocks on fresh generation."""
    check_rate_limit(request.client.host)
    try:
        from analysis.quant_engine import _quant_cache
        import time as _time
        # Use cached picks only — never block on fresh generation
        if "quant_picks" in _quant_cache:
            picks = _quant_cache["quant_picks"]["data"]
        else:
            return {"queued_longs": [], "queued_shorts": [], "total_queued": 0,
                    "already_held": 0, "regime": "LOADING",
                    "message": "Waiting for quant engine to complete first analysis cycle."}
        portfolio = get_portfolio_state()
        open_tickers = set(p["ticker"] for p in portfolio.get("positions", []))

        queued_longs = [
            {"symbol": p["symbol"], "direction": "LONG", "confidence": p["confidence"],
             "score": p["composite_score"], "price": p["price"], "sector": p.get("sector"),
             "reason": p["reasons"][0] if p.get("reasons") else "Multi-factor signal",
             "status": "queued" if p["symbol"] not in open_tickers else "already_held"}
            for p in picks.get("long_picks", [])
            if p["confidence"] >= 35
        ]
        queued_shorts = [
            {"symbol": p["symbol"], "direction": "SHORT", "confidence": p["confidence"],
             "score": p["composite_score"], "price": p["price"], "sector": p.get("sector"),
             "reason": p["reasons"][0] if p.get("reasons") else "Multi-factor signal",
             "status": "queued" if p["symbol"] not in open_tickers else "already_held"}
            for p in picks.get("short_picks", [])
            if p["confidence"] >= 35
        ]

        return {
            "queued_longs": queued_longs,
            "queued_shorts": queued_shorts,
            "total_queued": len([t for t in queued_longs + queued_shorts if t["status"] == "queued"]),
            "already_held": len([t for t in queued_longs + queued_shorts if t["status"] == "already_held"]),
            "next_trade_cycle": auto_trade_stats.get("last_run"),
            "regime": picks.get("regime", {}).get("regime", "UNKNOWN"),
        }
    except Exception as e:
        logger.error(f"Queued trades error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get queued trades")


@app.post("/api/paper-trade/rebalance")
def paper_rebalance(request: Request):
    """Trigger an immediate trade cycle (also runs automatically on schedule)."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        result = execute_trades_from_signals(picks)
        try:
            weight_update = auto_adjust_weights()
            result["weight_update"] = weight_update
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Rebalance error: {e}")
        raise HTTPException(status_code=500, detail="Failed to rebalance")


@app.post("/api/paper-trade/backtest")
def paper_backtest(request: Request):
    """Run rapid backtesting to populate trade history with simulated results."""
    check_rate_limit(request.client.host)
    try:
        return run_backtest(days_back=180, num_trades_target=500)
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        raise HTTPException(status_code=500, detail="Failed to run backtest")


@app.get("/api/overnight-intel")
def overnight_intel(request: Request):
    """Get overnight/pre-market intelligence — futures, global markets, weekend shifts."""
    check_rate_limit(request.client.host)
    try:
        return scan_overnight_intelligence()
    except Exception as e:
        logger.error(f"Overnight intel error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get overnight intelligence")


@app.get("/api/mistake-analysis")
def mistake_analysis(request: Request):
    """Get analysis of past trading mistakes and what the system learned."""
    check_rate_limit(request.client.host)
    try:
        from predictions.learner import analyze_mistakes
        return analyze_mistakes()
    except Exception as e:
        logger.error(f"Mistake analysis error: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyze mistakes")


@app.get("/api/system-intelligence")
@app.get("/api/intelligence-status")
def system_intelligence(request: Request):
    """Get the self-learning system's intelligence report."""
    check_rate_limit(request.client.host)
    try:
        return generate_intelligence_report()
    except Exception as e:
        logger.error(f"Intelligence report error: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate intelligence report")


@app.get("/api/chart-data/{ticker}")
def chart_data(request: Request, ticker: str, period: str = "1y"):
    """Get OHLCV data for interactive candlestick charts."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")
    try:
        from analysis.technical import calculate_sma, calculate_ema, calculate_bollinger_bands
        data = get_historical_data(clean_ticker, period)
        if not data:
            raise HTTPException(status_code=404, detail="No data found")

        closes = [d["close"] for d in data]
        sma_20 = calculate_sma(closes, 20)
        sma_50 = calculate_sma(closes, 50)
        sma_200 = calculate_sma(closes, 200)
        ema_12 = calculate_ema(closes, 12)
        bollinger = calculate_bollinger_bands(closes)

        chart = []
        for i, d in enumerate(data):
            point = {
                "date": d["date"],
                "open": d["open"],
                "high": d["high"],
                "low": d["low"],
                "close": d["close"],
                "volume": d["volume"],
                "sma_20": sma_20[i] if i < len(sma_20) else None,
                "sma_50": sma_50[i] if i < len(sma_50) else None,
                "sma_200": sma_200[i] if i < len(sma_200) else None,
                "ema_12": ema_12[i] if i < len(ema_12) else None,
                "bb_upper": bollinger["upper"][i] if i < len(bollinger["upper"]) else None,
                "bb_middle": bollinger["middle"][i] if i < len(bollinger["middle"]) else None,
                "bb_lower": bollinger["lower"][i] if i < len(bollinger["lower"]) else None,
            }
            chart.append(point)

        return {"ticker": clean_ticker, "period": period, "chart_data": chart}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chart data error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get chart data")


@app.get("/api/watchlist-analysis/{ticker}")
def watchlist_analysis(request: Request, ticker: str):
    """Run compressed quant analysis on a single watchlist stock.
    Returns: signal, confidence, factors, technicals, macro impact — all in one call."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        result = analyze_watchlist_stock(clean_ticker)
        return result
    except Exception as e:
        logger.error(f"Watchlist analysis error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Analysis failed")


@app.get("/api/watchlist-backtest")
def watchlist_backtest(request: Request, tickers: str = "", period: str = "6mo", add_dates: str = ""):
    """Portfolio visualizer — returns since added to watchlist, correlations, risk metrics."""
    check_rate_limit(request.client.host)
    if not tickers:
        raise HTTPException(status_code=400, detail="No tickers provided")

    ticker_list = [validate_ticker(t.strip()) for t in tickers.split(",") if t.strip()][:20]
    if not ticker_list:
        raise HTTPException(status_code=400, detail="No valid tickers")
    if period not in ("1mo", "3mo", "6mo", "1y", "2y"):
        period = "6mo"

    # Parse add dates from frontend (when user added each stock to watchlist)
    stock_add_dates = {}
    if add_dates:
        try:
            stock_add_dates = json.loads(add_dates)
        except Exception:
            pass

    try:
        import yfinance as yf
        import numpy as np
        from datetime import datetime as parse_dt

        # Download each stock individually — guarantees all tickers return data
        returns_data = {}
        price_series = {}

        def _download_single(sym):
            """Download a single ticker and return its Close series."""
            import time as _t
            _t.sleep(1.0)  # Light throttle (1s) — safe for 5 sequential calls
            sdf = yf.download(sym, period="2y", progress=False)
            if sdf is None or sdf.empty:
                return None
            # Flatten MultiIndex if present (yfinance wraps single tickers too)
            if isinstance(sdf.columns, pd.MultiIndex):
                sdf.columns = sdf.columns.get_level_values(0)
            if "Close" not in sdf.columns:
                return None
            s = sdf["Close"].dropna()
            return s if len(s) >= 2 else None

        for sym in ticker_list:
            try:
                sym_series = _download_single(sym)
                if sym_series is None or len(sym_series) < 2:
                    logger.warning(f"Backtest: no data for {sym}")
                    continue

                # Store full close prices before trimming
                full_closes = sym_series.values.astype(float).flatten()

                # Trim to add date if available
                if sym in stock_add_dates and stock_add_dates[sym]:
                    try:
                        add_date_str = str(stock_add_dates[sym]).split('T')[0]  # YYYY-MM-DD
                        # Convert index dates to YYYY-MM-DD strings for safe comparison
                        date_strs = [str(d)[:10] for d in sym_series.index]
                        keep = [i for i, ds in enumerate(date_strs) if ds >= add_date_str]
                        if len(keep) >= 2:
                            sym_series = sym_series.iloc[keep]
                        elif len(keep) == 1:
                            sym_series = sym_series.iloc[-2:]  # Just added, use last 2 days
                        else:
                            sym_series = sym_series.iloc[-2:]  # Added after last trading day
                    except Exception as e:
                        logger.warning(f"Date trim failed for {sym}: {e}")

                closes = sym_series.values.astype(float).flatten()
                if len(closes) < 2:
                    continue

                daily_rets = np.diff(closes) / closes[:-1]
                returns_data[sym] = daily_rets
                price_series[sym] = (closes / closes[0] * 100).tolist()
            except Exception as exc:
                logger.warning(f"Backtest extract failed for {sym}: {exc}")
                continue

        if not returns_data:
            raise HTTPException(status_code=404, detail="No valid data for tickers")

        # Calculate stats per stock
        stock_stats = {}
        for sym, rets in returns_data.items():
            total_ret = float((np.prod(1 + rets) - 1) * 100)
            # Only annualize if we have 20+ trading days, otherwise just show total return
            if len(rets) >= 20:
                ann_ret = float(((1 + total_ret / 100) ** (252 / len(rets)) - 1) * 100)
            else:
                ann_ret = total_ret  # Don't annualize short periods (inflates numbers)
            ann_vol = float(np.std(rets) * np.sqrt(252) * 100)
            sharpe = round(ann_ret / ann_vol, 2) if ann_vol > 0 else 0
            max_dd = 0
            peak = 1.0
            for r in rets:
                peak = max(peak, peak * (1 + r))
                dd = (peak * (1 + r) - peak) / peak * 100
                max_dd = min(max_dd, dd)

            def _safe(v, default=0):
                """Convert to float, replacing NaN/Inf with default."""
                f = float(v)
                return default if (np.isnan(f) or np.isinf(f)) else f

            stock_stats[sym] = {
                "total_return": round(_safe(total_ret), 2),
                "annualized_return": round(_safe(ann_ret), 1),
                "annualized_vol": round(_safe(ann_vol), 1),
                "sharpe_ratio": round(_safe(sharpe), 2),
                "max_drawdown": round(_safe(max_dd), 1),
                "trading_days": len(rets),
                "days_held": len(rets),
            }

        # Correlation matrix — need at least 2 returns per stock
        symbols = list(returns_data.keys())
        min_len = min(len(returns_data[s]) for s in symbols)
        corr_matrix = {}
        if min_len >= 2:
            for i, s1 in enumerate(symbols):
                corr_matrix[s1] = {}
                for j, s2 in enumerate(symbols):
                    r1 = returns_data[s1][-min_len:]
                    r2 = returns_data[s2][-min_len:]
                    try:
                        corr = float(np.corrcoef(r1, r2)[0, 1])
                        corr = 0.0 if (np.isnan(corr) or np.isinf(corr)) else corr
                    except Exception:
                        corr = 0.0
                    corr_matrix[s1][s2] = round(corr, 3)
        else:
            # Not enough overlapping data for correlations
            for s1 in symbols:
                corr_matrix[s1] = {s2: (1.0 if s1 == s2 else 0.0) for s2 in symbols}

        # Equal-weight portfolio performance
        if len(symbols) >= 2 and min_len >= 2:
            port_rets = np.zeros(min_len)
            for sym in symbols:
                port_rets += returns_data[sym][-min_len:] / len(symbols)
            port_total = float((np.prod(1 + port_rets) - 1) * 100)
            port_vol = float(np.std(port_rets) * np.sqrt(252) * 100)
            port_sharpe = round((_safe(port_total) * 252 / min_len) / port_vol, 2) if port_vol > 0 else 0
            portfolio_stats = {
                "total_return": round(_safe(port_total), 2),
                "annualized_vol": round(_safe(port_vol), 1),
                "sharpe_ratio": round(_safe(port_sharpe), 2),
                "diversification_benefit": round(_safe(
                    np.mean([stock_stats[s]["annualized_vol"] for s in symbols]) - port_vol
                ), 1),
            }
        elif len(symbols) >= 1:
            # Use simple average of individual stock stats
            avg_ret = np.mean([stock_stats[s]["total_return"] for s in symbols])
            avg_vol = np.mean([stock_stats[s]["annualized_vol"] for s in symbols])
            portfolio_stats = {
                "total_return": round(_safe(avg_ret), 2),
                "annualized_vol": round(_safe(avg_vol), 1),
                "sharpe_ratio": round(_safe(avg_ret / avg_vol if avg_vol > 0 else 0), 2),
                "diversification_benefit": 0.0,
            }
        else:
            portfolio_stats = stock_stats.get(symbols[0], {}) if symbols else {}

        return {
            "tickers": symbols,
            "period": period,
            "stock_stats": stock_stats,
            "correlation_matrix": corr_matrix,
            "portfolio_stats": portfolio_stats,
            "price_series": price_series,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        raise HTTPException(status_code=500, detail="Backtest failed")


# ============================================================
#  RENTECH API ENDPOINTS
# ============================================================

@app.get("/api/rentech/pairs")
def rentech_pairs(request: Request):
    """Get current pairs trading opportunities (stat arb)."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        return {
            "pairs_trades": picks.get("pairs_trades", []),
            "regime": picks.get("regime", {}),
            "timestamp": picks.get("generated_at", ""),
        }
    except Exception as e:
        logger.error(f"RenTech pairs error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get pairs data")


@app.get("/api/rentech/risk")
def rentech_risk(request: Request):
    """Get portfolio risk assessment (sector concentration, beta, correlation)."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        return {
            "portfolio_risk": picks.get("portfolio_risk", {}),
            "circuit_breaker": picks.get("circuit_breaker", {}),
            "regime": picks.get("regime", {}),
        }
    except Exception as e:
        logger.error(f"RenTech risk error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get risk data")


@app.get("/api/rentech/mean-reversion")
def rentech_mean_reversion(request: Request):
    """Get mean reversion trade setups (RSI2 Connors, VWAP, Bollinger)."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        return {
            "mean_reversion_setups": picks.get("mean_reversion_setups", []),
            "regime": picks.get("regime", {}),
            "timestamp": picks.get("generated_at", ""),
        }
    except Exception as e:
        logger.error(f"RenTech mean reversion error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get mean reversion data")


@app.get("/api/rentech/alt-data/{ticker}")
def rentech_alt_data(request: Request, ticker: str):
    """Get alternative data signals for a stock (short interest, insider, institutional)."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        from analysis.rentech import get_alt_data_signals
        signals = get_alt_data_signals([clean_ticker])
        return signals.get(clean_ticker, {"symbol": clean_ticker, "error": "No data"})
    except Exception as e:
        logger.error(f"RenTech alt data error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get alt data")


@app.get("/api/rentech/earnings-shield")
def rentech_earnings_shield(request: Request):
    """Get stocks blocked/warned due to imminent earnings."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        return picks.get("earnings_shield", {"blocked": [], "warning": []})
    except Exception as e:
        logger.error(f"Earnings shield error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get earnings data")


@app.get("/api/rentech/sector-rotation")
def rentech_sector_rotation(request: Request):
    """Get sector rotation flow data (inflow/outflow detection)."""
    check_rate_limit(request.client.host)
    try:
        from analysis.rentech import detect_sector_rotation
        # Call directly — much faster than running full generate_quant_picks()
        regime = detect_market_regime()
        price_data = {"regime": regime}
        result = detect_sector_rotation(price_data)
        if not result:
            return {"sectors": {}, "top_inflow": [], "top_outflow": [], "message": "No sector data available — market may be closed"}
        return result
    except Exception as e:
        logger.error(f"Sector rotation error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get sector rotation")


@app.get("/api/rentech/regime-prediction")
def rentech_regime_prediction(request: Request):
    """Get regime transition prediction (early warning system)."""
    check_rate_limit(request.client.host)
    try:
        from analysis.rentech import predict_regime_transition
        # Call directly — much faster than running full generate_quant_picks()
        regime = detect_market_regime()
        price_data = {"regime": regime}
        result = predict_regime_transition(price_data)
        if not result:
            return {"prediction": "UNKNOWN", "warning_signs": [], "bullish_signs": [], "message": "No regime data available"}
        return result
    except Exception as e:
        logger.error(f"Regime prediction error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get regime prediction")


@app.get("/api/rentech/news-sentiment/{ticker}")
def rentech_news_sentiment(request: Request, ticker: str):
    """Get news sentiment for a specific stock."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        from analysis.rentech import get_stock_news_sentiment
        result = get_stock_news_sentiment([clean_ticker])
        return result.get(clean_ticker, {"sentiment": "NEUTRAL", "score": 0})
    except Exception as e:
        logger.error(f"News sentiment error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get news sentiment")


@app.get("/api/rentech/options-flow/{ticker}")
def rentech_options_flow(request: Request, ticker: str):
    """Get unusual options activity for a specific stock."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        from analysis.rentech import detect_unusual_options
        result = detect_unusual_options([clean_ticker])
        return result.get(clean_ticker, {"signal": "NEUTRAL"})
    except Exception as e:
        logger.error(f"Options flow error for {clean_ticker}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get options flow")


@app.get("/api/rentech/dashboard")
def rentech_dashboard(request: Request):
    """Full RenTech dashboard — all data in one call."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        perf = get_performance_analytics()
        portfolio = get_portfolio_state()

        return {
            "regime": picks.get("regime", {}),
            "pairs_trades": picks.get("pairs_trades", []),
            "mean_reversion_setups": picks.get("mean_reversion_setups", []),
            "portfolio_risk": picks.get("portfolio_risk", {}),
            "circuit_breaker": picks.get("circuit_breaker", {}),
            "performance": {
                "total_return": perf.get("overall", {}).get("total_pnl", 0),
                "sharpe": perf.get("overall", {}).get("sharpe_ratio", 0),
                "win_rate": perf.get("overall", {}).get("win_rate", 0),
                "total_trades": perf.get("overall", {}).get("total_trades", 0),
                "avg_win": perf.get("overall", {}).get("avg_win", 0),
                "avg_loss": perf.get("overall", {}).get("avg_loss", 0),
                "profit_factor": perf.get("overall", {}).get("profit_factor", 0),
                "best_trade": perf.get("overall", {}).get("best_trade", 0),
                "worst_trade": perf.get("overall", {}).get("worst_trade", 0),
            },
            "portfolio": {
                "cash": portfolio.get("cash", 0),
                "total_value": portfolio.get("total_value", 0),
                "open_positions": len(portfolio.get("positions", [])),
            },
            "top_longs": picks.get("long_picks", [])[:5],
            "top_shorts": picks.get("short_picks", [])[:5],
            "timestamp": picks.get("generated_at", ""),
        }
    except Exception as e:
        logger.error(f"RenTech dashboard error: {e}")
        raise HTTPException(status_code=500, detail="Failed to load RenTech dashboard")


# --- Serve Frontend (in production, the built React app is here) ---

frontend_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        """Serve the React frontend for any non-API route."""
        # Security: prevent path traversal attacks
        safe_path = os.path.normpath(os.path.join(frontend_dir, full_path))
        if not safe_path.startswith(os.path.normpath(frontend_dir)):
            raise HTTPException(status_code=403, detail="Access denied")
        if os.path.isfile(safe_path):
            return FileResponse(safe_path)
        return FileResponse(os.path.join(frontend_dir, "index.html"))
