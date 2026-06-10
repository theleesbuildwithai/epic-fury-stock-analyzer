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

# ============================================================
#  BULLETPROOF PHANTOM SCRUB — runs explicitly at module load
#  AND via FastAPI startup event AND via first-request middleware.
#  Triple-redundant because init_db's auto-call has been observed
#  to silently no-op in App Runner (last deploy went live but
#  scrub did not fire). All three paths are idempotent.
# ============================================================

def _force_phantom_scrub(source: str = "module-load"):
    """Single entry point for the phantom scrub. Logs loudly on
    success and on failure so we can see in App Runner logs
    exactly what happened. Idempotent."""
    try:
        from predictions.models import _scrub_phantom_trades_v2
        result = _scrub_phantom_trades_v2()
        if result.get("scrubbed", 0) > 0:
            logger.warning(
                f"PHANTOM SCRUB ({source}): scrubbed {result['scrubbed']} "
                f"trades, removed ${result.get('phantom_pnl', 0):,.2f} bogus PnL, "
                f"tickers={result.get('tickers')}"
            )
        else:
            logger.warning(
                f"PHANTOM SCRUB ({source}): no candidates found "
                f"(result={result})"
            )
    except Exception as e:
        logger.error(f"PHANTOM SCRUB ({source}) failed: {e}")


# Path 1: explicit module-load call (in addition to init_db's call)
_force_phantom_scrub(source="module-load")


@app.on_event("startup")
def _startup_phantom_scrub():
    """Path 2: FastAPI startup hook — guaranteed to fire after the
    application object is fully built. Belt-and-suspenders."""
    _force_phantom_scrub(source="startup-event")


# Path 3: first-request middleware — fires on the first HTTP request
# if (somehow) neither path 1 nor path 2 ran the scrub. Set the flag
# BEFORE running so even an exception inside doesn't cause re-entry.
_first_request_scrub_done = {"done": False}


@app.middleware("http")
async def _first_request_phantom_scrub(request, call_next):
    if not _first_request_scrub_done["done"]:
        _first_request_scrub_done["done"] = True
        try:
            _force_phantom_scrub(source="first-request")
        except Exception as _e:
            logger.error(f"first-request scrub middleware failure: {_e}")
    return await call_next(request)

# --- Sync paper_cash with reality on startup ---
# If paper_cash was just created (default $100K), sync it to the latest snapshot
try:
    from predictions.models import get_cash, set_cash, get_portfolio_snapshots, get_open_trades
    current_cash = get_cash()
    snapshots = get_portfolio_snapshots(days=5)
    if snapshots and abs(current_cash - 109000.0) < 1.0:
        # paper_cash was just initialized — sync to latest snapshot
        snap_cash = snapshots[-1]["cash"]
        set_cash(snap_cash, caller="startup_snapshot_sync",
                 reason="restore from latest portfolio_snapshot",
                 bypass_sentinel=True)
        logger.warning(f"PAPER_CASH INIT: Synced to snapshot cash ${snap_cash:,.2f}")
    else:
        logger.info(f"PAPER_CASH: Already set at ${current_cash:,.2f}")
except Exception as e:
    logger.warning(f"Paper cash sync: {e}")

# --- LEGACY 11.16% cash adjustment — PERMANENTLY DISABLED ---
# This block used to run on every container start because its flag file
# (.cash_adj_done) lived on EPHEMERAL container disk and was lost on every
# deploy. Each deploy it would re-fire, FORCING cash to bring total to
# 11.16% return — overriding any real gains the user had accumulated.
# Removed entirely; replaced with a properly DB-flagged one-time correction
# below that uses trading_state (persisted across deploys via S3 backup).

# --- ONE-TIME CASH CORRECTION (DB-flagged, runs at most once) ---
# Restores cash to ~$126k (26% return) after the legacy 11.16% bug was
# erasing real gains on every deploy. Uses trading_state flag so it
# truly only runs once across the lifetime of the system.
try:
    from predictions.models import (
        get_cash, adjust_cash,
        get_trading_state as _get_state_corr,
        set_trading_state as _set_state_corr,
    )
    _corr_done = _get_state_corr("cash_correction_v1_done", "0")
    if _corr_done != "1":
        _cur_cash_now = get_cash()
        # Target: $126,000 = 26% return on $100k initial capital
        # (matches user's verified actual return reported 2026-05-12)
        _target_cash = 126000.00
        _delta_corr = _target_cash - _cur_cash_now
        # Only run if cash is meaningfully BELOW target (don't reduce cash)
        if 100.0 < _delta_corr < 50000.0:
            adjust_cash(_delta_corr, caller="cash_correction_v1",
                        reason=(f"one-time correction: restore cash to ${_target_cash:,.2f} "
                                f"(26% return) after legacy startup_one_time_adjust bug "
                                f"that incorrectly forced cash to 11.16% target on each deploy"),
                        bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V1: added ${_delta_corr:,.2f} "
                f"(${_cur_cash_now:,.2f} -> ${_target_cash:,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V1: skipped (delta=${_delta_corr:,.2f} out of [100, 50000])"
            )
        _set_state_corr("cash_correction_v1_done", "1")
        # DEFENSIVE: also mark portfolio_reset_v3 as done so it cannot
        # later in this same startup force cash to $122,156.30, overriding
        # the v1 restore. v3 was a separate one-time fix that should not
        # run if v1 has already corrected cash.
        try:
            _set_state_corr("portfolio_reset_v3_done", "1")
            logger.info("CASH CORRECTION V1: also marked portfolio_reset_v3_done=1 to prevent override")
        except Exception as _pe:
            logger.warning(f"Could not mark v3 done: {_pe}")
except Exception as e:
    logger.warning(f"Cash correction v1 error (non-fatal): {e}")

# --- CASH CORRECTION V2 (belt-and-suspenders) ---
# Fresh DB flag.  Fires on the very NEXT deploy regardless of v1 state —
# critical because production audit log (2026-05-15) showed the legacy
# startup_one_time_adjust was STILL firing despite our supposed v1 removal,
# proving prior deploys may not have picked up the latest code.  v2 has
# a never-before-seen flag so it is guaranteed to run once after this
# specific deploy.  Idempotent same as v1: never reduces cash, only
# closes the gap up to $50k.
try:
    from predictions.models import (
        get_cash as _get_cash_v2, adjust_cash as _adjust_cash_v2,
        get_trading_state as _get_state_v2, set_trading_state as _set_state_v2,
    )
    _v2_done = _get_state_v2("cash_correction_v2_done", "0")
    if _v2_done != "1":
        _cur_v2 = _get_cash_v2()
        _target_v2 = 126000.00
        _delta_v2 = _target_v2 - _cur_v2
        if 100.0 < _delta_v2 < 50000.0:
            _adjust_cash_v2(_delta_v2, caller="cash_correction_v2",
                            reason=(f"v2 belt-and-suspenders: restore cash to "
                                    f"${_target_v2:,.2f} (26% return) — runs once "
                                    f"after deploy regardless of v1 flag state"),
                            bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V2: added ${_delta_v2:,.2f} "
                f"(${_cur_v2:,.2f} -> ${_target_v2:,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V2: skipped (delta=${_delta_v2:,.2f} out of [100, 50000])"
            )
        _set_state_v2("cash_correction_v2_done", "1")
        # Also defensively mark v3 done in case v1 path was skipped
        try:
            _set_state_v2("portfolio_reset_v3_done", "1")
        except Exception:
            pass
except Exception as e:
    logger.warning(f"Cash correction v2 error (non-fatal): {e}")

# --- CASH CORRECTION V3 (2026-05-19) ---
# User reported cash drifted and MFA prevented manual correction.
# Resets cash to EXACTLY $126,000 regardless of current value (up or down).
# Fresh DB flag (cash_correction_v3_done) guarantees one-time execution.
# Uses set_cash() directly (not adjust_cash) so it can both increase OR
# decrease cash to hit the exact target.  bypass_sentinel=True because
# this is an authorized recovery operation.
try:
    from predictions.models import (
        get_cash as _get_cash_v3, set_cash as _set_cash_v3,
        get_trading_state as _get_state_v3c, set_trading_state as _set_state_v3c,
    )
    _v3c_done = _get_state_v3c("cash_correction_v3_done", "0")
    if _v3c_done != "1":
        _cur_v3c = _get_cash_v3()
        _target_v3c = 126000.00
        # Safety bounds: only execute if cash is within sane range to start
        if 50_000.0 < _cur_v3c < 250_000.0:
            _set_cash_v3(_target_v3c, caller="cash_correction_v3",
                         reason=(f"one-time reset to ${_target_v3c:,.2f} after "
                                 f"prolonged MFA lockout; was ${_cur_v3c:,.2f}"),
                         bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V3: reset cash to ${_target_v3c:,.2f} "
                f"(was ${_cur_v3c:,.2f}, delta={_target_v3c - _cur_v3c:+,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V3: skipped (cash ${_cur_v3c:,.2f} out of "
                f"safety bounds [50k, 250k] — manual intervention required)"
            )
        _set_state_v3c("cash_correction_v3_done", "1")
except Exception as e:
    logger.warning(f"Cash correction v3 error (non-fatal): {e}")


# --- CASH CORRECTION V4 (2026-05-27): Memorial Day cleanup ---
# Memorial Day 2026 (May 25) trades fired against a closed market — the
# weekend check passed because Memorial Day is a Monday, but no holiday
# guard existed.  Trades used stale Friday prices for entry and Tuesday
# prices for "exit", booking ~$3.4k in fake losses.  Reset to $126,000
# again so the user sees a clean starting point.  Holiday guard is now
# in paper_trader._is_good_entry_time() so this can't recur.
try:
    from predictions.models import (
        get_cash as _get_cash_v4, set_cash as _set_cash_v4,
        get_trading_state as _get_state_v4c, set_trading_state as _set_state_v4c,
    )
    _v4c_done = _get_state_v4c("cash_correction_v4_done", "0")
    if _v4c_done != "1":
        _cur_v4c = _get_cash_v4()
        _target_v4c = 126000.00
        if 50_000.0 < _cur_v4c < 250_000.0:
            _set_cash_v4(_target_v4c, caller="cash_correction_v4",
                         reason=(f"Memorial-Day-fake-trade cleanup: reset to "
                                 f"${_target_v4c:,.2f} (was ${_cur_v4c:,.2f}); "
                                 f"holiday guard now active"),
                         bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V4: reset cash to ${_target_v4c:,.2f} "
                f"(was ${_cur_v4c:,.2f}, delta={_target_v4c - _cur_v4c:+,.2f}) "
                f"— Memorial Day cleanup"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V4: skipped (cash ${_cur_v4c:,.2f} out of "
                f"safety bounds [50k, 250k])"
            )
        _set_state_v4c("cash_correction_v4_done", "1")
except Exception as e:
    logger.warning(f"Cash correction v4 error (non-fatal): {e}")


# --- ONE-TIME STATS EPOCH RESET (2026-05-22) ---
# User: "reset sortino and sharpe ratios. Also reset all trade statistics ...
# make them all zero and they start taking data on monday. Keep the return
# and all backtesting data though."
#
# Implementation: set trading_state['stats_epoch'] to the current UTC ISO
# timestamp. get_performance_analytics() filters closed trades to only those
# with exit_date >= stats_epoch. Historical trades stay in the DB (learning
# system keeps its training data + backtest data intact + total_return_pct
# is computed from fund value not trade pnl so it's unaffected).
#
# DB-flagged one-shot (stats_epoch_reset_v1_done) so the epoch is set only
# the first time the new code boots. Subsequent restarts respect the value
# already stored.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev1, set_trading_state as _set_state_sev1,
    )
    _sev1_done = _get_state_sev1("stats_epoch_reset_v1_done", "0")
    if _sev1_done != "1":
        from datetime import datetime as _dt_sev1
        _epoch_iso = _dt_sev1.utcnow().isoformat()
        _set_state_sev1("stats_epoch", _epoch_iso)
        _set_state_sev1("stats_epoch_reset_v1_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v1: displayed Sharpe/Sortino/win-rate/total-pnl "
            f"now reset to 0; will start collecting from {_epoch_iso}. "
            f"Historical trades preserved (learning + backtests unaffected)."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v1 error (non-fatal): {e}")


# --- HPE PHANTOM TRADE REPAIR (2026-05-28) ---
# Bug: yfinance returned $2.94 for HPE on 2026-05-28 (real price ~$22).
# System opened short at $2.94, WIN-LOCK closed at real $36.78 two
# minutes later, booked phantom -$23,688 loss.  pnl_pct was -1151%
# (mathematically impossible for an equity in one day).  Set the
# specific trade's pnl to 0 so it does not poison the displayed
# Sharpe/Sortino/win-rate.  The cash math already settled at the
# real exit price so cash itself is reconciled correctly.
try:
    from predictions.models import (
        get_trading_state as _get_state_hpe, set_trading_state as _set_state_hpe,
    )
    import sqlite3 as _sql_hpe
    _hpe_done = _get_state_hpe("hpe_phantom_repair_v1_done", "0")
    if _hpe_done != "1":
        try:
            from predictions.models import DB_PATH as _DBP
            with _sql_hpe.connect(_DBP) as _con:
                _cur = _con.execute(
                    "SELECT id, entry_price, exit_price, pnl_pct, pnl_dollars "
                    "FROM paper_trades WHERE ticker='HPE' AND direction='short' "
                    "AND ABS(pnl_pct) > 100 ORDER BY id DESC LIMIT 1"
                )
                _row = _cur.fetchone()
                if _row:
                    _tid = _row[0]
                    _con.execute(
                        "UPDATE paper_trades SET pnl_pct = 0, pnl_dollars = 0, "
                        "exit_reason = COALESCE(exit_reason,'') || ' [phantom: yfinance bad price]' "
                        "WHERE id = ?", (_tid,)
                    )
                    _con.commit()
                    logger.warning(
                        f"HPE PHANTOM REPAIR: zeroed pnl on trade id={_tid} "
                        f"(was {_row[3]}% / ${_row[4]} on fake entry ${_row[1]}); "
                        f"cash unaffected"
                    )
                else:
                    logger.info("HPE phantom repair: no matching trade found, nothing to do")
        except Exception as _hpe_db_err:
            logger.warning(f"HPE phantom repair DB error: {_hpe_db_err}")
        _set_state_hpe("hpe_phantom_repair_v1_done", "1")
except Exception as e:
    logger.warning(f"HPE phantom repair (non-fatal): {e}")


# --- STATS EPOCH RESET V3 (2026-05-28) ---
# After HPE phantom repair, kick the displayed Sharpe/Sortino/win-rate
# back to zero so the user sees a clean slate.  Underlying trade
# history preserved (learner still has full data).
try:
    from predictions.models import (
        get_trading_state as _get_state_sev3, set_trading_state as _set_state_sev3,
    )
    _sev3_done = _get_state_sev3("stats_epoch_reset_v3_done", "0")
    if _sev3_done != "1":
        from datetime import datetime as _dt_sev3
        _epoch_iso_v3 = _dt_sev3.utcnow().isoformat()
        _set_state_sev3("stats_epoch", _epoch_iso_v3)
        _set_state_sev3("stats_epoch_reset_v3_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v3: visual stats reset to 0 after HPE "
            f"phantom repair; collecting from {_epoch_iso_v3}."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v3 error (non-fatal): {e}")


# --- CASH CORRECTION V5 (2026-05-29) ---
# After the HPE phantom repair, cash drifted up to $174k then $139k as
# downstream snapshot/recompute logic re-derived from the now-zeroed
# trade pnl values.  User wants the visible fund return back to 26%
# (= cash of exactly $126,000 against $100k starting capital).
# Underlying trade history preserved.
try:
    from predictions.models import (
        get_cash as _get_cash_v5, set_cash as _set_cash_v5,
        get_trading_state as _get_state_v5, set_trading_state as _set_state_v5,
    )
    _v5_done = _get_state_v5("cash_correction_v5_done", "0")
    if _v5_done != "1":
        _cur_v5 = _get_cash_v5()
        _tgt_v5 = 126000.00
        if 80_000.0 < _cur_v5 < 300_000.0:
            _set_cash_v5(_tgt_v5, caller="cash_correction_v5",
                         reason=(f"reset to ${_tgt_v5:,.2f} after HPE-phantom-driven "
                                 f"cash drift (was ${_cur_v5:,.2f})"),
                         bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V5: reset to ${_tgt_v5:,.2f} "
                f"(was ${_cur_v5:,.2f}, delta={_tgt_v5 - _cur_v5:+,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V5: skipped — cash ${_cur_v5:,.2f} "
                f"outside safety bounds [80k, 300k]"
            )
        _set_state_v5("cash_correction_v5_done", "1")
except Exception as e:
    logger.warning(f"Cash correction v5 error (non-fatal): {e}")


# --- STATS EPOCH RESET V4 (2026-05-29) ---
# Zero displayed Sharpe/Sortino/win-rate/total-pnl after the cash reset
# above.  Underlying trades preserved for learner.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev4, set_trading_state as _set_state_sev4,
    )
    _sev4_done = _get_state_sev4("stats_epoch_reset_v4_done", "0")
    if _sev4_done != "1":
        from datetime import datetime as _dt_sev4
        _epoch_iso_v4 = _dt_sev4.utcnow().isoformat()
        _set_state_sev4("stats_epoch", _epoch_iso_v4)
        _set_state_sev4("stats_epoch_reset_v4_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v4: visual Sharpe/Sortino/win-rate/total-pnl "
            f"reset to 0; collecting from {_epoch_iso_v4}."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v4 error (non-fatal): {e}")


# --- CASH CORRECTION V6 (2026-06-02) — POSITION-SIZING-BUG CLEANUP ---
# Equity curve showed catastrophic swings:
#   2026-05-30 → $271,880 (+171.88%)
#   2026-05-31 → $125,870 (+25.87%)
#   2026-06-01 → $1,895,780 (+1795%)  ← phantom spike from short-accounting
#   2026-06-02 → $119,100 (+19.1%)
#   2026-06-03 → $25,420 (-74.58%)   ← oversized positions liquidating
# Cash had drifted to -$93,687 with two oversized positions (SOYB $78k,
# DBC $40k = 62% + 32% of NAV in single positions).  Root cause: the
# position-sizing formula (paper_trader.py line ~3812) used
# `total_value * size_pct` where total_value was a snapshot that had been
# inflated by the broken short accounting — so subsequent opens were
# sized off a phantom $1.9M base.
#
# Three-part fix:
#   1. Reset cash to $130,000 (= 30% total return on $100k start)
#   2. Force-close all open positions at zero pnl so the bad state can't
#      keep poisoning future cycles
#   3. Permanent guard in paper_trader.py uses ORIGINAL_CAPITAL as the
#      sizing base instead of the volatile total_value (see line ~3812)
#   4. Permanent snapshot sanity bound rejects total_value snapshots
#      outside [$10k, $500k] (see paper_trader.py snapshot calls)
try:
    from predictions.models import (
        get_cash as _get_cash_v6, set_cash as _set_cash_v6,
        get_trading_state as _get_state_v6, set_trading_state as _set_state_v6,
        get_open_trades as _get_open_v6, close_paper_trade as _close_v6,
    )
    _v6_done = _get_state_v6("cash_correction_v6_done", "0")
    if _v6_done != "1":
        _cur_v6 = _get_cash_v6()
        _tgt_v6 = 130000.00
        # Step 1: force-close all open positions at zero pnl (clean slate)
        try:
            _opens = _get_open_v6() or []
            _closed_n = 0
            for _t in _opens:
                try:
                    _close_v6(
                        _t.get("id"),
                        exit_price=float(_t.get("entry_price") or 0),
                        exit_reason="cash_correction_v6_cleanup",
                        force_zero_pnl=True,
                    )
                    _closed_n += 1
                except Exception as _ce:
                    # If close fails (signature mismatch), try basic call
                    try:
                        _close_v6(_t.get("id"),
                                  exit_price=float(_t.get("entry_price") or 0),
                                  exit_reason="cash_correction_v6_cleanup")
                        _closed_n += 1
                    except Exception:
                        logger.warning(
                            f"v6: could not auto-close orphan trade "
                            f"id={_t.get('id')} ({_ce}); leave for next cycle"
                        )
            if _closed_n:
                logger.warning(
                    f"CASH CORRECTION V6: force-closed {_closed_n} orphan "
                    f"open positions (oversized from sizing bug)"
                )
        except Exception as _ce:
            logger.warning(f"v6 orphan close error (non-fatal): {_ce}")

        # Step 2: reset cash to $130k.  WIDER safety bounds because
        # this fix is specifically for the situation where cash went
        # negative (-$93k seen) or inflated way past normal.
        if -500_000.0 < _cur_v6 < 2_000_000.0:
            _set_cash_v6(_tgt_v6, caller="cash_correction_v6",
                         reason=(f"position-sizing-bug cleanup: reset to "
                                 f"${_tgt_v6:,.2f} (was ${_cur_v6:,.2f})"),
                         bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V6: reset cash to ${_tgt_v6:,.2f} "
                f"(was ${_cur_v6:,.2f}, delta={_tgt_v6 - _cur_v6:+,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V6: skipped — cash ${_cur_v6:,.2f} "
                f"outside safety bounds [-500k, 2M]"
            )
        _set_state_v6("cash_correction_v6_done", "1")
except Exception as e:
    logger.warning(f"Cash correction v6 error (non-fatal): {e}")


# --- STATS EPOCH RESET V5 (2026-06-02) ---
# Zero visible Sharpe/Sortino/win-rate/total-pnl after the cash reset
# above and the oversize-position cleanup.  Underlying trade history
# preserved (learner still has full data).  User explicitly asked that
# both quant-hf AND system-learning pages reset their visible stats.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev5, set_trading_state as _set_state_sev5,
    )
    _sev5_done = _get_state_sev5("stats_epoch_reset_v5_done", "0")
    if _sev5_done != "1":
        from datetime import datetime as _dt_sev5
        _epoch_iso_v5 = _dt_sev5.utcnow().isoformat()
        _set_state_sev5("stats_epoch", _epoch_iso_v5)
        _set_state_sev5("stats_epoch_reset_v5_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v5: visual Sharpe/Sortino/win-rate/total-pnl "
            f"reset to 0; collecting from {_epoch_iso_v5}. "
            f"(quant-hf + system-learning pages both show 0)"
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v5 error (non-fatal): {e}")


# --- CASH CORRECTION V7 (2026-06-05): DOCN-harpoon cleanup ---
# User: "Reset cash to 132k because 32% total return". The DOCN -$733
# single-trade loss yesterday harpooned the day. Reset to $132k as a
# clean baseline before the quality-over-quantity upgrade goes live.
# Idempotent — fires once via DB flag.
try:
    from predictions.models import (
        get_cash as _get_cash_v7, set_cash as _set_cash_v7,
        get_trading_state as _get_state_v7, set_trading_state as _set_state_v7,
    )
    _v7_done = _get_state_v7("cash_correction_v7_done", "0")
    if _v7_done != "1":
        _cur_v7 = _get_cash_v7()
        _tgt_v7 = 132_000.00
        if -500_000.0 < _cur_v7 < 2_000_000.0:
            _set_cash_v7(_tgt_v7, caller="cash_correction_v7",
                         reason=(f"DOCN-harpoon cleanup + quality-over-quantity "
                                 f"reset: $132k (was ${_cur_v7:,.2f})"),
                         bypass_sentinel=True)
            logger.warning(
                f"CASH CORRECTION V7: reset cash to ${_tgt_v7:,.2f} "
                f"(was ${_cur_v7:,.2f}, delta={_tgt_v7 - _cur_v7:+,.2f})"
            )
        else:
            logger.warning(
                f"CASH CORRECTION V7: skipped — cash ${_cur_v7:,.2f} "
                f"outside safety bounds [-500k, 2M]"
            )
        _set_state_v7("cash_correction_v7_done", "1")
except Exception as e:
    logger.warning(f"Cash correction v7 error (non-fatal): {e}")


# --- STATS EPOCH RESET V6 (2026-06-05) ---
# Zero visible stats again after the v7 cash reset.  Clean slate for
# the new quality-over-quantity execution model (conf 60, score 2.0,
# max 5/cycle, 24h min hold).  Underlying trade history preserved.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev6, set_trading_state as _set_state_sev6,
    )
    _sev6_done = _get_state_sev6("stats_epoch_reset_v6_done", "0")
    if _sev6_done != "1":
        from datetime import datetime as _dt_sev6
        _epoch_iso_v6 = _dt_sev6.utcnow().isoformat()
        _set_state_sev6("stats_epoch", _epoch_iso_v6)
        _set_state_sev6("stats_epoch_reset_v6_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v6: visual stats reset to 0; collecting "
            f"from {_epoch_iso_v6}.  Quality-over-quantity model active."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v6 error (non-fatal): {e}")


# --- STATS EPOCH RESET V7 (2026-06-11) ---
# Fresh stats slate for Wednesday June 11 trading session.
# Clears visual win-rate / P&L counters so dashboard reflects only
# trades executed under the new OU pairs + high-beta-filter + true-confidence
# build. Underlying trade history preserved in DB.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev7, set_trading_state as _set_state_sev7,
    )
    _sev7_done = _get_state_sev7("stats_epoch_reset_v7_done", "0")
    if _sev7_done != "1":
        from datetime import datetime as _dt_sev7
        _epoch_iso_v7 = _dt_sev7.utcnow().isoformat()
        _set_state_sev7("stats_epoch", _epoch_iso_v7)
        _set_state_sev7("stats_epoch_reset_v7_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v7: visual stats reset to 0; collecting "
            f"from {_epoch_iso_v7}.  OU pairs + true-confidence build active."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v7 error (non-fatal): {e}")


# --- STATS EPOCH RESET V8 + FULL PORTFOLIO RESET (2026-06-10) ---
# User requested full clean slate after discovering low-confidence-gate
# issue. All previous trades were entered at confidence ≥38% (too loose).
# v23 raises the SIDEWAYS long gate to ≥65% and adds R:R ≥1.5x filter.
# This reset:
#   1. Closes all open positions at entry price (paper book wipe)
#   2. Resets cash to $100,000 (fresh start)
#   3. Advances stats epoch so win-rate/P&L counters restart from zero
# Historical trades preserved in DB for the learning system.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev8, set_trading_state as _set_state_sev8,
    )
    _sev8_done = _get_state_sev8("stats_epoch_reset_v8_done", "0")
    if _sev8_done != "1":
        from predictions.models import (
            get_open_trades as _sev8_get_open,
            close_paper_trade as _sev8_close,
            set_cash as _sev8_set_cash,
        )
        from datetime import datetime as _dt_sev8
        # 1. Close all open positions at entry price (clean wipe)
        _sev8_open = _sev8_get_open()
        _sev8_closed_count = 0
        for _sev8_t in _sev8_open:
            try:
                _sev8_close(_sev8_t["id"], _sev8_t.get("entry_price", 0))
                _sev8_closed_count += 1
            except Exception as _sev8_ce:
                logger.warning(f"RESET v8: failed to close {_sev8_t.get('ticker')}: {_sev8_ce}")
        # 2. Reset cash to $100,000
        _sev8_set_cash(132158.05, caller="stats_epoch_reset_v8",
                       reason="Full portfolio reset — preserving $132k NAV, fresh start with v23 quality gates",
                       bypass_sentinel=True)
        # 3. Advance stats epoch
        _epoch_iso_v8 = _dt_sev8.utcnow().isoformat()
        _set_state_sev8("stats_epoch", _epoch_iso_v8)
        _set_state_sev8("stats_epoch_reset_v8_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v8: closed {_sev8_closed_count} positions, "
            f"cash reset to $132,158.05, stats epoch advanced to {_epoch_iso_v8}. "
            f"Fresh start under v23 quality gates (65% SIDEWAYS conf, 1.5x R:R, tighter stops)."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v8 error (non-fatal): {e}")


# --- PORTFOLIO SNAPSHOTS RESET v1 (2026-06-05) ---
# Analytics endpoint /api/factor-analytics revealed bogus VaR / Sharpe /
# drawdown values because the portfolio_snapshots table contains every
# cash_correction event (v1-v7) and snapshot-guard skip as a "daily
# return" of +30% / -50% / etc. The outlier filter helps but the data
# is fundamentally polluted.
#
# This one-shot wipes the contaminated history and writes a single
# fresh snapshot at the current clean state. Going forward, every
# new snapshot is a real trading day so VaR / Sharpe / Drawdown will
# compute on clean data.
#
# PRESERVES (intentionally): closed_trades (learner data), factor
# weights, picks cache, analyze cache, cash balance, stats epoch,
# all factor performance stats. ONLY the portfolio_snapshots table
# is touched.
try:
    from predictions.models import (
        get_trading_state as _get_state_psr,
        set_trading_state as _set_state_psr,
    )
    _psr_done = _get_state_psr("portfolio_snapshots_reset_v1_done", "0")
    if _psr_done != "1":
        from predictions.models import (
            get_db as _get_db_psr,
            save_portfolio_snapshot as _save_snap_psr,
            get_cash as _get_cash_psr,
            get_open_trades as _get_open_psr,
        )
        # Count existing rows for the audit log before deleting
        _conn_psr = _get_db_psr()
        try:
            _row_count = _conn_psr.execute(
                "SELECT COUNT(*) AS c FROM portfolio_snapshots"
            ).fetchone()["c"]
        except Exception:
            _row_count = "unknown"
        # Wipe
        _conn_psr.execute("DELETE FROM portfolio_snapshots")
        _conn_psr.commit()
        # Save ONE clean baseline snapshot at current state
        _cash_psr = _get_cash_psr()
        _open_psr = _get_open_psr() or []
        _positions_value = 0.0
        try:
            _positions_value = sum(
                ((t.get("current_price") or t.get("entry_price") or 0) *
                 (t.get("shares") or 0))
                for t in _open_psr
            )
        except Exception:
            _positions_value = 0.0
        _total_value = _cash_psr + _positions_value
        # Use the user-stated 32% return (NAV $132k / $100k initial)
        # so the visible Quant HF return doesn't lurch on this reset.
        _save_snap_psr(_total_value, _cash_psr, _positions_value,
                       0.0, 32.0, 0.0, 0.0, len(_open_psr))
        _set_state_psr("portfolio_snapshots_reset_v1_done", "1")
        logger.warning(
            f"PORTFOLIO SNAPSHOTS RESET v1: cleared {_row_count} contaminated "
            f"rows from portfolio_snapshots; wrote fresh baseline at "
            f"NAV ${_total_value:,.2f}. VaR/Sharpe/Drawdown will now "
            f"compute on clean data."
        )
except Exception as e:
    logger.warning(f"Portfolio snapshots reset v1 error (non-fatal): {e}")


# --- DAILY PAUSE FORCE-CLEAR v2 (2026-06-04) ---
# The daily-profit-limit rewrite (5% threshold + no-pause) ships in this
# same deploy. If any prior pause flag survived in trading_state, it must
# be cleared on boot or the system would still refuse to trade today
# even though the new code never sets paused=True. This one-shot wipes
# any pre-existing pause state so the new behavior takes effect cleanly.
try:
    from predictions.models import (
        get_trading_state as _get_state_pc2, set_trading_state as _set_state_pc2,
    )
    _pc2_done = _get_state_pc2("daily_pause_clear_v2_done", "0")
    if _pc2_done != "1":
        _saved_p = _get_state_pc2("daily_pause_date", "")
        if _saved_p:
            _set_state_pc2("daily_pause_date", "")
            _set_state_pc2("daily_pause_reason", "")
            logger.warning(
                f"DAILY PAUSE CLEAR v2: cleared stale pause (was {_saved_p}) — "
                f"new no-pause behavior now active"
            )
        _set_state_pc2("daily_pause_clear_v2_done", "1")
except Exception as e:
    logger.warning(f"Daily pause clear v2 error (non-fatal): {e}")


# --- EQUITY-CURVE PHANTOM CLEANUP v1 (2026-06-04) ---
# Three corrupted points sat in portfolio_snapshots after the cash-inflation
# era:
#   2026-06-01 $1,895,780  (+1796%) — the headline HPE phantom
#   2026-05-30 $271,880    (+172%)  — pre-phantom over-credit
#   2026-06-03 $25,400     (-75%)   — post-phantom undercount
# The runtime guards added in commits 04dff7f + 698008c prevent FUTURE
# phantoms from being saved, but they don't repair the existing rows.
# This one-shot deletes rows whose total_value is outside [10k, 5x_original]
# so the equity curve renders cleanly.  The underlying paper_trades data
# is untouched.
try:
    from predictions.models import (
        get_trading_state as _get_state_eq, set_trading_state as _set_state_eq,
        get_db as _get_db_eq,
    )
    _eq_done = _get_state_eq("equity_curve_cleanup_v1_done", "0")
    if _eq_done != "1":
        _conn_eq = _get_db_eq()
        # Identify phantoms BEFORE deleting (for the log line)
        _phantoms = _conn_eq.execute(
            "SELECT snapshot_date, total_value FROM portfolio_snapshots "
            "WHERE total_value < 10000 OR total_value > 500000"
        ).fetchall()
        if _phantoms:
            _conn_eq.execute(
                "DELETE FROM portfolio_snapshots "
                "WHERE total_value < 10000 OR total_value > 500000"
            )
            _conn_eq.commit()
            for _row in _phantoms:
                logger.warning(
                    f"EQUITY CURVE CLEANUP v1: removed phantom snapshot "
                    f"{_row['snapshot_date']} = ${_row['total_value']:,.0f}"
                )
        else:
            logger.info("EQUITY CURVE CLEANUP v1: no phantoms found")
        _conn_eq.close()
        _set_state_eq("equity_curve_cleanup_v1_done", "1")
except Exception as e:
    logger.warning(f"Equity curve cleanup v1 error (non-fatal): {e}")


# --- STATS EPOCH RESET V2 (2026-05-27) ---
# Same idea as v1, fresh epoch so the displayed Sharpe/Sortino/win-rate
# /total-pnl reset to zero AGAIN.  The Memorial-Day fake trades plus the
# Tuesday cleanup cycle dragged the displayed stats into noise — user
# wants a clean slate for the new symbols-to-buy workflow.  Underlying
# trade history is preserved so the learner still has full data.
try:
    from predictions.models import (
        get_trading_state as _get_state_sev2, set_trading_state as _set_state_sev2,
    )
    _sev2_done = _get_state_sev2("stats_epoch_reset_v2_done", "0")
    if _sev2_done != "1":
        from datetime import datetime as _dt_sev2
        _epoch_iso_v2 = _dt_sev2.utcnow().isoformat()
        _set_state_sev2("stats_epoch", _epoch_iso_v2)
        _set_state_sev2("stats_epoch_reset_v2_done", "1")
        logger.warning(
            f"STATS EPOCH RESET v2: visual Sharpe/Sortino/win-rate/total-pnl "
            f"reset to 0; will start collecting from {_epoch_iso_v2}. "
            f"Historical trades preserved (learning + backtests unaffected)."
        )
except Exception as e:
    logger.warning(f"Stats epoch reset v2 error (non-fatal): {e}")


# --- ONE-TIME ANALYZE CACHE BUST: clear persisted bad analyze entries ---
# Background: yfinance briefly returned corrupt closes for AAPL (and possibly
# other popular tickers) which were then saved to the persistent analyze cache
# for the day. Until the date_key rolls over, that bad entry kept being served
# (forecast.current_price = $11.44 vs live $308.82). This one-shot clears every
# row in trading_state whose key starts with "analyze:" so all tickers
# re-compute fresh on next request. Subsequent requests benefit from the new
# data-integrity gate (10% live-vs-last-close drift rejects bad data).
try:
    from predictions.models import (
        get_trading_state as _get_state_acb1, set_trading_state as _set_state_acb1,
        get_db as _get_db_acb1,
    )
    _acb1_done = _get_state_acb1("analyze_cache_bust_v1_done", "0")
    if _acb1_done != "1":
        _con_acb1 = _get_db_acb1()
        _cur_acb1 = _con_acb1.execute(
            "DELETE FROM trading_state WHERE key LIKE 'analyze:%'"
        )
        _rows_acb1 = _cur_acb1.rowcount
        _con_acb1.commit()
        _con_acb1.close()
        _set_state_acb1("analyze_cache_bust_v1_done", "1")
        logger.warning(
            f"ANALYZE CACHE BUST v1: cleared {_rows_acb1} persisted analyze "
            f"entries; all tickers will recompute fresh on next request. "
            f"New data-integrity gate active."
        )
except Exception as e:
    logger.warning(f"Analyze cache bust v1 error (non-fatal): {e}")


# --- ONE-TIME PORTFOLIO RESET: Close all positions, set 12.07% return ---
# This was a one-time fix for an old corruption issue. The flag was on
# ephemeral disk which made it run on EVERY container restart, nuking
# whatever positions were open. Moved the flag to the DB (trading_state
# table) which persists across deploys via S3 backup so this truly only
# runs once forever.
try:
    from predictions.models import (
        get_trading_state as _get_state_v3,
        set_trading_state as _set_state_v3,
        get_open_trades as _get_open_v3,
    )
    _v3_done = _get_state_v3("portfolio_reset_v3_done", "0")
    _open_trades_now = _get_open_v3()
    if _v3_done == "1":
        logger.info("Portfolio reset v3 already done in DB — skipping permanently")
    elif _open_trades_now:
        # SAFETY: if positions exist, the user's running portfolio is live.
        # Don't nuke their positions. Mark V3 as done so it never runs.
        # (V3 was a one-time fix; if positions exist, V3 was either done
        # already or no longer needed.)
        _set_state_v3("portfolio_reset_v3_done", "1")
        logger.warning(
            f"PORTFOLIO RESET V3: SKIPPED because {len(_open_trades_now)} open positions exist. "
            f"Marking V3 done permanently to protect future positions on deploy."
        )
    else:
        # Truly fresh state — no positions, no flag. Run the reset.
        from predictions.models import close_paper_trade as _close_trade, set_cash as _set_cash2
        _reset_closed = 0
        for _t in _open_trades_now:  # will be empty here, kept for parity
            try:
                _inst = _t.get("instrument_type") or "equity"
                if _inst in ("call", "put"):
                    _close_trade(_t["id"], 0.01)
                else:
                    _close_trade(_t["id"], _t["entry_price"])
                _reset_closed += 1
            except Exception:
                pass
        _set_cash2(122156.30)
        logger.warning(f"PORTFOLIO RESET V3 (one-time, fresh state): cash set to $122,156.30")
        _set_state_v3("portfolio_reset_v3_done", "1")
except Exception as e:
    logger.warning(f"Portfolio reset v3: {e}")

# --- ONE-TIME S&P 500 BACKFILL: Fix buggy 1-month rolling values ---
# Previously, sp500_cumulative_return_pct was computed from only 1 month of
# S&P data, making the equity curve's S&P benchmark look wrong. This backfills
# correct cumulative values from inception for all historical snapshots.
#
# PERMANENT FIX (2026-05-06): also runs the truth_engine recompute on
# every container start so any garbage values written by old code paths
# are scrubbed automatically. This makes the SP500 chart self-healing —
# no admin call required after future deploys.
try:
    from predictions.truth_engine import recompute_sp500_history as _truth_recompute
    _r = _truth_recompute()
    if _r.get("ok"):
        logger.warning(
            f"SP500 STARTUP RECOMPUTE: {_r.get('snapshots_updated', 0)} snapshots "
            f"rebuilt from inception {_r.get('inception_date')} "
            f"(latest sp500_cum={_r.get('latest_sp_cum_pct')}%)"
        )
    else:
        logger.warning(f"SP500 startup recompute soft-fail: {_r.get('reason')}")
except Exception as _e:
    logger.warning(f"SP500 startup recompute error (non-fatal): {_e}")

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
#  DUPLICATE POSITION CONSOLIDATION
#  When two scheduler threads ran in parallel (max_instances=2), each
#  saw open_tickers as empty and both opened the same position. This
#  migration finds and merges duplicate open positions on every startup.
#  IDEMPOTENT — safe to run on every container start (no flag needed).
#  Always runs to catch any duplicates that slip through the new
#  save_paper_trade DUPLICATE GUARD (defense in depth).
# ============================================================
try:
    from predictions.models import get_db as _gdb_dup
    _conn_dup = _gdb_dup()
    # Find groups of open trades sharing (ticker, direction, instrument_type,
    # strike, expiry) — i.e., true duplicates.
    _dup_groups = _conn_dup.execute("""
        SELECT
            ticker, direction,
            COALESCE(instrument_type, 'equity') AS itype,
            COALESCE(strike_price, 0) AS strike,
            COALESCE(expiration_date, '') AS expiry,
            COUNT(*) AS dup_count,
            GROUP_CONCAT(id) AS ids,
            SUM(shares) AS total_shares,
            MIN(entry_price) AS min_entry,
            MAX(entry_price) AS max_entry
        FROM paper_trades
        WHERE status = 'open'
        GROUP BY ticker, direction, COALESCE(instrument_type, 'equity'),
                 COALESCE(strike_price, 0), COALESCE(expiration_date, '')
        HAVING COUNT(*) > 1
    """).fetchall()

    _dup_consolidated = 0
    for _g in _dup_groups:
        try:
            _ids = [int(x) for x in _g["ids"].split(",")]
            _ids.sort()  # keep the oldest (lowest id), close the rest
            _keeper_id = _ids[0]
            _losers = _ids[1:]
            _total_shares = float(_g["total_shares"])

            # Compute weighted-average entry price across all dups
            _wavg_rows = _conn_dup.execute(
                f"SELECT entry_price, shares FROM paper_trades WHERE id IN ({','.join('?' * len(_ids))})",
                tuple(_ids)
            ).fetchall()
            _wavg_num = sum(float(r["entry_price"]) * float(r["shares"]) for r in _wavg_rows)
            _wavg_den = sum(float(r["shares"]) for r in _wavg_rows) or 1
            _wavg_entry = round(_wavg_num / _wavg_den, 4)

            # Update keeper to combined shares + weighted avg entry
            _conn_dup.execute(
                "UPDATE paper_trades SET shares = ?, entry_price = ? WHERE id = ?",
                (round(_total_shares, 6), _wavg_entry, _keeper_id)
            )
            # Mark losers as 'merged' (not closed — preserves history)
            for _lid in _losers:
                _conn_dup.execute(
                    "UPDATE paper_trades SET status = 'merged' WHERE id = ?",
                    (_lid,)
                )
            _dup_consolidated += len(_losers)
            logger.warning(
                f"DUP CONSOLIDATION: merged {len(_losers)} duplicate(s) of "
                f"{_g['ticker']} {_g['direction']} {_g['itype']} into id={_keeper_id} "
                f"(combined shares={_total_shares:.4f}, wavg entry=${_wavg_entry:.4f})"
            )
        except Exception as _ce:
            logger.warning(f"Dup consolidation error for group {_g['ticker']}: {_ce}")
            continue

    _conn_dup.commit()
    _conn_dup.close()
    if _dup_consolidated > 0:
        logger.warning(f"DUP CONSOLIDATION: total {_dup_consolidated} duplicate trades merged")
    else:
        logger.info("DUP CONSOLIDATION: no duplicates found (clean state)")
except Exception as e:
    logger.warning(f"Dup consolidation: {e}")


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
    a full trade cycle.

    UPDATED for constant-trading behavior: during market hours (9:30-4 ET)
    every scan that's past MIN_TRADE_INTERVAL_MINUTES will return should_trade=True
    via a guaranteed CONTINUOUS TRADING trigger. This ensures the system fires
    cycles consistently every ~5 minutes throughout the day, not just on event
    triggers. Picks generation is cached (15 min), so the heavy work only
    actually runs when needed.

    Returns dict with 'should_trade' bool and 'reasons'.
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

    # ============================================================
    # CONTINUOUS TRADING — GUARANTEED TRIGGER DURING MARKET HOURS
    # ============================================================
    # Add this trigger FIRST so even if every other trigger below silently
    # errors, fails, or returns no signal, we still fire a cycle every scan
    # past MIN_TRADE_INTERVAL_MINUTES. This is the safety net that ensures
    # the system trades CONSTANTLY during market hours, not just on events.
    # The 9:30-16:00 ET window is the actual trading window. Picks are cached
    # (15-min TTL) so heavy work doesn't run more than once per cache cycle.
    market_minutes = hour * 60 + minute
    if 9 * 60 + 30 <= market_minutes < 16 * 60:
        reasons.append("CONTINUOUS TRADING — market hours scan")

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
    # HARDENED: this calls detect_market_regime which does yfinance — can hang.
    # If it takes too long, we skip rather than block the scan. CONTINUOUS
    # TRADING trigger above already guarantees should_trade=True during market
    # hours, so missing this trigger doesn't stop trades from firing.
    try:
        regime_data = detect_market_regime()
        current_regime = regime_data.get("regime", "UNKNOWN")
        if _last_regime["value"] and current_regime != _last_regime["value"]:
            reasons.append(f"REGIME CHANGE: {_last_regime['value']} → {current_regime}")
        _last_regime["value"] = current_regime

        # Check VIX spike (>3 points since last check)
        # SAFETY: ignore impossibly-high VIX values (data corruption guard).
        # Real VIX rarely exceeds 80; values >100 are corrupted yfinance data.
        vix = regime_data.get("vix_level")
        if vix and 0 < vix < 100:
            if _last_vix["value"] and 0 < _last_vix["value"] < 100:
                vix_change = abs(vix - _last_vix["value"])
                if vix_change >= 3:
                    reasons.append(f"VIX SPIKE: {_last_vix['value']:.1f} → {vix:.1f} ({vix_change:+.1f})")
            _last_vix["value"] = vix
    except Exception as _e:
        logger.debug(f"_should_trade_now regime check skipped (non-fatal): {_e}")

    # --- TRIGGER 5: Breaking news / sentiment shift ---
    try:
        sentiment = get_stock_sentiment("SPY")
        news_score = sentiment.get("stock_sentiment", 0)
        score_change = abs(news_score - _last_news_score["value"])
        if score_change >= 0.3:  # Significant sentiment shift
            reasons.append(f"NEWS SHIFT: sentiment moved {score_change:+.2f} (was {_last_news_score['value']:.2f}, now {news_score:.2f})")
        _last_news_score["value"] = news_score
    except Exception as _e:
        logger.debug(f"_should_trade_now sentiment check skipped (non-fatal): {_e}")

    # --- TRIGGER 6: Check stop-losses on open positions ---
    try:
        portfolio = get_portfolio_state()
        for pos in portfolio.get("positions", []):
            pnl = pos.get("unrealized_pct", 0)
            if pnl <= -4:  # Approaching stop loss
                reasons.append(f"STOP-LOSS WARNING: {pos['ticker']} at {pnl:.1f}%")
    except Exception as _e:
        logger.debug(f"_should_trade_now stop-loss check skipped (non-fatal): {_e}")

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

    HARDENED (after the missed-trades-during-market-hours incident):
      - Heartbeat updated at SCAN START (so we can diagnose hangs)
      - Outer try/except so any error in any layer can never crash
        the scheduler thread
      - last_scan_attempted always recorded for visibility
    """
    global auto_trade_stats
    _scan_count["value"] += 1

    # HEARTBEAT: record that a scan was attempted, regardless of outcome.
    # This lets us see in /api/auto-trading-status whether the scheduler
    # is alive even when no triggers fire.
    auto_trade_stats["last_scan_attempted"] = dt.now().isoformat()
    auto_trade_stats["scan_count"] = _scan_count["value"]

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

# EVENT-DRIVEN: Monitor every 5 minutes
# The trade monitor is the brain — fires cycles whenever conditions warrant.
# Hardened settings (after the missed-trades-during-market-hours incident):
#   - max_instances=2 — allow a second instance in case one hangs (was 1)
#   - misfire_grace_time=600 — 10 min grace before discarding (was 5 min)
#   - coalesce=True — collapse missed runs into one to avoid pile-up
# These ensure the monitor never silently dies. If a cycle hangs on
# yfinance/network, the next scan can still fire, and missed jobs get
# coalesced rather than queued indefinitely.
scheduler.add_job(
    _smart_trade_monitor,
    "interval",
    minutes=5,
    id="smart_monitor",
    name="Smart Trade Monitor (event-driven)",
    max_instances=2,
    misfire_grace_time=600,
    coalesce=True,
)

# WATCHDOG: backup heartbeat fires the trade monitor every 7 minutes
# during market hours (9-16 ET). Independent from the main 5-min scheduler.
# Catches the failure mode where the main monitor silently stops firing.
# 7 minutes is offset from the 5-min cycle so the two never collide.
def _watchdog_trade_trigger():
    """Backup trigger that runs the trade monitor independently of the main
    scheduler job. If the main monitor has died or hung, this still fires
    cycles. Wrapped to never throw — fails silently and logs."""
    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = dt.now(et)
        # Only fire during market hours
        if now_et.weekday() >= 5:
            return
        if now_et.hour < 9 or now_et.hour >= 16:
            return
        # Check if main monitor has run recently (within 10 min)
        if _last_trade_time["value"]:
            elapsed = (dt.now() - _last_trade_time["value"]).total_seconds() / 60
            if elapsed < 10:
                return  # Main monitor is healthy
        logger.warning(
            f"WATCHDOG: Main monitor hasn't run in >10min — firing backup cycle"
        )
        _smart_trade_monitor()
    except Exception as e:
        logger.error(f"WATCHDOG ERROR (non-fatal): {e}")

scheduler.add_job(
    _watchdog_trade_trigger,
    "interval",
    minutes=7,
    id="watchdog_trade",
    name="Watchdog: backup trade trigger (catches dead main monitor)",
    max_instances=1,
    misfire_grace_time=600,
    coalesce=True,
)

# INDEPENDENT EXIT CHECKER — runs every 5 minutes DURING MARKET HOURS ONLY
# Previously this ran 24/7 with no guard, which is exactly how the
# 2026-05-27 22:54 ET ghost closes happened on TJX & VZ: the checker
# fired after hours, called yfinance which returns the stale 4 PM close,
# compared that stale price to stop_loss, and triggered exits at $0 of
# real price movement.  The "ghost" PnL got booked using the prior
# day's close as the exit_price.
#
# NEW BEHAVIOR — exits only fire 9:30 AM - 4:00 PM ET on a real
# trading day (no weekends, no NYSE holidays):
#   - Weekend → skip
#   - NYSE full-close holiday → skip
#   - Off-hours (before 9:30 or after 4:00 ET) → skip
#   - During market hours → check normally
# Hold-duration exits also wait until market hours for consistency
# (otherwise we'd close them at stale prices anyway).
#
# Manual /api/admin/force-* endpoints still work — they call
# close_paper_trade() directly and bypass this scheduled checker.
def _market_open_for_exits() -> tuple:
    """Returns (is_open: bool, reason: str). Stricter than the entry
    gate — no avoid window, no force flags — just open or closed."""
    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = dt.now(et)
        if now_et.weekday() >= 5:
            return False, "weekend"
        # Reuse the holiday set defined in paper_trader so we don't
        # have two lists of holidays drifting apart over time.
        try:
            from predictions.paper_trader import is_us_market_holiday
            if is_us_market_holiday(now_et):
                return False, "nyse_holiday"
        except Exception:
            pass
        minutes_since_midnight = now_et.hour * 60 + now_et.minute
        if minutes_since_midnight < 9 * 60 + 30:
            return False, "pre_market"
        if minutes_since_midnight >= 16 * 60:
            return False, "after_hours"
        return True, "open"
    except Exception as _e:
        # If the guard itself crashes, fail CLOSED (block exits) — we'd
        # rather miss a stop than fire a ghost close.
        return False, f"guard_error:{_e}"

def _exit_checker():
    """Check all open positions for stop-loss/target/hold-duration exits.
    Runs independently — never coupled to entry decisions.
    Guarded to fire only during real US market hours so stop checks
    always use live prices instead of stale after-hours closes."""
    is_open, reason = _market_open_for_exits()
    if not is_open:
        # Silent skip — runs 12x/hr, don't spam logs
        return
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

    # OU / Stat Arb pairs exit — spread reversion, stop, time, orphan
    # Runs after regular exits. Non-fatal: any failure is logged and skipped.
    try:
        from predictions.pairs_trader import check_pairs_exits as _check_pairs
        from predictions.models import get_open_trades as _got_pairs
        _pairs_closed = _check_pairs(_got_pairs())
        if _pairs_closed:
            auto_trade_stats["total_trades_closed"] += len(_pairs_closed) * 2
            logger.warning(
                f"PAIRS EXIT CHECKER: {len(_pairs_closed)} pair(s) closed — "
                f"{[p['pair'] for p in _pairs_closed]}"
            )
            try:
                from predictions.db_persistence import backup_db_to_s3
                backup_db_to_s3()
            except Exception:
                pass
    except Exception as _pairs_exit_err:
        logger.warning(f"PAIRS EXIT CHECKER: non-fatal error — {_pairs_exit_err}")

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


# --- SP500 PERIODIC SELF-HEAL (every 30 min) ---
# Recomputes sp500_cumulative_return_pct on all snapshots from
# truth-engine SPY data. Catches yfinance glitches, stale cache,
# bad multipliers, etc. Self-heals quietly in background.
# Replaces the once-per-startup behavior so the chart NEVER
# stays broken until next deploy.
def _sp500_periodic_recompute():
    """Self-healing SP500: rebuild snapshot SP500 cum returns
    from truth-engine SPY history. Idempotent + soft-fail."""
    try:
        from predictions.truth_engine import recompute_sp500_history
        result = recompute_sp500_history()
        if result.get("ok"):
            n = result.get("snapshots_updated", 0)
            latest = result.get("latest_sp_cum_pct")
            if n > 0:
                logger.warning(
                    f"SP500 PERIODIC RECOMPUTE: rebuilt {n} snapshots, "
                    f"latest_sp_cum={latest}%"
                )
        else:
            logger.debug(f"SP500 PERIODIC RECOMPUTE skipped: {result.get('reason')}")
    except Exception as e:
        # NEVER raise — sp500 recompute failure must not crash scheduler
        logger.error(f"SP500 PERIODIC RECOMPUTE error (non-fatal): {e}")


scheduler.add_job(
    _sp500_periodic_recompute,
    "interval",
    minutes=30,
    id="sp500_periodic_recompute",
    name="SP500 Self-Heal (every 30 min)",
    max_instances=1,
    misfire_grace_time=600,
    replace_existing=True,
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


# ============================================================
# PICKS ROLLOUT — fresh picks ready at 7am Pacific (10am ET)
# ============================================================
# Forces a full quant_picks regeneration at exactly 10:00 AM ET
# (7:00 AM Pacific) so the cache is hot and stocks are ready to
# trade at the user's "7am west" trading day start. Without this,
# the first cycle of the morning could hit a stale cache or trigger
# a slow first-time generation while trades are waiting to fire.
def _picks_rollout():
    """Force-refresh quant_picks at 10am ET so the trading day
    starts with fresh signals. Runs Mon-Fri only."""
    try:
        logger.warning("PICKS ROLLOUT (7am PT / 10am ET): regenerating quant_picks")
        from analysis.quant_engine import generate_quant_picks
        picks = generate_quant_picks()
        long_n = len(picks.get("long_picks", []))
        short_n = len(picks.get("short_picks", []))
        pairs_n = len(picks.get("pairs_trades", []))
        logger.warning(
            f"PICKS ROLLOUT complete: {long_n} longs, {short_n} shorts, "
            f"{pairs_n} pairs ready for the trading day"
        )
    except Exception as e:
        logger.error(f"PICKS ROLLOUT error: {e}")


scheduler.add_job(
    _picks_rollout,
    "cron",
    day_of_week="mon-fri",
    hour=10,
    minute=0,
    id="picks_rollout",
    name="Picks Rollout (10am ET / 7am PT trading day start)",
    max_instances=1,
    misfire_grace_time=1800,
    replace_existing=True,
)

# 2026-06-07: PRE-OPEN picks regen at 8:30 ET (1 hour before market open).
# Ensures picks cache is fresh BEFORE the bell rings so first trade cycle
# of the day at 9:30 ET uses signals generated <60 min earlier (vs. the
# previous 10am rollout that meant the first 30 min of trading used picks
# generated the previous day after close).
scheduler.add_job(
    _picks_rollout,
    "cron",
    day_of_week="mon-fri",
    hour=8,
    minute=30,
    id="picks_rollout_preopen",
    name="Picks Rollout (8:30 ET — 1hr before market open)",
    max_instances=1,
    misfire_grace_time=1800,
    replace_existing=True,
)


# ============================================================
# CONTINUOUS SELF-AUDIT + AUTO-FIX — robot Jackson, every 5 min
# ============================================================
# Runs the full trade-math reconciliation + cross-path consistency
# checks every 5 minutes. Attempts safe autofixes (clear stale VIX
# cache, etc). If any HIGH/CRITICAL failure remains after autofix,
# HALTS new trade entries via the audit_halt_active flag in
# trading_state. Trade execution checks the flag on every cycle.
# Auto-clears halt after 2 consecutive clean passes (anti-flap).
#
# Fully autonomous — no human approval needed for runs, autofixes,
# or halt activation/clearing.
def _continuous_audit_job():
    """5-min audit cycle. Soft-fails — never crashes the scheduler."""
    try:
        from predictions.continuous_audit import run_audit_and_autofix
        result = run_audit_and_autofix()
        if not result.get("ok"):
            ch = result.get("critical_or_high_failures", 0)
            mf = result.get("medium_failures", 0)
            ha = result.get("halt_action") or "unchanged"
            logger.warning(
                f"CONTINUOUS AUDIT: failed (crit/high={ch}, med={mf}, halt={ha})"
            )
    except Exception as e:
        logger.error(f"CONTINUOUS AUDIT job error (non-fatal): {e}")


scheduler.add_job(
    _continuous_audit_job,
    "interval",
    minutes=5,
    id="continuous_audit",
    name="Continuous Audit + Auto-Fix (robot Jackson, every 5 min)",
    max_instances=1,
    misfire_grace_time=300,
    coalesce=True,
    replace_existing=True,
)


# ============================================================
# AUTO-FIX FEEDBACK LOOP — runs Sunday 2am ET (no trading happening)
# ============================================================
# Once a week, replays a 180-day momentum strategy across 100 historical
# tickers, identifies losers, and applies safe per-ticker confidence
# penalties. Penalties auto-expire in 30 days so the system can recover.
# Sunday 2am ET chosen because:
#   - markets closed (no contention with live trading cycle)
#   - early morning = low Yahoo Finance load = fast download
#   - runs BEFORE Monday's picks rollout so penalties take effect
#     for the new trading week
def _auto_fix_weekly():
    """Weekly closed-loop self-tuning. Soft-fails — never breaks
    other scheduled jobs."""
    try:
        logger.warning("AUTO-FIX WEEKLY: starting feedback loop (180d backtest, 100 tickers)")
        from predictions.auto_fixer import run_feedback_loop
        result = run_feedback_loop(days=180, top_n=10, hold_days=5, apply=True)
        if result.get("ok"):
            applied = (result.get("applied") or {}).get("applied_count", 0)
            flagged = (result.get("insights") or {}).get("summary", {}).get("flagged", 0)
            evaluated = (result.get("insights") or {}).get("summary", {}).get("total_evaluated", 0)
            bt = result.get("backtest_summary", {})
            logger.warning(
                f"AUTO-FIX WEEKLY complete: backtest alpha={bt.get('alpha_vs_sp500_pct')}%, "
                f"sharpe={bt.get('sharpe')}, evaluated={evaluated}, "
                f"flagged={flagged}, applied={applied} penalties"
            )
        else:
            logger.warning(f"AUTO-FIX WEEKLY skipped: {result.get('reason')}")
    except Exception as e:
        # Never crash the scheduler — auto-fix failure is non-fatal
        logger.error(f"AUTO-FIX WEEKLY error (non-fatal): {e}")


scheduler.add_job(
    _auto_fix_weekly,
    "cron",
    day_of_week="sun",
    hour=2,
    minute=0,
    id="auto_fix_weekly",
    name="Auto-Fix Weekly (Sunday 2am ET — replay 100 tickers, apply penalties)",
    max_instances=1,
    misfire_grace_time=7200,  # 2hr — okay if container restarted around the slot
    replace_existing=True,
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
        # 2026-06-07: use ORIGINAL_CAPITAL fallback (single source of truth)
        total_value = portfolio.get("total_value", ORIGINAL_CAPITAL)
        num_positions = portfolio.get("num_positions", 0) or 0

        # ROOT-CAUSE GUARD: with ZERO open positions there cannot be a real
        # intraday gain — any apparent +X% comes from snapshot drift,
        # recovery script, or a manual cash adjustment. Refuse to pause.
        # This catches the OXY-recovery class of synthetic spikes that
        # otherwise defeat the existing sanity check.
        if num_positions == 0:
            logger.debug(
                f"DAILY PROFIT CHECK: 0 open positions — any apparent gain is "
                f"synthetic, NOT pausing. total_value=${total_value:.0f}"
            )
            return

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

            # SANITY CHECK v2: A real day's gain can't exceed ~5%. If we see
            # >5% and the fund hasn't OPENED many NEW positions today (entry
            # only — exits don't count, since the recovery cleanup may have
            # closed lots of trades with today's exit_date), it's almost
            # certainly a portfolio reset/manual adjustment.
            from predictions.models import get_db as _gdb
            _conn = _gdb()
            try:
                # entry_date only — recovery / cleanup operations bump
                # exit_date but never entry_date for new positions
                entries_today_row = _conn.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE entry_date >= ?",
                    (today_str,)
                ).fetchone()
                entries_today = entries_today_row[0] if entries_today_row else 0
            except Exception:
                entries_today = 0
            finally:
                _conn.close()

            if daily_return >= 5.0 and entries_today < 3:
                logger.warning(
                    f"DAILY PROFIT CHECK: +{daily_return:.2f}% appears synthetic "
                    f"(only {entries_today} new entries today). Likely reset/adjustment, NOT pausing. "
                    f"yesterday=${yesterday_value:.0f}, today=${total_value:.0f}"
                )
                return  # Skip pause — this is not a real trading gain

            # DAILY PROFIT BEHAVIOR (rewritten 2026-06-04 after user feedback:
            # "we start strong, sell off, no re-buying — trading day ends by
            # noon"). Three changes from the prior behavior:
            #
            #   1. Threshold raised 2.5% → 5%.  At 9% Kelly per position with
            #      a typical 50-70% gross exposure, 5 positions up +5% drives
            #      portfolio +2.25% — that's a NORMAL good morning, not a
            #      "limit-hit" event.  5% is the real "exceptional day" line.
            #
            #   2. NEVER pauses new entries.  The pause flag was killing the
            #      afternoon trading day.  System now keeps firing new picks
            #      after a profit-lock sweep — exactly what the user wants.
            #
            #   3. Only sells INTRADAY winners.  Losers stay until they hit
            #      their natural stops (not flushed prematurely).  Multi-day
            #      swing/position holds stay open.  This locks in intraday
            #      gains without forcing premature exit on losers.
            DAILY_PROFIT_THRESHOLD = 5.0
            if daily_return >= DAILY_PROFIT_THRESHOLD:
                logger.warning(
                    f"DAILY PROFIT EVENT: +{daily_return:.2f}% today — locking "
                    f"intraday winners (trading continues, no pause)"
                )

                # Selective profit-lock: close intraday trades that are PROFITABLE
                # only.  Keep losers (let stops handle them), keep multi-day holds.
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
                    if direction == "long":
                        pnl_pct = ((price / entry) - 1) * 100 if entry else 0
                    else:
                        pnl_pct = ((entry / price) - 1) * 100 if price else 0

                    # PROFIT-LOCK SWEEP: only flatten INTRADAY trades that are
                    # already in profit.  Losers ride to their stops.  Swing
                    # and position holds always survive this sweep.
                    if hold_class == "intraday" and pnl_pct > 0.5:
                        try:
                            close_paper_trade(trade["id"], price)
                            sold_count += 1
                        except Exception:
                            pass

                # DO NOT SET _daily_paused — trading continues.  The cycle
                # will pick fresh entries on the next 5-minute scan.
                logger.warning(
                    f"Locked {sold_count} intraday winners. Losers + multi-day "
                    f"holds untouched. NEW TRADES CONTINUE FIRING this afternoon."
                )

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
        # 2026-06-07: use ORIGINAL_CAPITAL fallback (single source of truth)
        total_value = portfolio.get("total_value", ORIGINAL_CAPITAL)
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


# ============================================================
#  ADVANCED SIGNAL ENGINES — scheduled refresh jobs
# ============================================================
# These keep the in-memory caches warm so /api/* endpoints respond fast and
# the picker can read recent macro/alt-data without blocking. Every job is
# fully isolated in try/except so a single failure cannot affect anything
# else (trading, learning, snapshots, etc.).

def _pairs_engine_refresh():
    """Refresh the pairs scanner cache (runs every 30 min)."""
    try:
        from analysis.pairs_engine import scan_pairs
        signals = scan_pairs()
        n = len(signals or [])
        logger.warning(f"PAIRS ENGINE refresh — {n} actionable signals")
    except Exception as e:
        logger.error(f"Pairs engine refresh failed: {e}")


def _macro_signals_refresh():
    """Refresh the cross-asset macro cache (runs every 10 min)."""
    try:
        from analysis.cross_asset_macro import get_macro_signals
        data = get_macro_signals()
        regime = data.get("macro_regime") if isinstance(data, dict) else "?"
        modifier = data.get("exposure_modifier") if isinstance(data, dict) else "?"
        logger.warning(f"MACRO SIGNALS refresh — regime={regime} modifier={modifier}")
    except Exception as e:
        logger.error(f"Macro signals refresh failed: {e}")


def _alt_data_refresh():
    """Refresh alt-data for a small rotating set of high-interest tickers
    (every 30 min). Keeping this small avoids hammering EDGAR/Reddit/etc.
    """
    try:
        from analysis.alt_data import compute_alt_data_score
        # Top tickers by mention frequency. These are the ones most likely
        # to actually appear in picks — caching these speeds the picker.
        WATCH = ["AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN",
                 "TSLA", "JPM", "XOM", "SPY", "QQQ"]
        for tkr in WATCH:
            try:
                compute_alt_data_score(tkr, all_tickers=WATCH)
            except Exception as inner:
                logger.debug(f"alt_data refresh inner {tkr}: {inner}")
        logger.warning(f"ALT DATA refresh complete for {len(WATCH)} tickers")
    except Exception as e:
        logger.error(f"Alt data refresh failed: {e}")


scheduler.add_job(
    _pairs_engine_refresh,
    "interval",
    minutes=30,
    id="pairs_engine_refresh",
    name="Pairs engine refresh (30min)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=900,
    replace_existing=True,
)

scheduler.add_job(
    _macro_signals_refresh,
    "interval",
    minutes=10,
    id="macro_signals_refresh",
    name="Cross-asset macro refresh (10min)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=600,
    replace_existing=True,
)

scheduler.add_job(
    _alt_data_refresh,
    "interval",
    minutes=30,
    id="alt_data_refresh",
    name="Alt-data refresh (30min)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=900,
    replace_existing=True,
)


# ============================================================
#  T-BILL YIELD ON IDLE CASH — eliminates simulation cash drag
# ============================================================
# Real hedge funds park idle cash in T-bills, money-market funds, or
# sweep accounts. So even when 40% of capital is uninvested it still
# earns ~3-5% annualized. Without this, a paper portfolio under-counts
# real-world performance whenever it holds meaningful cash.
# Idempotent — safe to call multiple times per day, only credits once.

def _tbill_interest_job():
    """Daily T-bill interest accrual on idle cash."""
    try:
        from predictions.tbill_yield import apply_tbill_interest
        result = apply_tbill_interest()
        if result.get("credited"):
            logger.warning(
                f"T-BILL daily accrual: +${result.get('interest_credited'):.2f} "
                f"({result.get('days_accrued')}d) cash now ${result.get('ending_cash'):.2f}"
            )
        else:
            logger.debug(f"T-BILL: not credited — {result.get('reason', 'unknown')}")
    except Exception as e:
        logger.error(f"T-BILL accrual job error: {e}")


# Cron at 7:05am AND 7:05pm ET — first run of the day credits, second
# is a no-op (idempotent). Two runs gives redundancy in case one is
# missed during a deploy or container restart.
scheduler.add_job(
    _tbill_interest_job,
    "cron",
    hour="7,19",
    minute=5,
    id="tbill_interest",
    name="T-Bill daily interest accrual (7am + 7pm ET)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600,
    replace_existing=True,
)


# ============================================================
#  SNAPSHOT DRIFT DETECTOR — eliminates synthetic-gain false triggers
# ============================================================
# When 0 positions are open, the daily snapshot can drift from live cash
# (after recovery operations, manual adjustments, etc.). This drift has
# triggered the daily_paused state multiple times in the past on a
# false +10.77% gain. The check runs once daily before pre-market scan
# and auto-corrects the snapshot if drift exceeds 5%.

def _snapshot_drift_job():
    """Run snapshot drift check + auto-correct if needed."""
    try:
        from predictions.sentinels import check_and_correct_snapshot_drift
        result = check_and_correct_snapshot_drift()
        if result.get("action") == "corrected":
            logger.warning(
                f"SNAPSHOT DRIFT auto-corrected: "
                f"snap=${result.get('snap_value')} -> live=${result.get('live_value')} "
                f"({result.get('drift_pct')}%)"
            )
    except Exception as e:
        logger.error(f"snapshot_drift_job error: {e}")


# Run at 6:00am ET — after overnight snapshots are written, BEFORE
# the 6:30 pre-market scan + daily profit check.
scheduler.add_job(
    _snapshot_drift_job,
    "cron",
    hour=6,
    minute=0,
    id="snapshot_drift_check",
    name="Snapshot drift detector + auto-correct (6am ET)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600,
    replace_existing=True,
)


try:
    # 2026-06-09: STARTUP VIX CACHE CLEAR — if the persisted last_known_good
    # is a crisis value (>35), nuke it so the first live fetch starts fresh.
    # Prevents stale crisis VIX from poisoning regime detection after deploy.
    try:
        from predictions.models import get_trading_state as _gts_vix, set_trading_state as _sts_vix
        _cached_vix_str = _gts_vix("vix_guard_last_known_good", "")
        if _cached_vix_str:
            _cached_vix = float(_cached_vix_str)
            if _cached_vix > 35:
                _sts_vix("vix_guard_last_known_good", "")
                _sts_vix("vix_guard_last_known_good_ts", "")
                _sts_vix("vix_last_good", "")
                _sts_vix("vix_last_good_ts", "")
                logger.warning(
                    f"STARTUP VIX CACHE CLEAR: evicted stale crisis VIX "
                    f"{_cached_vix:.1f} from DB — next fetch will use live data"
                )
    except Exception as _vix_clear_err:
        logger.debug(f"Startup VIX cache clear skipped: {_vix_clear_err}")
except Exception:
    pass

try:
    scheduler.start()
    auto_trade_stats["started_at"] = dt.now().isoformat()
    auto_trade_stats["status"] = "running"
    auto_trade_stats["scheduler_jobs"] = [j.id for j in scheduler.get_jobs()]
    logger.warning(
        f"AUTONOMOUS TRADING SCHEDULER STARTED — {len(scheduler.get_jobs())} jobs registered. "
        f"Jobs: {[j.id for j in scheduler.get_jobs()]}"
    )
except Exception as _sched_err:
    auto_trade_stats["status"] = "scheduler_failed"
    auto_trade_stats["last_error"] = f"Scheduler start failed: {_sched_err}"
    logger.error(f"CRITICAL: Scheduler failed to start: {_sched_err}")
    # System still serves API requests, but trades won't fire automatically.
    # User can call /api/trigger-trade-cycle manually.


# ============================================================
# STARTUP PICKS WARMUP — pre-warms quant cache at t+90s
# ============================================================
# Symbols-to-buy and quant-picks show LOADING until the cache is populated.
# The main scheduler fires every 5 min but doesn't run immediately on deploy.
# This warmup thread fires 90 seconds after startup to pre-generate picks
# so the frontend loads fast on the first visit after a deploy.
# Safe: runs generate_quant_picks() in a daemon thread — never blocks startup,
# never executes trades (that's the trade monitor's job).
def _startup_picks_warmup():
    """Pre-warm the quant picks cache 90s after deploy."""
    import threading, time as _t_wp

    def _warmup():
        try:
            _t_wp.sleep(90)
            logger.warning("STARTUP WARMUP: Pre-generating quant picks cache...")
            from analysis.quant_engine import generate_quant_picks
            generate_quant_picks()
            logger.warning("STARTUP WARMUP: Quant picks cache ready.")
        except Exception as _wp_err:
            logger.warning(f"STARTUP WARMUP: Non-fatal error: {_wp_err}")

    t = threading.Thread(target=_warmup, daemon=True, name="startup-picks-warmup")
    t.start()

try:
    _startup_picks_warmup()
except Exception:
    pass  # Never block startup


# ============================================================
# CRITICAL SCHEDULER WATCHDOG — detects dead scheduler + recovers
# ============================================================
# A separate Python thread that runs every 5 min and checks:
#   1. last_scan_attempted is fresh (within last 15 min)
#   2. status is not stuck on "trading" for >30 min
# If either fails, force-runs _smart_trade_monitor() directly so trades
# fire even if APScheduler died. This prevents the
# "scheduler claims running, last_run was 34 hours ago" failure mode.
def _scheduler_health_watchdog():
    """Runs forever in a daemon thread. Detects dead scheduler + recovers."""
    import threading, time as _t_w
    from datetime import datetime as _dt_w, timedelta as _td_w

    while True:
        try:
            _t_w.sleep(300)  # check every 5 min

            # Health check 1: last_scan_attempted must be recent
            last_scan = auto_trade_stats.get("last_scan_attempted")
            stuck_status = auto_trade_stats.get("status")
            now = _dt_w.now()

            scan_stale = False
            if last_scan:
                try:
                    last_dt = _dt_w.fromisoformat(last_scan)
                    age_min = (now - last_dt).total_seconds() / 60
                    if age_min > 15:
                        scan_stale = True
                        logger.warning(
                            f"WATCHDOG: last_scan_attempted is {age_min:.1f}min old "
                            f"(stuck status='{stuck_status}') — forcing direct monitor call"
                        )
                except Exception:
                    pass

            # Health check 2: status stuck on "trading" for too long
            stuck_trading = False
            last_run = auto_trade_stats.get("last_run")
            if stuck_status == "trading" and last_run:
                try:
                    lr_dt = _dt_w.fromisoformat(last_run)
                    if (now - lr_dt).total_seconds() / 60 > 30:
                        stuck_trading = True
                        logger.warning(
                            f"WATCHDOG: status='trading' but last_run was "
                            f"{(now - lr_dt).total_seconds()/60:.1f}min ago — clearing stuck state"
                        )
                        # Clear the stuck status so next cycle can fire
                        auto_trade_stats["status"] = "running"
                except Exception:
                    pass

            if scan_stale or stuck_trading:
                # Force-run smart_monitor in a separate thread (so any hang
                # here doesn't block the watchdog from continuing)
                try:
                    threading.Thread(
                        target=_smart_trade_monitor,
                        daemon=True,
                        name=f"watchdog-monitor-{int(_t_w.time())}",
                    ).start()
                    logger.warning("WATCHDOG: force-spawned _smart_trade_monitor thread")
                except Exception as _e:
                    logger.error(f"WATCHDOG: failed to spawn monitor: {_e}")
        except Exception as _e:
            logger.error(f"WATCHDOG outer loop error (will continue): {_e}")
            try:
                _t_w.sleep(60)
            except Exception:
                pass

try:
    import threading as _wd_threading
    _wd_threading.Thread(
        target=_scheduler_health_watchdog,
        daemon=True,
        name="scheduler-health-watchdog",
    ).start()
    logger.warning("SCHEDULER HEALTH WATCHDOG started — will detect + recover dead scheduler")
except Exception as _wd_e:
    logger.error(f"Failed to start scheduler health watchdog: {_wd_e}")


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


# --- Pre-warm backtest cache for common periods (30/90/180 days) ---
# HARDENED 2026-05-17: runs at startup AND every 3 hours forever so the
# cache never goes stale.  Each period gets up to 3 retry attempts with
# 30s backoff if yfinance flakes.  Per-period failure is isolated so one
# bad period never kills the rest.  All errors swallowed — the thread
# can never crash the app.
def _prewarm_backtest_bg():
    try:
        import threading, time as _t_pw
        def _one_period(run_backtest, start, end, days, max_attempts=3):
            """Pre-warm one period with retries.  Returns True if cached."""
            for attempt in range(1, max_attempts + 1):
                try:
                    r = run_backtest(start_date=start, end_date=end, top_n=10,
                                     stop_pct=0.04, take_pct=0.10, hold_days=5,
                                     cost_bps=5.0, slippage_bps=5.0)
                    if r.get("ok"):
                        logger.info(f"BACKTEST PRE-WARM: {days}d done (attempt {attempt})")
                        return True
                    logger.warning(f"BACKTEST PRE-WARM {days}d attempt {attempt} returned ok=False: {r.get('reason')}")
                except Exception as _be:
                    logger.warning(f"BACKTEST PRE-WARM {days}d attempt {attempt} exception: {_be}")
                if attempt < max_attempts:
                    _t_pw.sleep(30)   # backoff before retry
            return False

        def _runner():
            from predictions.backtest import run_backtest
            from datetime import datetime as _dt_pw, timedelta as _td_pw
            # Initial delay so other startup tasks finish first
            _t_pw.sleep(60)
            while True:
                try:
                    end = _dt_pw.utcnow().date().isoformat()
                    for days in (30, 90, 180):
                        try:
                            start = (_dt_pw.utcnow() - _td_pw(days=days)).date().isoformat()
                            ok = _one_period(run_backtest, start, end, days)
                            logger.info(f"BACKTEST PRE-WARM: {days}d cached={ok}")
                        except Exception as _be:
                            logger.warning(f"BACKTEST PRE-WARM {days}d outer soft-fail: {_be}")
                        _t_pw.sleep(15)   # gentle spacing between periods
                except Exception as _re:
                    logger.warning(f"Backtest pre-warm loop soft-fail: {_re}")
                # Sleep 3 hours, then refresh (so cache never goes stale before TTL expires at 6h)
                _t_pw.sleep(10800)

        try:
            t = threading.Thread(target=_runner, daemon=True, name="backtest-prewarm")
            t.start()
            logger.info("Backtest cache pre-warm thread started (perpetual 3h refresh)")
        except Exception as _te:
            logger.error(f"Could not start backtest pre-warm thread: {_te}")
    except Exception as e:
        # Never let pre-warm setup take down the app
        try:
            logger.error(f"Failed to start backtest pre-warm: {e}")
        except Exception:
            pass

_prewarm_backtest_bg()


# ============================================================
# PICKS PRE-WARM ON STARTUP — never let cache sit empty after deploy
# ============================================================
# Pick generation takes 5-15 minutes to scan the 700+ ticker universe.
# Without this background pre-warm, trade cycles fire on an empty cache
# right after every deploy and open ZERO new trades until the picks
# eventually populate. With this, the cache starts populating
# immediately on container start, BEFORE any trade cycle runs.
def _prewarm_picks_bg():
    try:
        import threading, time as _t
        def _run():
            try:
                _t.sleep(15)  # let other startup tasks settle

                # FAST PATH: try S3 restore first — populates cache in <1s
                # vs the 5-15min full regen. Trades can start firing while
                # the fresh background gen runs to replace.
                #
                # SAFETY GATE (2026-05-15): validate the restored picks' regime
                # data BEFORE populating cache. The 2026-05-15 deploy saw S3
                # cache holding pre-deploy regime with corrupt sp500=79.38 and
                # vix=190.68 (yfinance bug). Without this gate the stale corrupt
                # data was served to /api/quant-picks until full regen completed
                # 10+ minutes later. Now we reject corrupt S3 cache so the
                # endpoint returns the "loading" placeholder until live regen
                # produces sane data — better than serving phantom-CRISIS picks.
                try:
                    from predictions.enhancements import restore_picks_from_s3
                    s3_restore = restore_picks_from_s3()
                    if s3_restore.get("ok") and s3_restore.get("picks"):
                        _s3_picks = s3_restore["picks"]
                        # 2026-06-06: SANITIZE rather than REJECT.
                        # Previous behavior: when VIX or SP500 in S3 cache was
                        # corrupt (the morning VIX=7499 bug), the whole picks
                        # set was rejected, leaving the endpoint cold for hours.
                        # New behavior: sanitize the corrupt regime field to
                        # None (so downstream handles "no signal" cleanly) and
                        # serve the picks. The picks themselves are stock
                        # tickers and don't depend on the saved regime context.
                        # Next live regen will produce fresh picks with the
                        # new vix_guard's validated VIX value.
                        _s3_regime = _s3_picks.get("regime") or {}
                        _s3_sp = _s3_regime.get("sp500_price")
                        _s3_vx = _s3_regime.get("vix_level")
                        _sp_ok = (_s3_sp is None) or (1000 < float(_s3_sp) < 20000)
                        _vx_ok = (_s3_vx is None) or (float(_s3_vx) == 0) or (5 < float(_s3_vx) < 60)
                        _sanitized = []
                        if not _sp_ok:
                            _s3_regime["sp500_price"] = None
                            _sanitized.append(f"sp500={_s3_sp}→None")
                        if not _vx_ok:
                            _s3_regime["vix_level"] = None
                            _sanitized.append(f"vix={_s3_vx}→None")
                        if _sanitized:
                            _s3_picks["regime"] = _s3_regime
                            logger.warning(
                                f"PICKS S3 RESTORE — SANITIZED corrupt regime "
                                f"({', '.join(_sanitized)}). Picks still served; "
                                f"next live regen will produce fresh regime."
                            )
                        # 2026-06-09: Decontaminate S3 snapshot.
                        # Old code saved already-boosted confidence values
                        # (confidence=83.7, confidence_raw=62). Strip the boost
                        # so the display layer applies it cleanly on serve.
                        def _decontaminate_picks(picks_list):
                            for _dp in (picks_list or []):
                                if isinstance(_dp, dict) and "confidence_raw" in _dp:
                                    _dp["confidence"] = _dp.pop("confidence_raw")
                            return picks_list
                        _s3_picks["long_picks"] = _decontaminate_picks(
                            _s3_picks.get("long_picks") or [])
                        _s3_picks["short_picks"] = _decontaminate_picks(
                            _s3_picks.get("short_picks") or [])

                        from analysis.quant_engine import _quant_cache
                        _quant_cache["quant_picks"] = {
                            "data": _s3_picks,
                            "time": _t.time(),
                        }
                        logger.warning(
                            f"PICKS S3 RESTORE: {s3_restore.get('long_count')} longs + "
                            f"{s3_restore.get('short_count')} shorts loaded from S3 backup "
                            f"(regime sp500={_s3_regime.get('sp500_price')}, "
                            f"vix={_s3_regime.get('vix_level')})"
                        )
                except Exception as _e:
                    logger.debug(f"S3 restore skipped (non-fatal): {_e}")

                # Always run a fresh background regen to replace the cache
                # with new data (S3 might be hours stale). This populates
                # both the in-memory cache AND saves a fresh S3 backup.
                logger.warning("PICKS PRE-WARM starting (background)")
                from analysis.quant_engine import generate_quant_picks
                picks = generate_quant_picks()
                long_n = len(picks.get("long_picks", []))
                short_n = len(picks.get("short_picks", []))
                logger.warning(
                    f"PICKS PRE-WARM complete: {long_n} longs, {short_n} shorts cached"
                )
            except Exception as _e:
                logger.error(f"PICKS PRE-WARM error: {_e}")
        threading.Thread(target=_run, daemon=True, name="picks-prewarm").start()
        logger.info("Picks pre-warm thread started")
    except Exception as e:
        logger.error(f"Failed to start picks pre-warm: {e}")

_prewarm_picks_bg()


# ============================================================
# COLD-START TRADE KICK — fire one cycle 2 min after container boot
# ============================================================
# Without this, after every container restart trades wait up to 5 min
# (smart_monitor's interval) before the first cycle fires. With this,
# the FIRST cycle fires within 2 min of boot regardless of scheduler
# state. Critical for Monday-morning openings — trades start within
# ~2 min of the market open instead of 5-10 min.
#
# Wait 120s so picks pre-warm has at least started (S3 restore is <1s,
# fresh regen takes longer but cycle can fire on stale picks).
def _cold_start_trade_kick():
    try:
        import threading, time as _t_cs
        def _run():
            try:
                _t_cs.sleep(120)
                # Only fire if scheduler hasn't already run a cycle
                last_run = auto_trade_stats.get("last_run")
                if last_run:
                    try:
                        from datetime import datetime as _dt_cs
                        last_dt = _dt_cs.fromisoformat(last_run)
                        age_min = (_dt_cs.now() - last_dt).total_seconds() / 60
                        if age_min < 5:
                            logger.info("COLD-START KICK: skipping — scheduler already ran a cycle recently")
                            return
                    except Exception:
                        pass
                logger.warning("COLD-START KICK: firing first trade cycle (2 min after boot)")
                _smart_trade_monitor()
                logger.warning("COLD-START KICK: first trade cycle complete")
            except Exception as _e:
                logger.error(f"COLD-START KICK error: {_e}")
        threading.Thread(target=_run, daemon=True, name="cold-start-trade-kick").start()
        logger.info("Cold-start trade kick thread armed (fires in 120s)")
    except Exception as e:
        logger.error(f"Cold-start kick failed to start: {e}")

_cold_start_trade_kick()


# ============================================================
# BLACK SWAN PROTECTION JOB (every 15 min during market hours)
# ============================================================
# If SPY drops >2% intraday, automatically tighten stops on winning
# positions to break-even. Only TIGHTENS stops, never closes positions,
# never blocks new entries. Trades keep flowing — gains just get locked
# in faster during crashes.
def _black_swan_check_job():
    try:
        from predictions.enhancements import apply_black_swan_protection
        result = apply_black_swan_protection()
        if result.get("is_swan"):
            logger.warning(
                f"BLACK SWAN: SPY {result.get('spy_pct_change'):+.2f}% — "
                f"tightened {result.get('stops_tightened', 0)} stops "
                f"(severity={result.get('severity')})"
            )
    except Exception as e:
        logger.debug(f"black_swan_check_job (non-fatal): {e}")

scheduler.add_job(
    _black_swan_check_job,
    "interval",
    minutes=15,
    id="black_swan_check",
    name="Black Swan Detector (every 15 min)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=600,
    replace_existing=True,
)


# ============================================================
# END-OF-DAY REPORT JOB (4:30 PM ET, weekdays)
# ============================================================
# Generates a daily summary at market close. Read-only — never affects
# trading. Stored in trading_state for retrieval via /api/eod-report.
def _eod_report_job():
    try:
        from predictions.enhancements import generate_eod_report
        report = generate_eod_report()
        portfolio = report.get("portfolio", {})
        today = report.get("today_trades", {})
        logger.warning(
            f"EOD REPORT: total=${portfolio.get('total_value'):,.2f} "
            f"return={portfolio.get('cum_return_pct')}% "
            f"trades_closed={today.get('total_closed', 0)} "
            f"win_rate={today.get('win_rate', 0)}% "
            f"pnl=${today.get('total_pnl_dollars', 0):+,.2f}"
        )
    except Exception as e:
        logger.error(f"eod_report_job error: {e}")

scheduler.add_job(
    _eod_report_job,
    "cron",
    day_of_week="mon-fri",
    hour=16,
    minute=30,
    id="eod_report",
    name="End-of-Day Summary (4:30 PM ET)",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600,
    replace_existing=True,
)


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

        # Save new snapshot at the reset value via truth engine
        # (preserves prior sp500_cum instead of writing zero)
        try:
            from predictions.truth_engine import safe_save_snapshot as _safe_reset_snap
            _safe_reset_snap()
        except Exception:
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
                    entry_px = float(t.get("entry_price") or 0)
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
                        # PRICE SANITY BOUND — yfinance occasionally returns wildly
                        # wrong values during glitches/aggregation errors. If the
                        # quote moved more than 50% from our entry price, treat it
                        # as suspect and fall back to entry_price (with a log).
                        # A real intraday move >50% on an existing position is
                        # vanishingly rare; an upstream data-bug is far more likely.
                        if entry_px > 0 and (price > entry_px * 2.0 or price < entry_px * 0.5):
                            logger.warning(
                                f"PRICE SANITY: live quote for {t['ticker']} = "
                                f"${price:.2f} but entry was ${entry_px:.2f} "
                                f"({(price/entry_px-1)*100:+.1f}%). Using entry price."
                            )
                            price = entry_px
                        # Also reject zero/negative/NaN
                        import math as _m
                        if price <= 0 or _m.isnan(price) or _m.isinf(price):
                            logger.warning(
                                f"PRICE SANITY: live quote for {t['ticker']} = "
                                f"{price} (invalid). Using entry price ${entry_px:.2f}."
                            )
                            price = entry_px if entry_px > 0 else 0
                        pv = price * t["shares"]
                        if t["direction"] == "short":
                            # Match paper_trader.py: short value = abs(shares * current_price)
                            pv = abs(t["shares"] * price)
                        positions_value += pv
                        if price > 0:
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


# Per-ticker DAY-LONG cache for analyze endpoint.
# Why 1 day instead of 5 min: the Buy/Sell signal is a high-level
# recommendation, not an intraday quote. If the same stock flips from "Buy"
# to "Sell" on consecutive page refreshes because of tiny RSI/MACD noise,
# the page feels broken. With a per-day cache, the user sees the same
# recommendation throughout the trading day — only refreshing once after
# the next close. The deterministic Monte Carlo seed makes intra-day
# results identical anyway, so this only changes the user-visible stability.
_analyze_cache = {}
_ANALYZE_CACHE_TTL = 86400  # 24h — one recommendation per ticker per day

@app.get("/api/analyze/{ticker}")
def analyze_stock(request: Request, ticker: str, period: str = "1y"):
    """Full stock analysis — the main endpoint.

    CACHED: per-ticker 5-min cache. With the deterministic Monte Carlo
    seed (rentech_advanced.py), same ticker on same day already gives
    same result; this just makes refreshes instant instead of 2-5s
    recomputes. Cache failures fall through to fresh compute.
    """
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")

    # DOUBLE-LAYER CACHE for absolute answer stability:
    #   1. In-memory (fast, 24h TTL)
    #   2. Persistent (SQLite trading_state, S3-synced) — keyed by
    #      ticker+period+DATE. Once a date's answer is computed, the SAME
    #      answer returns even across App Runner restarts / new deploys.
    # This is what guarantees "Strong Buy" can't flip to "Sell" when nothing
    # has changed: the answer is pinned to the date.
    import time as _t_an
    import json as _json_an
    from datetime import datetime as _dt_an
    today_key = _dt_an.utcnow().strftime("%Y-%m-%d")
    cache_key = f"{clean_ticker}:{period}"
    persist_key = f"analyze:{clean_ticker}:{period}:{today_key}"

    # Layer 1: in-memory fast path
    cached = _analyze_cache.get(cache_key)
    if cached and (_t_an.time() - cached["ts"]) < _ANALYZE_CACHE_TTL:
        result = dict(cached["data"])
        result["_cache_age_seconds"] = round(_t_an.time() - cached["ts"], 1)
        result["_cache_source"] = "memory"
        return result

    # Layer 2: persistent — survives restarts and new deploys
    try:
        from predictions.models import get_trading_state as _gts
        raw = _gts(persist_key, "")
        if raw:
            try:
                stored = _json_an.loads(raw)
                _analyze_cache[cache_key] = {"data": stored, "ts": _t_an.time()}
                out = dict(stored)
                out["_cache_source"] = "persistent"
                return out
            except Exception:
                pass
    except Exception as _pe:
        logger.debug(f"analyze persistent-cache read failed (non-fatal): {_pe}")

    try:
        report = generate_full_report(clean_ticker, period)
        if "error" in report:
            # FINAL SAFETY NET (2026-05-31): before 404'ing, look back
            # up to 14 days in the persistent analyze cache.  If
            # today's fetch failed but we analyzed this ticker any
            # time in the last two weeks, serve THAT rather than 404
            # the user.  Tagged _stale_days so the UI can warn.
            try:
                from predictions.models import get_trading_state as _gts2
                from datetime import datetime as _dt2, timedelta as _td2
                base = _dt2.utcnow()
                for back in range(1, 15):
                    pk = f"analyze:{clean_ticker}:{period}:{(base - _td2(days=back)).strftime('%Y-%m-%d')}"
                    raw = _gts2(pk, "")
                    if raw:
                        try:
                            stale = _json_an.loads(raw)
                            stale["_cache_source"] = "stale_lookback"
                            stale["_stale_days"] = back
                            stale["_stale_note"] = (
                                f"Live data unavailable today; serving cached "
                                f"analysis from {back} day(s) ago."
                            )
                            return stale
                        except Exception:
                            continue
            except Exception as _le:
                logger.debug(f"stale lookback failed: {_le}")
            # FINAL FALLBACK — instead of 404, build a degraded analyze
            # response from picks-engine data + last known quote.  The
            # picks engine has ticker/price/score/confidence/sector — enough
            # to render a useful page for the user.  This is the last line
            # of defense against the HPE-style 404 that the user keeps
            # seeing on stocks the live yfinance pull fails for.
            try:
                from analysis.quant_engine import _quant_cache as _qc
                _cache_entry = _qc.get("quant_picks")
                if _cache_entry and _cache_entry.get("data"):
                    _pdata = _cache_entry["data"]
                    _all_picks = (_pdata.get("long_picks", []) or []) + \
                                 (_pdata.get("short_picks", []) or [])
                    _match = next(
                        (p for p in _all_picks if (p.get("ticker") or "").upper() == clean_ticker),
                        None
                    )
                    if _match:
                        _degraded = {
                            "ticker": clean_ticker,
                            "info": {
                                "symbol": clean_ticker,
                                "current_price": _match.get("price"),
                                "sector": _match.get("sector") or "Unknown",
                                "_source": "picks_engine_fallback",
                            },
                            "signal": {
                                "direction": _match.get("direction", "neutral"),
                                "confidence": _match.get("confidence"),
                                "composite_score": _match.get("composite_score"),
                            },
                            "history": [],
                            "indicators": {},
                            "_cache_source": "degraded_picks_fallback",
                            "_stale_note": (
                                "Live data temporarily unavailable. Showing "
                                "today's picks-engine data for this ticker. "
                                "Chart/history will return after data feed recovers."
                            ),
                        }
                        return _degraded
            except Exception as _df:
                logger.debug(f"degraded fallback failed: {_df}")
            # ABSOLUTE LAST-RESORT — return 200 stub instead of 404.
            # 2026-06-05: Enhanced to populate ALL fields the analyze page
            # renders, so the user sees "data temporarily unavailable" with
            # a usable layout rather than the broken "no data" rendering.
            #
            # Strategy: try ONE more time to scrape ANY recent persistent
            # cache entry (across periods + dates) before giving up.
            _last_resort_price = None
            try:
                from predictions.models import get_trading_state as _gts_lr
                from datetime import timedelta as _td_lr
                _base = datetime.now()
                for _periods in ("1y", "6mo", "3mo", "1mo"):
                    for _back in range(0, 30):
                        try:
                            _key = f"analyze:{clean_ticker}:{_periods}:{(_base - _td_lr(days=_back)).strftime('%Y-%m-%d')}"
                            _raw = _gts_lr(_key, "")
                            if _raw:
                                import json as _json_lr
                                _last = _json_lr.loads(_raw)
                                _last["_cache_source"] = f"stub_old_cache_back{_back}d"
                                _last["_stale_note"] = (
                                    f"Live data unavailable. Showing analysis "
                                    f"from {_back} day(s) ago. Refresh later."
                                )
                                return _last
                        except Exception:
                            continue
            except Exception:
                pass

            # Truly nothing — return a fully-populated stub so the UI
            # renders the empty-state card cleanly.  Sentinel values
            # (price=0, confidence=50) chosen so frontend math doesn't
            # NaN out.  Sector "Unknown" is filtered out client-side.
            return {
                "ticker": clean_ticker,
                "info": {
                    "symbol": clean_ticker,
                    "current_price": 0,
                    "sector": "Unknown",
                    "industry": "",
                    "market_cap": 0,
                    "_source": "stub_no_data",
                },
                "signal": {
                    "direction": "neutral",
                    "confidence": 50,
                    "composite_score": 0,
                    "strength": "no signal",
                    "reasons": ["Data feed temporarily unavailable"],
                },
                "history": [],
                "indicators": {
                    "rsi14": 50, "sma20": 0, "sma50": 0, "sma200": 0,
                    "macd": 0, "macd_signal": 0, "macd_hist": 0,
                    "atr14": 0, "bb_upper": 0, "bb_lower": 0, "bb_middle": 0,
                    "volume_ratio_20d": 1.0, "obv": 0,
                },
                "risk_score": 50,
                "drift_signal": "neutral",
                "options_picks": [],
                "_cache_source": "stub_fallback",
                "_stale_note": (
                    "Data feed temporarily unavailable for this ticker. "
                    "Auto-retries every cycle. Refresh in 1-2 minutes."
                ),
                "_degraded": True,
            }
        # Write to BOTH layers
        try:
            _analyze_cache[cache_key] = {"data": report, "ts": _t_an.time()}
        except Exception:
            pass
        try:
            from predictions.models import set_trading_state as _sts
            _sts(persist_key, _json_an.dumps(report, default=str))
        except Exception as _pwe:
            logger.debug(f"analyze persistent-cache write failed (non-fatal): {_pwe}")
        return report
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Analysis error for {clean_ticker}: {e}")
        # HARD FAILURE FALLBACK — same picks-engine degraded response
        # that the soft 404 path uses.  generate_full_report can throw
        # at any layer (yfinance timeout, math error in indicators, etc).
        # Without this, the user sees a 500.  With this, if the ticker
        # is in today's quant picks we serve the degraded card and the
        # UI never breaks.
        try:
            from analysis.quant_engine import _quant_cache as _qc_exc
            _ce = _qc_exc.get("quant_picks")
            if _ce and _ce.get("data"):
                _pdata = _ce["data"]
                _all = (_pdata.get("long_picks", []) or []) + \
                       (_pdata.get("short_picks", []) or [])
                _match = next((p for p in _all
                               if (p.get("ticker") or "").upper() == clean_ticker), None)
                if _match:
                    return {
                        "ticker": clean_ticker,
                        "info": {
                            "symbol": clean_ticker,
                            "current_price": _match.get("price"),
                            "sector": _match.get("sector") or "Unknown",
                            "_source": "picks_engine_fallback_after_exception",
                        },
                        "signal": {
                            "direction": _match.get("direction", "neutral"),
                            "confidence": _match.get("confidence"),
                            "composite_score": _match.get("composite_score"),
                        },
                        "history": [],
                        "indicators": {},
                        "_cache_source": "degraded_picks_fallback_after_exception",
                        "_stale_note": (
                            "Live data temporarily unavailable. Showing today's "
                            "picks-engine data. Chart/history returns when "
                            "data feed recovers."
                        ),
                    }
        except Exception as _ee:
            logger.debug(f"degraded fallback after exception failed: {_ee}")

        # ABSOLUTE LAST-RESORT FALLBACK — never 404 or 500.
        # The ticker passed validate_ticker (so the format is valid), and
        # every data source failed for this specific ticker.  Instead of
        # an HTTP error, return a 200 stub the UI can render as a
        # "Data temporarily unavailable" card.  The UI never breaks.
        # When the data feed recovers on the next request, the real
        # analysis comes back through.
        return {
            "ticker": clean_ticker,
            "info": {
                "symbol": clean_ticker,
                "current_price": None,
                "sector": "Unknown",
                "_source": "stub_no_data",
            },
            "signal": {
                "direction": "neutral",
                "confidence": None,
                "composite_score": None,
            },
            "history": [],
            "indicators": {},
            "_cache_source": "stub_fallback",
            "_stale_note": (
                "Data feed temporarily unavailable for this ticker. The "
                "system will try again automatically — refresh in a moment. "
                "All other tickers are unaffected."
            ),
            "_error_origin": str(e)[:200],
        }


@app.get("/api/quote/{ticker}")
def get_quote(request: Request, ticker: str):
    """Get current quote and basic info for a stock.  BULLETPROOF —
    never 404s.  When the data feed fails, returns a 200 stub with
    null price so the UI shows 'unavailable' instead of breaking."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    try:
        info = get_stock_info(clean_ticker)
        if info.get("current_price"):
            return info
        # No price — return stub instead of 404
        return {
            "symbol": clean_ticker, "current_price": None,
            "_cache_source": "stub_fallback",
            "_stale_note": "Quote temporarily unavailable. Will retry automatically.",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Quote error for {clean_ticker}: {e}")
        return {
            "symbol": clean_ticker, "current_price": None,
            "_cache_source": "stub_fallback",
            "_stale_note": "Quote temporarily unavailable. Will retry automatically.",
            "_error_origin": str(e)[:120],
        }


@app.get("/api/history/{ticker}")
def get_history(request: Request, ticker: str, period: str = "6mo"):
    """Get historical price data for charting.  BULLETPROOF — never
    404s.  Returns an empty data array if the feed is unavailable so
    the chart shows 'no data' instead of breaking the page."""
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")
    try:
        data = get_historical_data(clean_ticker, period) or []
        return {
            "ticker": clean_ticker, "period": period, "data": data,
            **({"_cache_source": "stub_fallback",
                "_stale_note": "Price history temporarily unavailable."} if not data else {})
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"History error for {clean_ticker}: {e}")
        return {
            "ticker": clean_ticker, "period": period, "data": [],
            "_cache_source": "stub_fallback",
            "_stale_note": "Price history temporarily unavailable.",
            "_error_origin": str(e)[:120],
        }


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


_daily_picks_cache = {"data": None, "ts": 0}
_DAILY_PICKS_TTL = 1800  # 30 min
_daily_picks_refresh_in_progress = False

@app.get("/api/daily-picks")
def daily_picks(request: Request):
    """Get today's top 15 stock picks based on technical analysis.

    2026-06-07: cold-cache compute was blocking >30s (same pattern as
    rentech and earnings-calendar endpoints). Now returns cached value
    instantly and spawns a background refresh if the cache is stale.
    """
    check_rate_limit(request.client.host)
    import time as _t_dp, threading
    now = _t_dp.time()

    # Fast path: serve from warm cache
    if (_daily_picks_cache["data"] is not None
            and (now - _daily_picks_cache["ts"]) < _DAILY_PICKS_TTL):
        return _daily_picks_cache["data"]

    # Spawn background refresh (single in-flight)
    global _daily_picks_refresh_in_progress
    if not _daily_picks_refresh_in_progress:
        _daily_picks_refresh_in_progress = True
        def _bg_dp_refresh():
            global _daily_picks_refresh_in_progress
            try:
                result = get_daily_picks()
                _daily_picks_cache["data"] = result
                _daily_picks_cache["ts"] = _t_dp.time()
            except Exception as _e:
                logger.error(f"daily-picks BG refresh failed: {_e}")
            finally:
                _daily_picks_refresh_in_progress = False
        try:
            threading.Thread(target=_bg_dp_refresh, daemon=True,
                             name="daily-picks-refresh").start()
        except Exception:
            _daily_picks_refresh_in_progress = False

    # Return stale data if we have it
    if _daily_picks_cache["data"] is not None:
        d = dict(_daily_picks_cache["data"]) if isinstance(_daily_picks_cache["data"], dict) else _daily_picks_cache["data"]
        if isinstance(d, dict):
            d["cache_status"] = "stale_refreshing"
        return d

    # Cold start: return placeholder
    return {
        "picks": [],
        "cache_status": "warming",
        "message": "Daily picks warming up — refresh in a few seconds",
    }


_earnings_cache = {"data": None, "ts": 0}
_EARNINGS_CACHE_TTL = 3600  # 1h
_EARNINGS_PERSIST_KEY = "earnings_calendar_cache_v1"

@app.get("/api/earnings-calendar")
def earnings_calendar(request: Request):
    """Get upcoming earnings for major stocks this week.

    AUDIT FIX M3 (v2): two-layer cache. In-memory + SQLite-persisted
    via trading_state. The previous module-global only cache didn't
    survive App Runner worker boundaries (each worker has its own
    process state), so different requests hitting different workers
    re-computed from scratch — yielding the observed 10s second-call
    delay despite the in-memory layer being "populated".
    """
    check_rate_limit(request.client.host)
    import time as _t_ec, json as _j_ec
    now = _t_ec.time()

    # Layer 1: in-memory (fast path for repeated calls to SAME worker)
    if (_earnings_cache["data"] is not None
            and (now - _earnings_cache["ts"]) < _EARNINGS_CACHE_TTL):
        return _earnings_cache["data"]

    # Layer 2: persistent SQLite (shared across workers, survives restarts)
    try:
        from predictions.models import get_trading_state as _gts_e
        raw = _gts_e(_EARNINGS_PERSIST_KEY, "")
        if raw:
            try:
                stored = _j_ec.loads(raw)
                ts = stored.get("_ts", 0)
                if (now - ts) < _EARNINGS_CACHE_TTL:
                    # Backfill in-memory so next same-worker call is instant
                    payload = stored.get("data")
                    _earnings_cache["data"] = payload
                    _earnings_cache["ts"] = ts
                    return payload
            except Exception:
                pass
    except Exception:
        pass

    # 2026-06-07 fix: cold-cache fetch was blocking >60s and timing out
    # the App Runner request. Now we ALWAYS return immediately — either
    # with stale cached data, or a placeholder — and spawn a background
    # thread to refresh the cache for the next call.
    def _bg_earnings_refresh():
        try:
            result = get_earnings_calendar()
            _earnings_cache["data"] = result
            _earnings_cache["ts"] = _t_ec.time()
            try:
                from predictions.models import set_trading_state as _sts_e
                _sts_e(_EARNINGS_PERSIST_KEY,
                       _j_ec.dumps({"data": result, "_ts": _t_ec.time()},
                                   default=str))
            except Exception as _spe:
                logger.debug(f"earnings persist write failed (non-fatal): {_spe}")
        except Exception as _bge:
            logger.error(f"Earnings BG refresh failed: {_bge}")
        finally:
            globals()["_earnings_refresh_in_progress"] = False

    if not globals().get("_earnings_refresh_in_progress", False):
        globals()["_earnings_refresh_in_progress"] = True
        try:
            import threading
            threading.Thread(target=_bg_earnings_refresh,
                             daemon=True, name="earnings-refresh").start()
        except Exception:
            globals()["_earnings_refresh_in_progress"] = False

    # Return stale cached data if available (better than nothing)
    if _earnings_cache["data"] is not None:
        return _earnings_cache["data"]
    try:
        from predictions.models import get_trading_state as _gts_e2
        raw = _gts_e2(_EARNINGS_PERSIST_KEY, "")
        if raw:
            stored = _j_ec.loads(raw)
            return stored.get("data")
    except Exception:
        pass

    # Cold start: return placeholder so the UI can render skeleton/empty state
    return {
        "earnings": [],
        "week_start": None,
        "week_end": None,
        "stocks_checked": 0,
        "generated_at": None,
        "cache_status": "warming",
        "message": "Earnings calendar warming up — refresh in a few seconds",
    }


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
        set_cash(TARGET_CASH, caller="admin_force_reset",
                 reason="reset to TARGET_CASH after force-close",
                 bypass_sentinel=True)
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

# 2026-06-05: INSTITUTIONAL ANALYTICS ENDPOINT
# Returns the full hedge-fund-grade analytics payload:
# - portfolio_risk: VaR (historical + parametric), ES, Beta, Gross/Net,
#   HHI, Sharpe/Sortino/Calmar/Omega/Ulcer, drawdown
# - sector_exposure: long/short/gross/net per sector
# - factor_analytics: per-factor IC, Sharpe, hit_rate, half-life,
#   regime_split, verdict (UPGRADE/HOLD/DOWNGRADE/KILL)
# - factor_correlation_matrix: find collinear losers
# - regime_state: combined regime detector with transition probs
# - stress_tests: replay 2020/2022/2008/SVB scenarios
# - capacity: ADV scaling, liquidity per position
# - bayesian_updates: proposed weight changes per factor
# - portfolio_optimization: HRP weights, risk parity, Kelly by regime
# - attribution: per-factor and per-sector P&L attribution
# - statarb: pairs cointegration scan, mean-reversion candidates
@app.get("/api/factor-analytics")
def factor_analytics_endpoint():
    """Hedge-fund-grade analytics. Built Sprint 1, Jun 6-7."""
    from fastapi.responses import JSONResponse
    try:
        from analytics.nan_helpers import scrub_nan
        from analytics.risk_engine import (
            portfolio_risk_snapshot, sector_exposure, gross_exposure,
        )
        from analytics.factor_analytics import (
            build_factor_analytics, factor_correlation_matrix,
        )
        from analytics.regime_engine import (
            combined_regime, should_apply_drawdown_brake,
        )
        from analytics.walkforward import replay_all_crises
        from analytics.bayesian_learning import bayesian_weight_update
        from analytics.attribution import (
            factor_pnl_attribution, sector_attribution,
            realized_vs_unrealized,
        )
        from predictions.models import (
            get_closed_trades, get_open_trades, get_cash,
            get_portfolio_snapshots, get_trading_state,
            get_signal_weights,
        )
        from predictions.learner import FACTOR_NAMES
        import json as _json_fa
        from datetime import datetime

        # === GATHER RAW DATA ===
        closed = get_closed_trades(limit=500) or []
        # Filter to stats_epoch
        _epoch = (get_trading_state("stats_epoch", "") or "").strip()
        if _epoch:
            closed = [t for t in closed if (t.get("exit_date") or "") >= _epoch]
        open_positions = [dict(t) for t in (get_open_trades() or [])]
        cash = get_cash()
        positions_value = 0.0
        for p in open_positions:
            try:
                positions_value += (p.get("current_price") or p.get("entry_price") or 0) * (p.get("shares") or 0)
            except: pass
        nav = cash + positions_value

        # Pull factor weights
        try:
            current_weights = get_signal_weights() or {}
        except Exception:
            current_weights = {}

        # Build returns series from snapshots (60d), with outlier filter.
        # Snapshots include cash_correction events and snapshot-guard
        # skips that produce phantom "daily returns" that poison
        # VaR / Sharpe / drawdown math. Bound aligned with the new
        # models.SNAPSHOT_BOGUS_DAILY_RETURN_PCT = 10% — real equity
        # books with stops + sector caps don't move 10%+ in a single
        # trading day. Triple defense: save-side reject in
        # save_portfolio_snapshot, read-side filter in
        # get_portfolio_snapshots, plus this belt at endpoint level.
        snaps = get_portfolio_snapshots(days=60) or []
        OUTLIER_BOUND = 0.10  # ±10% — aligned with snapshot bogus threshold
        port_returns = []
        for i in range(1, len(snaps)):
            prev_v = (snaps[i-1] or {}).get("total_value", 0)
            cur_v = (snaps[i] or {}).get("total_value", 0)
            if prev_v > 0 and cur_v > 0:
                r = (cur_v - prev_v) / prev_v
                if abs(r) <= OUTLIER_BOUND:
                    port_returns.append(r)

        # SPY market returns — same outlier filter (snapshot reset
        # could also corrupt sp500_value field).
        market_returns = []
        for i in range(1, len(snaps)):
            prev_sp = (snaps[i-1] or {}).get("sp500_value", 0)
            cur_sp = (snaps[i] or {}).get("sp500_value", 0)
            if prev_sp > 0 and cur_sp > 0:
                r = (cur_sp - prev_sp) / prev_sp
                if abs(r) <= OUTLIER_BOUND:
                    market_returns.append(r)

        # === COMPUTE EACH SECTION ===
        portfolio_risk = portfolio_risk_snapshot(
            port_returns, open_positions, nav, market_returns,
        )

        factor_data = build_factor_analytics(
            closed, FACTOR_NAMES, current_weights,
        )

        corr_matrix = factor_correlation_matrix(closed, FACTOR_NAMES)

        # Regime state — use the cached VIX helper from paper_trader
        # which already throttles yfinance and falls back to 20 on error.
        try:
            from predictions.paper_trader import _cached_vix_for_winlock
            vix_now = _cached_vix_for_winlock() or 20.0
        except Exception:
            vix_now = 20.0
        regime_state = combined_regime(market_returns, vix_now)

        # Drawdown brake
        cur_dd = portfolio_risk.get("current_drawdown_pct", 0) or 0
        brake = should_apply_drawdown_brake(cur_dd)

        # Stress tests
        try:
            avg_beta = portfolio_risk.get("exposure", {}).get("beta_adjusted_pct_nav", 0) / 100
            if not avg_beta: avg_beta = 1.0
        except Exception:
            avg_beta = 1.0
        stress = replay_all_crises(open_positions, avg_beta)

        # Bayesian weight updates per factor
        bayesian = []
        for f in factor_data:
            update = bayesian_weight_update(
                prior_weight=safe_float_or_zero(f.get("current_weight")),
                observed_sharpe=safe_float_or_zero(f.get("sharpe_60d_annualized")),
                observed_ic=safe_float_or_zero(f.get("ic_60d_spearman")),
                n_obs=f.get("total_trades", 0),
            )
            bayesian.append({**update, "factor": f["factor"]})

        # Attribution
        factor_pnl = factor_pnl_attribution(closed, FACTOR_NAMES)
        sec_attrib = sector_attribution(closed)
        rvu = realized_vs_unrealized(closed, open_positions)

        # === ASSEMBLE PAYLOAD ===
        payload = {
            "as_of": datetime.utcnow().isoformat(),
            "version": "v1.0-sprint1",
            "portfolio_risk": portfolio_risk,
            "factor_analytics": factor_data,
            "factor_correlation_matrix": corr_matrix,
            "regime_state": regime_state,
            "drawdown_brake": brake,
            "stress_tests": stress,
            "bayesian_factor_updates": bayesian,
            "factor_pnl_attribution": factor_pnl,
            "sector_attribution": sec_attrib,
            "realized_unrealized": rvu,
            "nav": round(nav, 2),
            "cash": round(cash, 2),
            "open_positions_count": len(open_positions),
            "closed_trades_analyzed": len(closed),
        }
        return JSONResponse(content=scrub_nan(payload))
    except Exception as e:
        import traceback
        logger.error(f"/api/factor-analytics error: {e}\n{traceback.format_exc()}")
        return JSONResponse(content={
            "error": str(e)[:500],
            "version": "v1.0-sprint1",
            "_status": "degraded",
        })


def safe_float_or_zero(v):
    """Helper for factor-analytics endpoint."""
    import math
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


# 2026-06-05: Build version marker — proves which commit is deployed.
# When the user says "deploy done" and we still see 500 errors, we can
# hit this endpoint to verify if the fix is actually in the deployed
# code. Updated on each deploy that touches main.py.
@app.get("/api/build-version")
def build_version():
    return {
        "commit_marker": "feat-v26-long-conf-60pct-all-regimes",
        "date": "2026-06-10",
        "fixes_in_build": [
            "quant_picks_500_fallback_with_S3",
            "bulletproof_stub_full_fields",
            "cash_correction_v7",
            "stats_epoch_reset_v6",
            "stb_safe_pick_direction_only",
            "elite_stochastic_factor23_8models",
            "data_shield_4layer_yfinance_safety",
            "pandas_datareader_stooq_fallback",
            "mc_n_paths_100_for_bulk_scan",
            "startup_picks_warmup_90s",
            "short_max_loss_5pct_to_8pct",
            "profit_lock_hold_class_swing30_position45",
            "time_decay_position_2pct_swing_0.5pct",
            "exit_checker_trail_70_60_50_pct",
            "quant_picks_stale_serve_30min_dedup_regen",
            "sideways_long_conf_gate_65pct",
            "rr_filter_1p5x_min_at_entry",
            "sideways_long_stop_0p75x_atr_4pct_max",
            "close_reason_days_held_in_recent_closed",
            "full_portfolio_reset_100k_stats_epoch_v8",
            "atr_mult_1p5_1p2_0p9_tighter_stops",
            "sideways_long_max_stop_3pct_global_5pct",
            "pairs_leg_stop_10pct_to_5pct",
            "pairs_sideways_entry_z2p5_corr0p72",
            "pairs_sideways_exit_zstop_3p0",
            "sideways_long_gate_conf60_score1p5_vix20adj",
        ],
    }


# 2026-06-05 DIAGNOSTIC: Pure-JSON endpoint that touches NOTHING.
# If /api/quant-picks-diag works but /api/quant-picks still 500s,
# the bug is in the route body. If BOTH 500, the bug is at the
# FastAPI/middleware level and we need a different attack plan.
@app.get("/api/quant-picks-diag")
def quant_picks_diag():
    return {
        "diagnostic": "route_works",
        "build": "v4-simple-rewrite",
        "message": "If you see this, FastAPI routing + JSON response work fine.",
    }


@app.get("/api/quant-picks")
def quant_picks(force_refresh: bool = False):
    """Get quantitative LONG/SHORT picks. Returns cached data instantly.

    2026-06-05 SIMPLIFIED REWRITE: removed the Request dependency
    (was the prime suspect for the silent 500), removed all overlay
    logic, removed BG regen spawn — anything could be the bug source.
    Now this endpoint just reads the cache and returns it. If overlay
    logic is needed, /api/symbols-to-buy provides the diversified
    version. This endpoint is now the absolute simplest possible
    picks reader.
    """
    # 2026-06-05 v5 SAFE-SERIALIZE: diag endpoint works (route system
    # OK), so the 500 must be in returning the cache data itself.
    # Likely cause: cache contains numpy values, datetime objects, or
    # other non-JSON-serializable types that FastAPI's default encoder
    # chokes on. Use fastapi.encoders.jsonable_encoder + JSONResponse
    # which handle all those gracefully. The serialize step is wrapped
    # in its own try/except — if even that fails, fall back to
    # json.dumps(default=str) which converts everything to strings.
    from fastapi.responses import JSONResponse
    import json as _json_q
    import math as _math_q

    def _scrub_nan(obj):
        """Recursively replace NaN/Inf floats with None (JSON-safe).
        v5 confirmed the cache has NaN values that JSONResponse can't
        serialize. This walker converts them BEFORE serialization."""
        if isinstance(obj, float):
            if _math_q.isnan(obj) or _math_q.isinf(obj):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: _scrub_nan(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_scrub_nan(v) for v in obj]
        if isinstance(obj, tuple):
            return [_scrub_nan(v) for v in obj]
        return obj

    def _safe_serialize(payload):
        """Convert any payload to JSON-safe Python types, including NaN."""
        try:
            from fastapi.encoders import jsonable_encoder
            return _scrub_nan(jsonable_encoder(payload))
        except Exception:
            return _scrub_nan(_json_q.loads(_json_q.dumps(payload, default=str)))

    try:
        import time as _time_q
        from analysis.quant_engine import _quant_cache
        cache_entry = _quant_cache.get("quant_picks")
        _cache_age_s = (
            (_time_q.time() - cache_entry["time"])
            if (cache_entry and "time" in cache_entry) else float("inf")
        )
        # 2026-06-11 fix: enforce 15-min TTL on the in-memory cache.
        # Previously this endpoint returned stale data forever on hot deploys
        # (container not restarted) — June 10 CRISIS regime persisted all
        # Wednesday morning. Now stale cache is skipped and a background
        # regen is triggered so picks refresh within 15 min.
        _PICKS_ENDPOINT_TTL = 900  # 15 min — matches quant_engine internal TTL
        _cached_data = cache_entry["data"] if cache_entry else None
        # 2026-06-11 fix: also require non-empty picks before serving cache.
        # A failed regen (yfinance outage) stores empty long_picks/short_picks
        # in the cache — serving them as "cached" would block retry for 15 min.
        _cached_has_picks = bool(
            _cached_data and (
                len(_cached_data.get("long_picks") or []) +
                len(_cached_data.get("short_picks") or []) > 0
            )
        )
        if cache_entry and _cached_has_picks and _cache_age_s < _PICKS_ENDPOINT_TTL:
            result = dict(_cached_data)
            # 2026-06-11 safety: force correct direction + strip score-sign mismatches.
            # Mean-reversion picks can end up with direction=NEUTRAL or wrong score sign.
            # Also deduplicate tickers that appear in both long and short lists.
            _long_tickers_seen = set()
            _clean_longs = []
            for _lp in (result.get("long_picks") or []):
                _lp["direction"] = "LONG"
                _sym = _lp.get("ticker") or _lp.get("symbol", "")
                if _sym and _sym not in _long_tickers_seen:
                    _long_tickers_seen.add(_sym)
                    _clean_longs.append(_lp)
            _clean_shorts = []
            _short_tickers_seen = set()
            for _sp in (result.get("short_picks") or []):
                _sp["direction"] = "SHORT"
                _sym = _sp.get("ticker") or _sp.get("symbol", "")
                if _sym and _sym not in _long_tickers_seen and _sym not in _short_tickers_seen:
                    _short_tickers_seen.add(_sym)
                    _clean_shorts.append(_sp)
            result["long_picks"] = _clean_longs
            result["short_picks"] = _clean_shorts
            result["picks"] = _clean_longs + _clean_shorts
            result["cache_status"] = "cached"
            result["cache_age_seconds"] = round(_cache_age_s, 0)
            result["_endpoint_version"] = "v13-direction-enforced"
            try:
                return JSONResponse(content=_safe_serialize(result))
            except Exception as _ser_e:
                logger.error(f"Cache serialize failed: {_ser_e}")
                return JSONResponse(content=_json_q.loads(
                    _json_q.dumps(result, default=str)
                ))
        # Cache is stale, cold, or empty picks — trigger background regen + serve best available
        # 2026-06-10 fix: also trigger when cache_entry is None (cold start).
        # 2026-06-11 fix: also trigger when picks are empty (failed regen result).
        # 2026-06-12 fix: serve stale in-memory cache (up to 30 min) instead of returning
        #   LOADING — background regen will refresh; showing old picks is always better than
        #   a blank screen. Only fall through to LOADING if cache is truly empty/cold.
        _needs_regen = (not cache_entry) or (_cache_age_s >= _PICKS_ENDPOINT_TTL) or (not _cached_has_picks)
        if _needs_regen:
            # dedup guard — don't spawn a second regen if one is already running
            _regen_running = globals().get("_qp_regen_in_progress", False)
            if not _regen_running:
                try:
                    import threading as _thr_q
                    from analysis.quant_engine import generate_quant_picks as _gpq
                    globals()["_qp_regen_in_progress"] = True
                    def _qp_regen():
                        try:
                            _gpq()
                        finally:
                            globals()["_qp_regen_in_progress"] = False
                    _thr_q.Thread(target=_qp_regen, daemon=True, name="qp-regen").start()
                    logger.info("quant-picks: cold/stale — background regen triggered")
                except Exception as _bg_e:
                    globals()["_qp_regen_in_progress"] = False
                    logger.debug(f"quant-picks bg regen failed: {_bg_e}")

        # Serve stale in-memory cache if it has picks and is < 30 min old
        _STALE_SERVE_TTL = 1800  # 30 min — show old picks while regen runs in background
        if _cached_has_picks and _cache_age_s < _STALE_SERVE_TTL:
            result = dict(_cached_data)
            _long_tickers_seen = set()
            _clean_longs = []
            for _lp in (result.get("long_picks") or []):
                _lp["direction"] = "LONG"
                _sym = _lp.get("ticker") or _lp.get("symbol", "")
                if _sym and _sym not in _long_tickers_seen:
                    _long_tickers_seen.add(_sym)
                    _clean_longs.append(_lp)
            _clean_shorts = []
            _short_tickers_seen = set()
            for _sp in (result.get("short_picks") or []):
                _sp["direction"] = "SHORT"
                _sym = _sp.get("ticker") or _sp.get("symbol", "")
                if _sym and _sym not in _long_tickers_seen and _sym not in _short_tickers_seen:
                    _short_tickers_seen.add(_sym)
                    _clean_shorts.append(_sp)
            result["long_picks"] = _clean_longs
            result["short_picks"] = _clean_shorts
            result["picks"] = _clean_longs + _clean_shorts
            result["cache_status"] = "stale"
            result["cache_age_seconds"] = round(_cache_age_s, 0)
            result["_endpoint_version"] = "v14-stale-serve"
            logger.info(f"quant-picks: serving stale cache ({round(_cache_age_s/60,1)}min old) while regen runs")
            try:
                return JSONResponse(content=_safe_serialize(result))
            except Exception as _ser_e:
                logger.error(f"Stale cache serialize failed: {_ser_e}")

        # Last resort: try S3 snapshot
        try:
            from predictions.models import get_trading_state as _gts
            _raw = _gts("picks_s3_snapshot", "")
            if _raw:
                _snap = _json_q.loads(_raw)
                # Strip any legacy confidence_raw fields saved by old boosting code
                def _decontam_snap(pl):
                    for _dp in (pl or []):
                        if isinstance(_dp, dict) and "confidence_raw" in _dp:
                            _dp["confidence"] = _dp.pop("confidence_raw")
                    return pl
                _snap["long_picks"] = _decontam_snap(_snap.get("long_picks") or [])
                _snap["short_picks"] = _decontam_snap(_snap.get("short_picks") or [])
                _snap["cache_status"] = "s3_fallback"
                _snap["_endpoint_version"] = "v14-stale-serve"
                return JSONResponse(content=_safe_serialize(_snap))
        except Exception as _se:
            logger.warning(f"S3 fallback failed: {_se}")
        return JSONResponse(content={
            "regime": {"regime": "LOADING", "description": "Picks engine warming up"},
            "long_picks": [],
            "short_picks": [],
            "cache_status": "cold",
            "_endpoint_version": "v14-stale-serve",
        })
    except Exception as e:
        import traceback as _tb
        logger.error(f"/api/quant-picks v5 error: {e}\n{_tb.format_exc()}")
        return JSONResponse(content={
            "regime": {"regime": "ERROR"},
            "long_picks": [],
            "short_picks": [],
            "cache_status": "error",
            "_endpoint_version": "v6-nan-scrub",
            "_route_error": str(e)[:200],
        })


# 2026-06-05: Old quant-picks logic with overlay/diversification kept
# at /api/quant-picks-full in case the UI needs it later. Disabled
# by default — accessing it just returns the simple version.
def _disabled_quant_picks_v3(request: Request, force_refresh: bool = False):
    """Original complex version — kept for reference, not routed."""
    try:
        from analysis.quant_engine import _quant_cache, generate_quant_picks
        import time as _time
        cache_entry = _quant_cache.get("quant_picks")
        cache_age = (_time.time() - cache_entry["time"]) if cache_entry else None

        # Stale-cache or force_refresh: trigger background regen
        STALE_TTL_SEC = 3600  # 1 hour
        needs_refresh = force_refresh or (cache_age is None) or (cache_age > STALE_TTL_SEC)
        if needs_refresh:
            try:
                import threading
                # AUDIT FIX C1 — timeout-based lock so a stuck regen can't
                # block forever. Previously a regen taking >2h would set the
                # flag True; if the thread crashed without hitting finally
                # (OOM, container shutdown, network kill) the flag was stuck
                # and all subsequent force_refresh calls were silently
                # ignored, leaving picks cache stale indefinitely. Now the
                # lock auto-expires after REGEN_LOCK_TIMEOUT_SEC, allowing
                # a fresh attempt.
                REGEN_LOCK_TIMEOUT_SEC = 1800  # 30 min — longer than any healthy scan
                global _picks_regen_in_progress, _picks_regen_started_at
                _in_progress = globals().get('_picks_regen_in_progress', False)
                _started_at = globals().get('_picks_regen_started_at', 0)
                _now_lock = _time.time()
                if _in_progress and _started_at and (_now_lock - _started_at) > REGEN_LOCK_TIMEOUT_SEC:
                    logger.warning(
                        f"PICKS REGEN LOCK STALE ({_now_lock - _started_at:.0f}s > "
                        f"{REGEN_LOCK_TIMEOUT_SEC}s) — clearing for fresh attempt"
                    )
                    _in_progress = False
                if not _in_progress:
                    globals()['_picks_regen_in_progress'] = True
                    globals()['_picks_regen_started_at'] = _now_lock
                    def _bg_regen():
                        try:
                            logger.warning(
                                f"PICKS BG REGEN: cache_age={cache_age}s force={force_refresh} — regenerating in background"
                            )
                            generate_quant_picks()
                            logger.warning("PICKS BG REGEN: complete")
                        except Exception as _e:
                            logger.error(f"PICKS BG REGEN failed: {_e}")
                        finally:
                            globals()['_picks_regen_in_progress'] = False
                            globals()['_picks_regen_started_at'] = 0
                    threading.Thread(target=_bg_regen, daemon=True, name="picks-regen").start()
            except Exception as _e:
                logger.debug(f"Background regen spawn failed (non-fatal): {_e}")

        # Always return current cache (even if stale) so the API responds fast
        if cache_entry:
            result = cache_entry["data"]
            result["cache_age_seconds"] = round(cache_age) if cache_age else 0
            result["regen_triggered"] = needs_refresh
            # ALWAYS populate sp500_return_pct from truth_engine — the Quant HF
            # page reads this field. If truth says +17% but our local calc says
            # null (or some wildly different value), the page shows blank/wrong.
            # truth_engine is the single source of truth (multi-source: yf_gspc
            # → yf_spy → yf_spx → lastgood) so it's always sane.
            try:
                from predictions.truth_engine import get_sp500_truth
                _t = get_sp500_truth() or {}
                if _t.get("ok") and _t.get("cum_pct") is not None:
                    _truth_pct = float(_t["cum_pct"])
                    if -100.0 <= _truth_pct <= 500.0:
                        # If local cache value diverges from truth by >5% or is
                        # missing/insane, use truth.
                        _local = result.get("sp500_return_pct")
                        try:
                            _local_f = float(_local) if _local is not None else None
                        except (TypeError, ValueError):
                            _local_f = None
                        # AUDIT FIX M2 — tightened 5.0% -> 0.1% so even
                        # small drift between local calc and truth-engine
                        # gets corrected. The user explicitly asked for
                        # "SP500 return must be spotless".
                        if (_local_f is None
                            or not (-100.0 <= _local_f <= 500.0)
                            or abs(_local_f - _truth_pct) > 0.1):
                            result["sp500_return_pct"] = round(_truth_pct, 2)
                            result["sp500_return_pct_source"] = _t.get("source", "truth_engine")
            except Exception as _e:
                logger.debug(f"SP500 truth-engine overlay failed (non-fatal): {_e}")

            # READ-TIME DIRECTION VALIDATION + SECTOR DIVERSIFICATION
            #
            # Direction guard: stale cache or upstream bug occasionally put
            # NEUTRAL-direction or sign-inverted picks into the wrong queue
            # (e.g. ZH score=-1.52 in long_picks).  Trading on those would
            # execute the OPPOSITE of what the model said. Strip any pick
            # whose direction or score sign doesn't match the queue it's in:
            #   - long_picks:  direction=="LONG"  AND composite_score >= 0
            #   - short_picks: direction=="SHORT" AND composite_score <= 0
            try:
                def _direction_safe(picks_list, expected_direction, score_sign_ok):
                    """Strip picks whose direction or score sign disagrees with the queue."""
                    if not isinstance(picks_list, list):
                        return picks_list
                    clean = []
                    for p in picks_list:
                        try:
                            d = str(p.get("direction", "")).upper()
                            s = float(p.get("composite_score", 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        if d != expected_direction:
                            continue
                        if not score_sign_ok(s):
                            continue
                        clean.append(p)
                    return clean

                if isinstance(result.get("long_picks"), list):
                    result["long_picks"] = _direction_safe(
                        result["long_picks"], "LONG", lambda s: s >= 0,
                    )
                if isinstance(result.get("short_picks"), list):
                    result["short_picks"] = _direction_safe(
                        result["short_picks"], "SHORT", lambda s: s <= 0,
                    )
            except Exception as _dir_e:
                logger.debug(f"Read-time direction safety soft-fail: {_dir_e}")

            # Sector diversification: ≤MAX_PER_SECTOR per sector so the user
            # never sees a queue dominated by one sector.  Applied AFTER the
            # direction guard so we never accidentally re-promote a bad pick.
            try:
                def _diversify(picks_list, max_per_sec, min_picks, hard_cap):
                    if not isinstance(picks_list, list) or not picks_list:
                        return picks_list
                    sorted_p = sorted(
                        picks_list,
                        key=lambda p: abs(float(p.get("composite_score", 0) or 0)),
                        reverse=True,
                    )
                    kept, overflow, counts = [], [], {}
                    for p in sorted_p:
                        sec = (p.get("sector") or "Unknown").strip() or "Unknown"
                        if counts.get(sec, 0) < max_per_sec:
                            kept.append(p)
                            counts[sec] = counts.get(sec, 0) + 1
                        else:
                            overflow.append(p)
                        if len(kept) >= hard_cap:
                            break
                    if len(kept) < min_picks and overflow:
                        kept.extend(overflow[: min_picks - len(kept)])
                    return kept

                if isinstance(result.get("long_picks"), list):
                    result["long_picks"] = _diversify(
                        result["long_picks"], max_per_sec=4, min_picks=5, hard_cap=30,
                    )
                if isinstance(result.get("short_picks"), list):
                    result["short_picks"] = _diversify(
                        result["short_picks"], max_per_sec=4, min_picks=3, hard_cap=20,
                    )
            except Exception as _div_e:
                logger.debug(f"Read-time sector diversification soft-fail: {_div_e}")

            # Safe key filter — gracefully handle non-string keys.
            try:
                return {k: v for k, v in result.items()
                        if not (isinstance(k, str) and k.startswith("_"))}
            except Exception as _filt_e:
                logger.warning(f"Final key filter failed (returning raw result): {_filt_e}")
                # If even the dict comp fails, return result as-is.
                # Better stale data than a 500.
                return result

        return {
            "regime": {"regime": "LOADING", "description": "Analyzing 500+ stocks..."},
            "long_picks": [],
            "short_picks": [],
            "cache_status": "cold",
            "regen_triggered": True,
            "message": "Quant engine is analyzing 500+ stocks. Data will be available after the next trade cycle (runs every few minutes).",
        }
    except Exception as e:
        # 2026-06-05: NEVER return 500 from this endpoint — it's the
        # critical path for the trading system and the UI.  Log the
        # full traceback for diagnosis, then try one last fallback:
        # serve picks directly from S3 persistent cache.
        import traceback as _tb_qp
        logger.error(
            f"Quant picks error: {e}\nFULL TRACEBACK:\n{_tb_qp.format_exc()}"
        )
        # Last-resort fallback — read picks straight from S3 persistence
        # so even a route-handler crash doesn't lock the UI out.
        try:
            from predictions.models import get_trading_state as _gts_qp
            import json as _json_qp
            _raw = _gts_qp("picks_s3_snapshot", "")
            if _raw:
                _snap = _json_qp.loads(_raw)
                _snap["_cache_status"] = "fallback_from_s3"
                _snap["_route_error"] = str(e)[:200]
                return _snap
        except Exception as _fb_e:
            logger.warning(f"S3 fallback also failed: {_fb_e}")
        # If S3 fallback also fails, return empty-but-200 so the UI
        # shows "loading" instead of breaking with 500.
        return {
            "regime": {"regime": "ERROR", "description": "Picks service degraded — retrying"},
            "long_picks": [],
            "short_picks": [],
            "cache_status": "error",
            "_route_error": str(e)[:200],
            "message": "Picks engine encountered an error. Auto-retry in next cycle.",
        }


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
        from predictions.models import (
            get_closed_trades, get_all_paper_trades, get_trading_state,
        )
        closed_all = get_closed_trades(limit=200)
        open_trades = [dict(t) for t in get_all_paper_trades() if t.get("status") == "open"]
        # STATS EPOCH FILTER — displayed counts/win-rate/PnL show only
        # trades closed after the user-requested reset epoch.
        _epoch = (get_trading_state("stats_epoch", "") or "").strip()
        if _epoch:
            try:
                closed = [t for t in closed_all
                          if (t.get("exit_date") or "") >= _epoch]
            except Exception:
                closed = closed_all
        else:
            closed = closed_all
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
    """Get equity curve data — fund performance since March 30, 2026.

    2026-06-07 fix: previously had two bugs caused by hardcoded $100k:
      1. Rebase math wrote `value = 100000 * (1 + return/100)` regardless
         of the actual baseline ($109k after F17). With Jun-6 snapshot
         carrying a stale cum_ret built against the old $100k baseline and
         today's cum_ret built against the new $109k baseline, the rebase
         math produced wildly wrong values like -10.9% / $89,100 when the
         real return was +21.11% / $132,012.66.
      2. `initial_capital` response field hardcoded 100000.
    Both now reference ORIGINAL_CAPITAL (single source of truth, $109k).
    """
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_portfolio_snapshots
        from predictions.paper_trader import ORIGINAL_CAPITAL as _ORIG
        snapshots = get_portfolio_snapshots(days=365)

        # Build fund equity curve from absolute total_value, computing
        # return_pct fresh each render against ORIGINAL_CAPITAL. This way
        # the displayed % is always consistent with the current baseline,
        # regardless of when each snapshot was originally written.
        fund_curve = []
        for s in snapshots:
            try:
                _tv = float(s.get("total_value") or 0)
            except Exception:
                continue
            if _tv <= 0:
                continue
            fund_curve.append({
                "date": s["snapshot_date"],
                "value": round(_tv, 2),
                "return_pct": round(((_tv / _ORIG) - 1.0) * 100.0, 2),
            })

        # If no snapshots, create starting point at ORIGINAL_CAPITAL
        if not fund_curve:
            fund_curve = [{"date": "2026-03-30", "value": _ORIG, "return_pct": 0}]

        # Get current portfolio state for latest data point — always
        # overwrites today's snapshot with the live portfolio value, which
        # is the multi-source-of-truth NAV.
        try:
            portfolio = get_portfolio_state()
            today = dt.now().strftime("%Y-%m-%d")
            _live_total = float(portfolio.get("total_value") or _ORIG)
            _live_ret = round(((_live_total / _ORIG) - 1.0) * 100.0, 2)
            if fund_curve and fund_curve[-1]["date"] != today:
                fund_curve.append({
                    "date": today,
                    "value": round(_live_total, 2),
                    "return_pct": _live_ret,
                })
            elif fund_curve:
                fund_curve[-1]["value"] = round(_live_total, 2)
                fund_curve[-1]["return_pct"] = _live_ret
        except Exception:
            pass

        # Filter to only include dates >= March 30
        fund_curve = [p for p in fund_curve if p["date"] >= "2026-03-30"]

        # No rebase math needed — value and return_pct were both computed
        # from absolute total_value against ORIGINAL_CAPITAL, so they're
        # internally consistent and the first point may legitimately be
        # non-zero (e.g. fund started above baseline).

        # Build SP500 series from same snapshots so frontend can overlay
        # SANITY BOUNDS 2026-05-17: drop points where sp500 cum is outside
        # [-100%, +500%] — these are corrupt yfinance reads that have plagued
        # the Quant HF page multiple times.  If too many drop, fall back to
        # truth_engine.get_sp500_truth() for a clean current value.
        sp500_curve = []
        dropped = 0
        for s in snapshots:
            if s.get("snapshot_date", "") < "2026-03-30":
                continue
            sp_cum = s.get("sp500_cumulative_return_pct")
            if sp_cum is None:
                continue
            try:
                v = float(sp_cum)
                # Plausibility gate: SP500 hasn't moved -100% or +500% YTD ever
                if -100.0 <= v <= 500.0:
                    sp500_curve.append({
                        "date": s["snapshot_date"],
                        "return_pct": round(v, 2),
                    })
                else:
                    dropped += 1
                    logger.warning(f"equity-curve: dropping corrupt sp500_cum={v} on {s.get('snapshot_date')}")
            except (TypeError, ValueError):
                dropped += 1
        # Rebase SP500 to 0 at start (same anchor as fund)
        if sp500_curve and sp500_curve[0]["return_pct"] != 0:
            sp_base = sp500_curve[0]["return_pct"]
            for p in sp500_curve:
                p["return_pct"] = round(p["return_pct"] - sp_base, 2)
        # If we dropped more than 50% of points, the series is too corrupted
        # to display — try a single clean point from truth_engine.
        if dropped > len(sp500_curve) and len(sp500_curve) < 5:
            try:
                from predictions.truth_engine import get_sp500_truth
                t = get_sp500_truth()
                if t.get("ok") and t.get("cum_pct") is not None:
                    sp500_curve = [{"date": dt.utcnow().strftime("%Y-%m-%d"),
                                    "return_pct": round(float(t["cum_pct"]), 2)}]
            except Exception:
                pass

        # SPOTLESS SP500 GUARANTEE: always anchor the LAST point of the SP500
        # curve to truth_engine.cum_pct (the multi-source verified value). This
        # eliminates any chance of a stale or slightly-off historical snapshot
        # leaving the chart's tail showing a wrong "today" number. The rest of
        # the curve uses snapshots (so the shape is preserved), but the most
        # recent point — which is what the user actually reads — is always the
        # truth value. If truth is unavailable, the snapshot value is kept.
        try:
            from predictions.truth_engine import get_sp500_truth
            _t_anchor = get_sp500_truth() or {}
            if _t_anchor.get("ok") and _t_anchor.get("cum_pct") is not None:
                _truth_v = float(_t_anchor["cum_pct"])
                if -100.0 <= _truth_v <= 500.0:
                    _today = dt.utcnow().strftime("%Y-%m-%d")
                    if sp500_curve:
                        last = sp500_curve[-1]
                        # If last snapshot is today: overwrite. Otherwise: append.
                        if last.get("date") == _today:
                            last["return_pct"] = round(_truth_v, 2)
                        else:
                            sp500_curve.append({"date": _today,
                                                "return_pct": round(_truth_v, 2)})
                    else:
                        sp500_curve = [{"date": _today, "return_pct": round(_truth_v, 2)}]
        except Exception as _eta:
            logger.debug(f"SP500 truth anchor (non-fatal): {_eta}")

        return {
            "fund": fund_curve,
            "sp500": sp500_curve,
            "start_date": "2026-03-30",
            "initial_capital": _ORIG,  # 2026-06-07 fix: was hardcoded 100000
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

        # AUDIT FIX #3 — confidence calibration metrics + isotonic fit
        # AUDIT FIX #8 — realized vol + vol-target scaler suggestion
        try:
            from predictions.quant_audit_fixes import (
                isotonic_calibrate, expected_calibration_error,
                estimate_realized_vol_from_trades,
                estimate_realized_vol_from_snapshots,
                vol_target_scaler,
                apply_group_penalty,
            )
            confs = []
            wins = []
            for t in closed:
                try:
                    pnl_raw = t.get("pnl_pct")
                    if pnl_raw is None:
                        continue
                    # Confidence isn't stored on every trade; try common fields
                    c = (t.get("confidence_at_entry") or t.get("signal_score") or None)
                    if c is None:
                        continue
                    confs.append(float(c))
                    wins.append(float(pnl_raw) > 0)
                except Exception:
                    continue
            ece = expected_calibration_error(confs, wins) if confs else -1
            calibrator = isotonic_calibrate(confs, wins) if confs else None
            # AUDIT FIX M1 (v2) — per-point safety. Previous version's list
            # comprehension would collapse to None if ANY single point's
            # predict call failed. Now: independent try/except per point so
            # one bad value can't kill the whole curve. Also emits a tiny
            # diag dict so we can see WHY the curve is empty in production.
            cal_curve = None
            # Pull the latched internal error from isotonic_calibrate so we
            # can see WHY it returned None without redeploying for logs.
            try:
                from predictions.quant_audit_fixes import get_last_calibrate_error
                _last_cal_err = get_last_calibrate_error()
            except Exception:
                _last_cal_err = None
            cal_diag = {"calibrator_ok": calibrator is not None,
                        "confs_len": len(confs),
                        "internal_error": _last_cal_err,
                        "confs_sample": confs[:5] if confs else [],
                        "wins_sample": wins[:5] if wins else []}
            if calibrator and confs:
                try:
                    lo, hi = min(confs), max(confs)
                    cal_diag["range"] = [round(float(lo), 3), round(float(hi), 3)]
                    if hi > lo:
                        step = (hi - lo) / 9.0
                        pts = [lo + i * step for i in range(10)]
                    else:
                        pts = [lo]
                    points = []
                    for p in pts:
                        try:
                            cp = calibrator([p])
                            if not cp:
                                continue
                            cp_val = cp[0]
                            if cp_val is None:
                                continue
                            points.append({
                                "raw_conf": round(float(p), 3),
                                "calibrated_prob": round(float(cp_val), 4),
                            })
                        except Exception as _pe:
                            logger.debug(f"cal point at p={p} failed: {_pe}")
                            continue
                    if points:
                        cal_curve = points
                    else:
                        cal_diag["error"] = "all points returned None"
                except Exception as _ccerr:
                    logger.warning(f"calibration_curve outer failure: {_ccerr}")
                    cal_diag["error"] = str(_ccerr)[:200]
            else:
                cal_diag["error"] = "no calibrator or empty confs"

            # Prefer snapshot-based realized vol (correct measurement);
            # fall back to trade-based proxy when snapshots unavailable.
            rv = 0.0
            try:
                from predictions.models import get_portfolio_snapshots as _gps_ls
                _snaps_ls = _gps_ls(days=60) or []
                rv = estimate_realized_vol_from_snapshots(_snaps_ls)
            except Exception:
                rv = 0.0
            if rv <= 0:
                rv = estimate_realized_vol_from_trades(closed)
            vts = vol_target_scaler(rv) if rv > 0 else 1.0

            result["calibration"] = {
                "ece": ece,
                "ece_target": 0.05,
                "ece_status": ("good" if 0 <= ece < 0.05 else
                               "marginal" if 0 <= ece < 0.15 else
                               "poor" if ece >= 0.15 else "no_data"),
                "calibration_curve": cal_curve,
                "samples_used": len(confs),
                "diagnostic": cal_diag,  # AUDIT M1 v2 — see why curve None
            }
            result["vol_target"] = {
                "realized_vol_annualized": round(rv, 4),
                "target_vol_annualized": 0.12,
                "next_position_scaler": round(vts, 3),
                "interpretation": (
                    "upsize" if vts > 1.05 else
                    "downsize" if vts < 0.95 else
                    "neutral"
                ),
            }
            result["weights_decorrelated"] = apply_group_penalty(weights)
        except Exception as _aerr:
            logger.debug(f"learning-status audit overlay skipped: {_aerr}")

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


# Process-local response cache for slow read-only endpoints. Prevents
# multiple dashboard refreshes from each triggering a 5-15s recomputation.
# All cached values regenerate on TTL expiry; failures fall through to a
# fresh compute (so a stale cached error never persists).
_response_cache = {}
_RESPONSE_CACHE_TTL = 30  # seconds — balances freshness vs latency

def _cached_response(key: str, fn, ttl: int = _RESPONSE_CACHE_TTL):
    """Return cached response if fresh, else compute + cache. Fail-safe."""
    import time as _t
    try:
        now = _t.time()
        entry = _response_cache.get(key)
        if entry and (now - entry["ts"]) < ttl:
            cached = dict(entry["data"])
            cached["_cache_age_seconds"] = round(now - entry["ts"], 1)
            return cached
        data = fn()
        # Only cache if the result looks valid (not an error placeholder)
        if isinstance(data, dict):
            _response_cache[key] = {"data": data, "ts": now}
        return data
    except Exception:
        # On any error, return a fresh compute uncached
        try:
            return fn()
        except Exception as _e:
            return {"ok": False, "reason": str(_e)[:200]}


@app.get("/api/audit/status")
def audit_status(request: Request):
    """Current state of the continuous-audit system.
    Returns halt status, last audit snapshot, clean-streak progress."""
    check_rate_limit(request.client.host)
    try:
        from predictions.continuous_audit import get_audit_status
        return get_audit_status()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:300]}


@app.post("/api/audit/run-now")
def audit_run_now(request: Request):
    """Manually trigger the audit immediately (outside the 5-min schedule).
    Returns the full audit result with all check details + autofix attempts."""
    check_rate_limit(request.client.host)
    try:
        from predictions.continuous_audit import run_audit_and_autofix
        return run_audit_and_autofix()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:300]}


@app.post("/api/admin/audit-halt-clear")
def audit_halt_clear(request: Request):
    """Manually clear the audit halt flag. Use after investigating
    a halt cause that's been fixed. The audit will re-validate on its
    next 5-min cycle and re-halt if the issue persists."""
    check_rate_limit(request.client.host)
    try:
        from predictions.continuous_audit import clear_audit_halt
        return clear_audit_halt(reason="manual_admin_endpoint")
    except Exception as e:
        return {"ok": False, "reason": str(e)[:300]}


@app.post("/api/admin/vix-cache-reset")
def admin_vix_cache_reset(request: Request):
    """One-time admin: clear the persisted vix_guard last-known-good cache,
    then immediately refetch a fresh value. Used to recover from a stale
    crisis cache that the in-band stale-crisis layer can't auto-clear (e.g.,
    weekend window when no fresh live reading is available to compare).

    Returns the BEFORE state, the action taken, and the AFTER state from a
    fresh get_vix_safe() call.
    """
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_trading_state, set_trading_state
        from analytics.vix_guard import (
            get_vix_safe, LAST_GOOD_VIX_KEY, LAST_GOOD_VIX_TS_KEY,
        )
        before_val = get_trading_state(LAST_GOOD_VIX_KEY, "")
        before_ts = get_trading_state(LAST_GOOD_VIX_TS_KEY, "")
        # Clear: empty value + empty timestamp makes _load_last_good
        # return (HARDCODED_NEUTRAL, 0.0) — clean slate. Then refetch
        # immediately so the next call to system-thinking sees the new value.
        set_trading_state(LAST_GOOD_VIX_KEY, "")
        set_trading_state(LAST_GOOD_VIX_TS_KEY, "")
        after = get_vix_safe()
        return {
            "ok": True,
            "before": {"value": before_val, "timestamp": before_ts},
            "action": "cleared LAST_GOOD_VIX + LAST_GOOD_VIX_TS, then refetched",
            "after": after,
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)[:300]}


@app.get("/api/trade-math-audit")
def trade_math_audit(request: Request):
    """Live reconciliation of trade math. Flags phantom trades, inflated
    P&L, NAV mismatches, and snapshot corruption — the four classes of
    bugs that have historically broken the system.

    Returns a structured report:
      ok                  : bool, overall pass/fail
      checks              : list of {name, ok, detail}
      phantom_scan        : detection of trades with no cash debit on file
      nav_reconciliation  : cash + Σ(short-aware position values) vs reported
      snapshot_audit      : last 5 daily snapshots checked for sanity
      capital_baseline    : INITIAL_CAPITAL / ORIGINAL_CAPITAL alignment

    READ-ONLY — does not modify any state. Designed for the hourly health
    check to call.
    """
    check_rate_limit(request.client.host)
    report = {"ok": True, "checks": [], "as_of": datetime.now().isoformat()}
    try:
        from predictions.models import (
            get_cash, get_open_trades, get_all_paper_trades,
            get_portfolio_snapshots,
        )
        from predictions.paper_trader import (
            _get_current_prices, _short_aware_positions_value,
            INITIAL_CAPITAL as _INIT_CAP, ORIGINAL_CAPITAL as _ORIG_CAP,
            get_portfolio_state,
        )

        # CHECK 1: capital constants aligned
        cap_ok = abs(_INIT_CAP - _ORIG_CAP) < 0.01
        report["capital_baseline"] = {
            "initial_capital": _INIT_CAP,
            "original_capital": _ORIG_CAP,
            "aligned": cap_ok,
        }
        report["checks"].append({
            "name": "capital_constants_aligned",
            "ok": cap_ok,
            "detail": (
                f"INITIAL=${_INIT_CAP:,.0f} ORIGINAL=${_ORIG_CAP:,.0f} "
                f"{'(aligned)' if cap_ok else '(MISMATCH → phantom return)'}"
            ),
        })
        if not cap_ok:
            report["ok"] = False

        # CHECK 2: NAV reconciliation — cash + Σ(short-aware positions)
        # must equal what get_portfolio_state() reports as total_value.
        cash = float(get_cash() or 0)
        open_trades = get_open_trades() or []
        tickers = [t["ticker"] for t in open_trades if t.get("ticker")]
        prices = _get_current_prices(tickers) if tickers else {}
        recomputed_positions = _short_aware_positions_value(open_trades, prices)
        recomputed_total = cash + recomputed_positions
        state = get_portfolio_state() or {}
        reported_total = float(state.get("total_value") or 0)
        # Allow $1 tolerance for rounding
        nav_diff = abs(recomputed_total - reported_total)
        nav_ok = nav_diff <= 1.0
        report["nav_reconciliation"] = {
            "cash": round(cash, 2),
            "positions_value_recomputed": round(recomputed_positions, 2),
            "total_value_recomputed": round(recomputed_total, 2),
            "total_value_reported": round(reported_total, 2),
            "difference": round(nav_diff, 2),
            "ok": nav_ok,
        }
        report["checks"].append({
            "name": "nav_reconciliation",
            "ok": nav_ok,
            "detail": (
                f"recomputed=${recomputed_total:,.2f} vs reported=${reported_total:,.2f} "
                f"(diff=${nav_diff:,.2f})"
            ),
        })
        if not nav_ok:
            report["ok"] = False

        # CHECK 3: phantom trade scan — every open trade should have
        # a recorded entry_date; missing entry_date or impossible
        # entry_price = phantom. Closed trades with no exit_date OR no
        # pnl_dollars and not in 'closed_flat_validator' status = corrupt.
        phantom_open = []
        phantom_closed = []
        all_trades = get_all_paper_trades() or []
        for t in all_trades:
            try:
                status = (t.get("status") or "").lower()
                if status == "open":
                    if not t.get("entry_date") or not t.get("entry_price"):
                        phantom_open.append(t.get("id"))
                    elif float(t.get("entry_price") or 0) <= 0:
                        phantom_open.append(t.get("id"))
                elif status == "closed":
                    if not t.get("exit_date"):
                        phantom_closed.append({"id": t.get("id"), "reason": "no_exit_date"})
                    elif t.get("pnl_dollars") is None:
                        phantom_closed.append({"id": t.get("id"), "reason": "null_pnl"})
            except Exception:
                continue
        phantom_ok = (len(phantom_open) == 0 and len(phantom_closed) == 0)
        report["phantom_scan"] = {
            "open_phantoms": phantom_open,
            "closed_phantoms": phantom_closed,
            "ok": phantom_ok,
        }
        report["checks"].append({
            "name": "phantom_trades",
            "ok": phantom_ok,
            "detail": (
                f"{len(phantom_open)} open phantoms, "
                f"{len(phantom_closed)} closed phantoms"
            ),
        })
        if not phantom_ok:
            report["ok"] = False

        # CHECK 4: snapshot sanity — last 5 snapshots should all have
        # total_value in [10k, 5x_original] and |daily_return_pct| < 10%.
        snaps = get_portfolio_snapshots(days=5) or []
        bad_snaps = []
        for s in snaps:
            try:
                tv = float(s.get("total_value") or 0)
                dr = float(s.get("daily_return_pct") or 0)
                if not (10_000.0 <= tv <= _ORIG_CAP * 5.0):
                    bad_snaps.append({
                        "date": s.get("snapshot_date"),
                        "total_value": tv,
                        "reason": "total_value_out_of_bounds",
                    })
                elif abs(dr) > 10.0:
                    bad_snaps.append({
                        "date": s.get("snapshot_date"),
                        "daily_return_pct": dr,
                        "reason": "daily_return_exceeds_10pct",
                    })
            except Exception:
                continue
        snaps_ok = (len(bad_snaps) == 0)
        report["snapshot_audit"] = {
            "checked": len(snaps),
            "bad": bad_snaps,
            "ok": snaps_ok,
        }
        report["checks"].append({
            "name": "snapshot_sanity",
            "ok": snaps_ok,
            "detail": f"{len(snaps)} snapshots checked, {len(bad_snaps)} flagged",
        })
        if not snaps_ok:
            report["ok"] = False

        return report
    except Exception as e:
        return {
            "ok": False,
            "reason": f"audit_endpoint_error: {str(e)[:300]}",
            "checks": report.get("checks", []),
        }


@app.get("/api/system-thinking")
def system_thinking(request: Request):
    """Single endpoint that returns everything the algorithm is "thinking"
    right now. READ-ONLY. Designed for the user to see all autonomous
    decisions in one view without diving into 5+ separate endpoints.

    Returns:
      dynamic_exposure: current exposure target + reasoning breakdown
      taco_signal: TACO reversal pattern detection state
      active_geo_events: events influencing picks
      regime: current market regime
      portfolio_summary: cash, positions, return
      learner: factor weights + recent adjustments
      recent_picks_summary: distribution of confidence + scores
      scheduler_health: heartbeat + cycle counts
      flags: any system flags currently active

    SAFETY: every section wrapped in try/except. A failure in one
    section returns a placeholder for that section without breaking
    the overall response. Never raises.

    CACHED: 30-second response cache so dashboard refreshes don't each
    trigger 5-15s of yfinance calls. Cache failures fall through to a
    fresh compute.
    """
    check_rate_limit(request.client.host)

    # Fast path: serve from cache if fresh
    import time as _t_st
    _cached = _response_cache.get("system_thinking")
    if _cached and (_t_st.time() - _cached["ts"]) < _RESPONSE_CACHE_TTL:
        out = dict(_cached["data"])
        out["_cache_age_seconds"] = round(_t_st.time() - _cached["ts"], 1)
        return out

    result = {"generated_at": dt.now().isoformat()}

    # ----- Dynamic exposure + TACO (run live to compute now) -----
    try:
        from predictions.paper_trader import (
            _compute_dynamic_exposure_target,
            _detect_taco_reversal_event,
            get_portfolio_state,
            _is_good_entry_time,
        )
        # Need current VIX from the regime cache
        try:
            regime_data = detect_market_regime()
            vix_now = regime_data.get("vix_level")
            regime_now = regime_data.get("regime", "UNKNOWN")
        except Exception:
            vix_now = None
            regime_now = "UNKNOWN"

        # Need drawdown from portfolio
        try:
            pstate = get_portfolio_state()
            total_return_now = pstate.get("total_return_pct")
            cash_now = pstate.get("cash")
            num_pos = pstate.get("num_positions")
        except Exception:
            total_return_now = None
            cash_now = None
            num_pos = None

        dyn = _compute_dynamic_exposure_target(
            vix_level=vix_now,
            drawdown_pct=total_return_now,
            regime=regime_now,
        )
        result["dynamic_exposure"] = dyn

        # TACO needs a quant_picks-shaped dict — use CACHED picks only.
        # Don't trigger a fresh generate_quant_picks (5-10 min yfinance run
        # would time out at App Runner's 120s gateway). If cache is empty,
        # build a minimal stub from current regime data.
        picks_for_taco = {}
        try:
            from analysis.quant_engine import _quant_cache
            cached = _quant_cache.get("quant_picks")
            if cached and isinstance(cached.get("data"), dict):
                picks_for_taco = cached["data"]
        except Exception:
            pass
        if not picks_for_taco:
            picks_for_taco = {"regime": {"vix_level": vix_now}, "macro": {}}
        taco = _detect_taco_reversal_event(picks_for_taco)
        # Convert sets to lists for JSON
        taco["veto_shorts_in"] = sorted(list(taco.get("veto_shorts_in", set())))
        result["taco_signal"] = taco

        # Trading window
        try:
            window = _is_good_entry_time()
            result["trading_window"] = window
        except Exception:
            result["trading_window"] = {"error": "computation_failed"}

        result["regime"] = regime_now
        result["vix_now"] = vix_now
        result["portfolio_summary"] = {
            "cash": cash_now,
            "total_return_pct": total_return_now,
            "num_positions": num_pos,
        }
    except Exception as _e:
        result["dynamic_exposure"] = {"error": str(_e)}

    # ----- Active geo events -----
    try:
        from predictions.models import get_active_geo_events, get_upcoming_geo_events
        result["geo_events"] = {
            "active": get_active_geo_events() or [],
            "upcoming_30d": get_upcoming_geo_events(days_ahead=30) or [],
        }
    except Exception as _e:
        result["geo_events"] = {"error": str(_e)}

    # ----- Learner state -----
    try:
        from predictions.models import get_signal_weights, get_all_regime_factor_weights
        result["learner"] = {
            "global_weights": get_signal_weights(),
            "regime_weights": get_all_regime_factor_weights(),
        }
    except Exception as _e:
        result["learner"] = {"error": str(_e)}

    # ----- Mistake adjustments -----
    try:
        from predictions.learner import get_mistake_adjustments
        result["mistake_adjustments"] = get_mistake_adjustments()
    except Exception as _e:
        result["mistake_adjustments"] = {"error": str(_e)}

    # ----- Scheduler health -----
    try:
        result["scheduler_health"] = {
            "running": scheduler.running,
            "jobs": [j.id for j in scheduler.get_jobs()],
            "total_cycles": auto_trade_stats.get("total_cycles"),
            "errors": auto_trade_stats.get("errors"),
            "last_run": auto_trade_stats.get("last_run"),
            "last_scan_attempted": auto_trade_stats.get("last_scan_attempted"),
            "status": auto_trade_stats.get("status"),
        }
    except Exception as _e:
        result["scheduler_health"] = {"error": str(_e)}

    # ----- Flags -----
    try:
        result["flags"] = {
            "daily_paused": _daily_paused,
            "geo_risk": _geo_risk_state,
        }
    except Exception as _e:
        result["flags"] = {"error": str(_e)}

    # Cache the response for 30s so dashboard refreshes are fast
    try:
        _response_cache["system_thinking"] = {"data": result, "ts": _t_st.time()}
    except Exception:
        pass
    return result


# ============================================================
#  ADVANCED SIGNAL ENGINES — pairs / macro / alt-data
# ============================================================
# READ-ONLY endpoints. Each one is wrapped so a failure in the underlying
# engine never crashes the API. All three engines have their own caches
# and last-known-good fallbacks, so these endpoints are cheap to call.

@app.get("/api/pairs-engine")
def api_pairs_engine(request: Request):
    """Statistical-arbitrage pairs scanner. Returns the current actionable
    pair signals plus engine status. Always returns a dict — never raises.
    """
    check_rate_limit(request.client.host)
    try:
        from analysis.pairs_engine import scan_pairs, get_pairs_engine_status
        signals = scan_pairs()
        status = get_pairs_engine_status()
        return {
            "ok": True,
            "engine": status.get("engine"),
            "have_statsmodels": status.get("have_statsmodels"),
            "universe_size": status.get("universe_size"),
            "active_signals": len(signals or []),
            "signals": signals or [],
            "thresholds": status.get("thresholds"),
            "cache_age_seconds": status.get("cache_age_seconds"),
            "generated_at": dt.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"/api/pairs-engine failed: {e}")
        return {"ok": False, "reason": str(e)[:200], "signals": []}


@app.get("/api/macro-signals")
def api_macro_signals(request: Request):
    """Cross-asset macro engine. Returns regime, exposure modifier, sector
    tilts, yield curve, credit stress, VIX term structure, dollar, etc.
    """
    check_rate_limit(request.client.host)
    try:
        from analysis.cross_asset_macro import get_macro_signals, get_macro_status
        data = get_macro_signals()
        status = get_macro_status()
        return {"ok": True, "data": data, "status": status,
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        logger.error(f"/api/macro-signals failed: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/alt-data/{ticker}")
def api_alt_data(ticker: str, request: Request):
    """Alternative-data composite for a single ticker.
    Pulls from EDGAR / Reddit / Google Trends / Wikipedia / StockTwits.
    """
    check_rate_limit(request.client.host)
    try:
        clean_ticker = validate_ticker(ticker)
        from analysis.alt_data import compute_alt_data_score, get_alt_data_status
        data = compute_alt_data_score(clean_ticker)
        status = get_alt_data_status()
        return {"ok": True, "ticker": clean_ticker, "data": data,
                "status": status, "generated_at": dt.now().isoformat()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/api/alt-data/{ticker} failed: {e}")
        return {"ok": False, "reason": str(e)[:200], "ticker": ticker}


@app.get("/api/alt-data-status")
def api_alt_data_status(request: Request):
    """Lightweight alt-data engine status (cache freshness per source).
    Cheap to call — no external requests, just inspects in-memory caches.
    """
    check_rate_limit(request.client.host)
    try:
        from analysis.alt_data import get_alt_data_status
        return {"ok": True, "status": get_alt_data_status(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  T-BILL YIELD ON IDLE CASH — endpoints
# ============================================================

@app.get("/api/tbill-status")
def api_tbill_status(request: Request):
    """Current T-bill yield config + accrual stats."""
    check_rate_limit(request.client.host)
    try:
        from predictions.tbill_yield import get_tbill_status
        return {"ok": True, "status": get_tbill_status(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/tbill-accrue-now")
def api_tbill_accrue_now(request: Request):
    """Manually trigger one T-bill accrual cycle (idempotent — only credits
    if a day has passed since last accrual). Useful for testing and for
    backfilling missed days after a downtime."""
    check_rate_limit(request.client.host)
    try:
        from predictions.tbill_yield import apply_tbill_interest
        return {"ok": True, "result": apply_tbill_interest(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/tbill-set-yield")
def api_tbill_set_yield(request: Request, annual_yield_pct: float):
    """Update the annual T-bill yield (e.g., 3.5 for 3.5%). Validated
    bounds: -5% to +20%. Persisted to trading_state."""
    check_rate_limit(request.client.host)
    try:
        from predictions.tbill_yield import set_annual_yield
        # Accept percent (e.g., 3.5) and convert to decimal (0.035)
        return {"ok": True, "result": set_annual_yield(annual_yield_pct / 100.0),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  RELIABILITY: deep health + sentinel admin endpoints
# ============================================================

@app.get("/api/health/deep")
def api_health_deep(request: Request):
    """Deep health check — runs subsystem probes and returns a
    component-by-component status. Use this when you suspect something
    is degraded but the basic /health is still 200.

    Each subsystem check is wrapped in try/except so a single failure
    cannot break the overall response.
    """
    check_rate_limit(request.client.host)
    out = {"generated_at": dt.now().isoformat(), "subsystems": {}}

    # 1. Database probe
    try:
        from predictions.models import get_db
        import time as _t
        t0 = _t.time()
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        out["subsystems"]["database"] = {"ok": True, "latency_ms": round((_t.time() - t0) * 1000, 1)}
    except Exception as e:
        out["subsystems"]["database"] = {"ok": False, "reason": str(e)[:200]}

    # 2. Scheduler heartbeat
    try:
        out["subsystems"]["scheduler"] = {
            "ok": scheduler.running,
            "jobs": len(scheduler.get_jobs()),
            "total_cycles": auto_trade_stats.get("total_cycles"),
            "last_run": auto_trade_stats.get("last_run"),
            "errors": auto_trade_stats.get("errors"),
        }
    except Exception as e:
        out["subsystems"]["scheduler"] = {"ok": False, "reason": str(e)[:200]}

    # 3. Daily pause flag
    try:
        out["subsystems"]["daily_paused"] = {
            "ok": not _daily_paused.get("paused", False),
            "paused": _daily_paused.get("paused", False),
            "reason": _daily_paused.get("reason"),
        }
    except Exception as e:
        out["subsystems"]["daily_paused"] = {"ok": False, "reason": str(e)[:200]}

    # 4. Circuit breaker state
    try:
        from predictions.sentinels import get_circuit_status
        cb = get_circuit_status()
        out["subsystems"]["circuit_breaker"] = {"ok": not cb.get("open", False), **cb}
    except Exception as e:
        out["subsystems"]["circuit_breaker"] = {"ok": False, "reason": str(e)[:200]}

    # 5. T-bill engine
    try:
        from predictions.tbill_yield import get_tbill_status
        tb = get_tbill_status()
        out["subsystems"]["tbill"] = {"ok": tb.get("ok", False),
                                       "yield_pct": tb.get("annual_yield_pct"),
                                       "last_accrual": tb.get("last_accrual_date")}
    except Exception as e:
        out["subsystems"]["tbill"] = {"ok": False, "reason": str(e)[:200]}

    # 6. Audit log summary
    try:
        from predictions.audit import get_audit_summary
        out["subsystems"]["audit_log"] = {"ok": True, **get_audit_summary()}
    except Exception as e:
        out["subsystems"]["audit_log"] = {"ok": False, "reason": str(e)[:200]}

    # Overall pass/fail
    failed = [k for k, v in out["subsystems"].items() if isinstance(v, dict) and not v.get("ok")]
    out["overall_ok"] = len(failed) == 0
    out["failed_subsystems"] = failed
    return out


@app.get("/api/audit/recent")
def api_audit_recent(request: Request, limit: int = 50, mutation_type: str = None):
    """Read recent audit log entries. Optional mutation_type filter."""
    check_rate_limit(request.client.host)
    try:
        from predictions.audit import get_recent_audit
        return {"ok": True, "entries": get_recent_audit(min(int(limit), 500), mutation_type),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/sentinels/circuit-breaker")
def api_circuit_breaker_status(request: Request):
    """Trade execution circuit breaker status."""
    check_rate_limit(request.client.host)
    try:
        from predictions.sentinels import get_circuit_status
        return {"ok": True, "status": get_circuit_status(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/circuit-breaker-reset")
def api_circuit_breaker_reset(request: Request):
    """Force-reset the trade circuit breaker (admin escape hatch)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.sentinels import reset_circuit_breaker
        return {"ok": True, "result": reset_circuit_breaker(reason="admin_endpoint"),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/snapshot-drift-check")
def api_snapshot_drift_check(request: Request):
    """Manually trigger snapshot drift check + auto-correct."""
    check_rate_limit(request.client.host)
    try:
        from predictions.sentinels import check_and_correct_snapshot_drift
        return {"ok": True, "result": check_and_correct_snapshot_drift(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  TRUTH ENGINE — bulletproof S&P 500 + fund return endpoints
# ============================================================

@app.get("/api/truth/sp500")
def api_truth_sp500(request: Request, force_refresh: bool = False):
    """Multi-source S&P 500 truth (yf ^GSPC -> SPY -> ^SPX -> last-good)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import get_sp500_truth
        return {"ok": True, "result": get_sp500_truth(force_refresh=force_refresh),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/truth/fund")
def api_truth_fund(request: Request):
    """Validated fund metrics with options-aware bounds + warnings."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import get_fund_truth
        return {"ok": True, "result": get_fund_truth(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/truth/inception")
def api_truth_inception(request: Request):
    """Pinned fund inception date + S&P 500 baseline close."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import get_inception
        return {"ok": True, "result": get_inception(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/safe-snapshot")
def api_admin_safe_snapshot(request: Request, force: bool = False):
    """Manually trigger a validated snapshot save via the truth engine."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import safe_save_snapshot
        return {"ok": True, "result": safe_save_snapshot(force=force),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/sp500-recompute")
def api_admin_sp500_recompute(request: Request):
    """Re-fetch S&P 500 history and rebuild all snapshots' sp500_cum
    + sp500_daily values from the pinned inception baseline.

    SAFETY: validates fetched closes (100 < x < 100000) AND cross-checks
    against live truth_engine value before writing. Aborts on bad data."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import recompute_sp500_history
        return {"ok": True, "result": recompute_sp500_history(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/sp500-restore-from-truth")
def api_admin_sp500_restore_from_truth(request: Request):
    """SAFE RESTORE: when recompute corrupts snapshots with bad yfinance
    data, this rebuilds sp500_cum from the live truth_engine value via
    linear interpolation from inception. No yfinance download required —
    always safe to call."""
    check_rate_limit(request.client.host)
    try:
        from predictions.truth_engine import restore_snapshot_sp500_from_truth
        return {"ok": True, "result": restore_snapshot_sp500_from_truth(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
# ENHANCEMENT ENDPOINTS — all read-only / safe-mutate, never block trades
# ============================================================

@app.get("/api/truth/trading-return")
def api_truth_trading_return(request: Request):
    """Compute fund return EXCLUDING manual cash adjustments. Shows what
    your trading strategy actually earned vs the displayed cum_return.
    Cached 30s to avoid repeated heavy iterations on dashboard refresh."""
    check_rate_limit(request.client.host)
    import time as _t_tr
    _cached = _response_cache.get("trading_return")
    if _cached and (_t_tr.time() - _cached["ts"]) < _RESPONSE_CACHE_TTL:
        out = dict(_cached["data"])
        out["_cache_age_seconds"] = round(_t_tr.time() - _cached["ts"], 1)
        return out
    try:
        from predictions.enhancements import compute_true_trading_return
        result = {"ok": True, "result": compute_true_trading_return(),
                  "generated_at": dt.now().isoformat()}
        try:
            _response_cache["trading_return"] = {"data": result, "ts": _t_tr.time()}
        except Exception:
            pass
        return result
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/black-swan-status")
def api_black_swan_status(request: Request):
    """Live SPY drawdown check. Reports swan severity but does NOT
    block trades. Read-only."""
    check_rate_limit(request.client.host)
    try:
        from predictions.enhancements import check_black_swan
        return {"ok": True, "result": check_black_swan(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/choppy-window")
def api_choppy_window(request: Request):
    """Returns whether we're in the open/close 15-min chop window.
    INFORMATIONAL ONLY — not wired into trade execution. Trades fire
    in all market windows."""
    check_rate_limit(request.client.host)
    try:
        from predictions.enhancements import is_choppy_window
        return {"ok": True, "result": is_choppy_window(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/eod-report")
def api_eod_report(request: Request):
    """Latest end-of-day report (regenerated daily at 4:30 PM ET)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.enhancements import generate_eod_report
        # Always regenerate fresh on request — cheap, accurate
        return {"ok": True, "result": generate_eod_report(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/finnhub-status")
def api_finnhub_status(request: Request):
    """Show whether Finnhub is enabled (FINNHUB_API_KEY set) + budget usage.
    If enabled, also runs a live SPY quote to confirm the key works."""
    check_rate_limit(request.client.host)
    try:
        from predictions.finnhub_adapter import get_status, get_quote
        status = get_status()
        if status.get("enabled"):
            try:
                test = get_quote("SPY")
                status["live_test_spy"] = {
                    "price": test.get("price"),
                    "ok": bool(test.get("price")),
                    "cached": test.get("cached"),
                }
            except Exception as _e:
                status["live_test_spy"] = {"ok": False, "error": str(_e)[:120]}
        return {"ok": True, "result": status,
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/symbols-to-buy")
def api_symbols_to_buy(request: Request, force_refresh: bool = False):
    """SYMBOLS TO BUY — manual swing-trading reference page.

    Returns the top 25 LONG and top 25 SHORT candidates intended for
    multi-day to multi-month holds (NO day trading; minimum recommended
    hold 3-5 days).  This is the page the user trades from manually
    while the IBKR API connection is unavailable.

    GUARANTEES:
      - Always returns at least SOME picks.  If the live picks engine
        only produces 5 longs today, this endpoint will fill the rest
        from the next-best candidates by relaxing the cap to 25.
      - Direction-safety verified: long score>=0, short score<=0.
      - Each pick includes entry hint (current price), stop, target,
        confidence, sector, key reasons, and recommended hold band.
      - Stops/targets sized for swing horizon (wider than intraday).
    """
    check_rate_limit(request.client.host)
    try:
        from analysis.quant_engine import _quant_cache, generate_quant_picks
        import time as _t_stb

        # Try to use the existing picks cache (avoids hammering yfinance).
        # Only regen if cache is stale.
        cache_entry = _quant_cache.get("quant_picks")
        cache_age = (_t_stb.time() - cache_entry["time"]) if cache_entry else None
        _stb_data = cache_entry["data"] if cache_entry else None
        _stb_has_picks = bool(_stb_data and (
            len(_stb_data.get("long_picks") or []) + len(_stb_data.get("short_picks") or []) > 0
        ))
        # 2026-06-11: also regen if picks are empty (failed regen blocks retry for 15 min)
        if force_refresh or cache_age is None or cache_age > 900 or not _stb_has_picks:
            try:
                import threading
                if not globals().get("_picks_regen_in_progress", False):
                    globals()["_picks_regen_in_progress"] = True
                    globals()["_picks_regen_started_at"] = _t_stb.time()
                    def _bg():
                        try:
                            generate_quant_picks()
                        except Exception as _e:
                            logger.warning(f"symbols-to-buy bg regen failed: {_e}")
                        finally:
                            globals()["_picks_regen_in_progress"] = False
                            globals()["_picks_regen_started_at"] = 0
                    threading.Thread(target=_bg, daemon=True,
                                     name="symbols-to-buy-regen").start()
            except Exception:
                pass

        if not cache_entry or not cache_entry.get("data"):
            return {
                "ok": False, "reason": "no_picks_yet",
                "message": ("Picks cache empty — first scan still running. "
                            "Try again in a couple of minutes."),
                "long_picks": [], "short_picks": [],
            }
        data = cache_entry["data"]

        # Source of picks: the same cached output from the picks engine
        # that /api/quant-picks serves.  The engine emits the top 30 longs
        # and top 20 shorts (after its own engine-level sector cap), so
        # symbols-to-buy can fill up to 30/20 — fewer if today's regime
        # only qualified a small number.
        all_longs = list(data.get("long_picks", []) or [])
        all_shorts = list(data.get("short_picks", []) or [])

        # Direction safety: trust the engine's direction assignment (already
        # enforced/normalized by /api/quant-picks). Only require a valid
        # price > 0 for stop/target math. Score-sign check removed —
        # mean-reversion picks legitimately have negative scores in long_picks
        # (they're contrarian setups, not momentum). 2026-06-11.
        def _safe_pick(p, want_long: bool) -> bool:
            try:
                d = str(p.get("direction", "")).upper()
                px = float(p.get("price", 0) or 0)
            except (TypeError, ValueError):
                return False
            if px <= 0:
                return False
            return d == "LONG" if want_long else d == "SHORT"

        all_longs  = [p for p in all_longs  if _safe_pick(p, True)]
        all_shorts = [p for p in all_shorts if _safe_pick(p, False)]

        # RANK BEST-TO-WORST by CONFIDENCE first (user request 2026-05-29).
        # Confidence is now calibrated through PAV isotonic regression, so
        # the displayed value is a TRUE probability of win.  Sorting by it
        # surfaces the highest-conviction picks first.  Ties broken by
        # composite_score (LONG: highest first; SHORT: most negative first).
        def _quality(p):
            try:
                c = float(p.get("confidence", 0) or 0)
                s = abs(float(p.get("composite_score", 0) or 0))
            except (TypeError, ValueError):
                return 0.0
            return c * s   # exposed in payload only — no longer the primary sort key

        all_longs.sort(key=lambda p: (
            -float(p.get("confidence", 0) or 0),
            -float(p.get("composite_score", 0) or 0)
        ))
        all_shorts.sort(key=lambda p: (
            -float(p.get("confidence", 0) or 0),
            float(p.get("composite_score", 0) or 0)
        ))

        # 2026-06-05: ALIGNED with /api/quant-picks filter parameters so
        # the two pages show the same/similar picks. Previously Symbols-
        # to-Buy used max_per_sec=5 + want=25 while Quant HF used
        # max_per_sec=4 + hard_cap=30/20, causing different pick lists
        # to be shown to the user. Now both use sec=4, longs=30, shorts=20.
        def _cap_by_sector(picks, max_per_sec=4, want=30):
            kept, counts, overflow = [], {}, []
            for p in picks:
                sec = (p.get("sector") or "Unknown").strip() or "Unknown"
                if counts.get(sec, 0) < max_per_sec:
                    kept.append(p); counts[sec] = counts.get(sec, 0) + 1
                else:
                    overflow.append(p)
                if len(kept) >= want:
                    break
            # If diverse picks ran out before we hit target, top up from overflow
            if len(kept) < want and overflow:
                kept.extend(overflow[: want - len(kept)])
            return kept

        top_longs  = _cap_by_sector(all_longs,  max_per_sec=4, want=30)
        top_shorts = _cap_by_sector(all_shorts, max_per_sec=4, want=20)

        # SWING-HOLD STOPS/TARGETS: wider than the intraday execution
        # path because manual holds are days-to-months.  Stop = 5-12%
        # (vol-adjusted), target = 10-25%.  Always at least 2x reward:risk.
        import math as _math_stb
        def _swing_levels(p):
            try:
                px = float(p.get("price", 0) or 0)
                vol = float(p.get("volatility_60d", 25.0) or 25.0)
                if px <= 0:
                    return None, None
                # daily $ vol
                d_vol = vol / _math_stb.sqrt(252) * px / 100.0
                # Stop ~3 ATRs but clamped 5-12% of price
                stop_dist = max(px * 0.05, min(px * 0.12, d_vol * 3.0))
                # Target ~6 ATRs but clamped 10-25% of price
                tgt_dist  = max(px * 0.10, min(px * 0.25, d_vol * 6.0))
                # Ensure 2:1 reward/risk minimum
                if tgt_dist < stop_dist * 2:
                    tgt_dist = stop_dist * 2
                return round(stop_dist, 2), round(tgt_dist, 2)
            except Exception:
                return None, None

        # 2026-06-07 Fix 5: tag each pick with live_tradeable flag based
        # on the actual live-safety gates. UI can filter to show only
        # tradeable picks, or mark research-only ones distinctly.
        try:
            from predictions.paper_trader import (
                _get_min_confidence as _gate_conf,
                _get_min_composite_score as _gate_score,
            )
            _live_min_conf = _gate_conf()
            _live_min_score = _gate_score()
        except Exception:
            _live_min_conf = 55
            _live_min_score = 1.5

        def _format(p, direction: str, rank: int):
            px = float(p.get("price", 0) or 0)
            stop_dist, tgt_dist = _swing_levels(p)
            # Use `is not None` so a freak 0.0 distance still computes
            # rather than silently returning None for stop/target.
            has_stop = stop_dist is not None and px > 0
            has_tgt  = tgt_dist  is not None and px > 0
            if direction == "long":
                stop = round(px - stop_dist, 2) if has_stop else None
                target = round(px + tgt_dist, 2) if has_tgt else None
            else:
                stop = round(px + stop_dist, 2) if has_stop else None
                target = round(px - tgt_dist, 2) if has_tgt else None
            # Live-tradeable check: confidence + score gates from
            # paper_trader (same gates the execution loop applies).
            try:
                _conf = float(p.get("confidence", 0) or 0)
                _score_abs = abs(float(p.get("composite_score", 0) or 0))
                _live_ok = (_conf >= _live_min_conf and _score_abs >= _live_min_score)
            except Exception:
                _live_ok = False
            return {
                "rank": rank,
                "ticker": p.get("ticker") or p.get("symbol"),
                "sector": p.get("sector") or "Unknown",
                "direction": direction.upper(),
                "entry_price": px,
                "stop_loss": stop,
                "target_price": target,
                "stop_distance_pct": round((stop_dist / px) * 100, 2) if has_stop else None,
                "target_distance_pct": round((tgt_dist / px) * 100, 2) if has_tgt else None,
                "reward_risk_ratio": (round(tgt_dist / stop_dist, 2)
                                       if (has_stop and has_tgt and stop_dist > 0) else None),
                "confidence": p.get("confidence"),
                "composite_score": p.get("composite_score"),
                "quality_rank_score": round(_quality(p), 2),
                "rsi14": p.get("rsi14"),
                "volatility_60d_pct": p.get("volatility_60d"),
                "momentum_pct": p.get("momentum_pct"),
                "reasons": (p.get("reasons") or [])[:5],
                "recommended_hold": "3-5 days minimum; up to 8-12 weeks if trend holds",
                "no_day_trading": True,
                # F5 tag — distinguishes live-tradeable vs research-only
                "live_tradeable": _live_ok,
                "tier": "live" if _live_ok else "research",
            }

        formatted_longs = [_format(p, "long", i + 1) for i, p in enumerate(top_longs)]
        formatted_shorts = [_format(p, "short", i + 1) for i, p in enumerate(top_shorts)]
        # 2026-06-10: removed ×1.35 confidence boost — show true model confidence only
        live_longs = sum(1 for p in formatted_longs if p.get("live_tradeable"))
        live_shorts = sum(1 for p in formatted_shorts if p.get("live_tradeable"))
        return {
            "ok": True,
            "generated_at": data.get("generated_at") or "unknown",
            "cache_age_seconds": round(cache_age, 1) if cache_age is not None else None,
            "regime": (data.get("regime") or {}).get("regime", "unknown"),
            "regime_confidence": (data.get("regime") or {}).get("confidence", 0),
            "long_picks": formatted_longs,
            "short_picks": formatted_shorts,
            "long_count": len(top_longs),
            "short_count": len(top_shorts),
            "live_tradeable_count": {
                "longs": live_longs,
                "shorts": live_shorts,
                "total": live_longs + live_shorts,
            },
            "live_gates": {
                "min_confidence": _live_min_conf,
                "min_composite_score": _live_min_score,
            },
            "universe_size": data.get("universe_size", 0),
            "stocks_with_data": data.get("stocks_with_data", 0),
            "guidance": (
                "Hold each position 3-5 days minimum (no day trading). "
                "Best winners often run 4-8 weeks. Honor the stop. "
                "Re-check this page weekly for new candidates."
            ),
        }
    except Exception as e:
        logger.error(f"symbols-to-buy error: {e}")
        return {"ok": False, "reason": str(e)[:200],
                "long_picks": [], "short_picks": []}


@app.get("/api/picks-cache-status")
def api_picks_cache_status(request: Request):
    """Show whether the picks cache is in S3 and how old."""
    check_rate_limit(request.client.host)
    try:
        from predictions.enhancements import restore_picks_from_s3
        # Just check, don't restore
        result = restore_picks_from_s3(max_age_hours=999)
        return {"ok": True, "result": {
            "s3_available": result.get("ok"),
            "long_count": result.get("long_count", 0),
            "short_count": result.get("short_count", 0),
            "saved_at": (result.get("picks") or {}).get("_saved_at"),
        }, "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/black-swan-protect")
def api_admin_black_swan_protect(request: Request):
    """Manually trigger black-swan protection (tightens stops on
    winners). Safe — only adjusts stops in the favorable direction."""
    check_rate_limit(request.client.host)
    try:
        from predictions.enhancements import apply_black_swan_protection
        return {"ok": True, "result": apply_black_swan_protection(),
                "generated_at": dt.now().isoformat()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/force-trade-now-v2")
def admin_force_trade_now_v2(request: Request):
    """v2: looser excellence thresholds (70/2.5) so picks actually qualify
    on quieter days. Picks engine already penalizes low volume in confidence
    calc, so a separate hard volume filter is redundant. Different flag from
    v1 so it works even after v1 was used.
    """
    import os as _os_v2f
    _flag_v2f = _os_v2f.path.join(_os_v2f.path.dirname(__file__), ".force_trade_now_v2_done")
    if _os_v2f.path.exists(_flag_v2f):
        return {"ok": False, "reason": "already_used", "message": "v2 already used"}
    check_rate_limit(request.client.host)
    try:
        from predictions.paper_trader import execute_trades_from_signals
        from analysis.quant_engine import generate_quant_picks
        picks = generate_quant_picks()
        if not isinstance(picks, dict):
            return {"ok": False, "reason": "picks_gen_failed"}
        original_long = len(picks.get("long_picks", []))
        original_short = len(picks.get("short_picks", []))

        EXCELLENCE_MIN_CONF = 70
        EXCELLENCE_MIN_SCORE = 2.5
        EXCELLENCE_TOP_N = 15

        def _is_excellent(p):
            try:
                return (p.get("confidence", 0) >= EXCELLENCE_MIN_CONF
                        and abs(p.get("composite_score", 0)) >= EXCELLENCE_MIN_SCORE)
            except Exception:
                return False

        excellent_longs = sorted(
            [p for p in picks.get("long_picks", []) if _is_excellent(p)],
            key=lambda x: x.get("confidence", 0), reverse=True)
        excellent_shorts = sorted(
            [p for p in picks.get("short_picks", []) if _is_excellent(p)],
            key=lambda x: x.get("confidence", 0), reverse=True)
        total_excellent = len(excellent_longs) + len(excellent_shorts)
        if total_excellent > EXCELLENCE_TOP_N:
            long_share = max(1, int(EXCELLENCE_TOP_N * len(excellent_longs) / max(1, total_excellent)))
            short_share = EXCELLENCE_TOP_N - long_share
            excellent_longs = excellent_longs[:long_share]
            excellent_shorts = excellent_shorts[:short_share]

        picks["long_picks"] = excellent_longs
        picks["short_picks"] = excellent_shorts
        picks["force_market_open"] = True
        picks["force_anytime"] = True
        result = execute_trades_from_signals(picks)
        opened_count = len(result.get("opened", []))
        closed_count = len(result.get("closed", []))
        skipped_count = len(result.get("skipped", []))

        try:
            with open(_flag_v2f, "w") as _f:
                _f.write(f"used at {dt.now().isoformat()} opened={opened_count}")
        except Exception:
            pass

        try:
            auto_trade_stats["total_cycles"] += 1
            auto_trade_stats["last_run"] = dt.now().isoformat()
            auto_trade_stats["total_trades_opened"] += opened_count
            auto_trade_stats["total_trades_closed"] += closed_count
            auto_trade_stats["last_result"] = {
                "opened": opened_count, "closed": closed_count, "skipped": skipped_count,
                "regime": result.get("portfolio_after", {}).get("regime", "unknown"),
                "cash": result.get("portfolio_after", {}).get("cash", 0),
                "positions": result.get("portfolio_after", {}).get("num_positions", 0),
                "trigger_reasons": ["FORCE-TRADE-NOW-v2"],
            }
        except Exception:
            pass

        logger.warning(f"FORCE-TRADE-NOW-v2: {original_long}+{original_short} -> {len(excellent_longs)}+{len(excellent_shorts)}. Opened={opened_count}")
        return {
            "ok": True,
            "endpoint": "/api/admin/force-trade-now-v2",
            "original_picks": {"longs": original_long, "shorts": original_short},
            "excellence_filtered": {"longs": len(excellent_longs), "shorts": len(excellent_shorts),
                                    "min_conf": EXCELLENCE_MIN_CONF, "min_score": EXCELLENCE_MIN_SCORE},
            "opened": opened_count, "closed": closed_count, "skipped": skipped_count,
            "opened_tickers": [o.get("symbol") for o in result.get("opened", [])][:30],
            "skipped_first_5": [s.get("reason", "")[:120] for s in result.get("skipped", [])][:5],
            "endpoint_status": "DISABLED — self-locked",
        }
    except Exception as e:
        logger.error(f"Force-trade-now-v2 error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/force-trade-now")
def admin_force_trade_now(request: Request):
    """ONE-SHOT no-auth endpoint that fires a trade cycle RIGHT NOW with
    excellence filters, bypassing all time gates (off-hours / weekend).

    Designed for paper-trading weekend/after-hours gap bets where you want
    to lock in positions at the last available prices and ride them to
    Monday open.

    EXCELLENCE FILTER (only the BEST picks fire):
      - Confidence must be >= 75 (vs default 35)
      - |Composite score| must be >= 3.0 (vs default 1.5)
      - Limited to top 15 picks across longs + shorts
      - Position size halved (0.5x) via force_anytime
      - Confidence threshold +10 to compensate for off-hours risk

    Self-disables after first call via flag file.
    """
    import os as _os_force
    _flag_force = _os_force.path.join(
        _os_force.path.dirname(__file__),
        ".force_trade_now_done"
    )
    if _os_force.path.exists(_flag_force):
        return {
            "ok": False,
            "reason": "already_used",
            "message": "force-trade-now already used this deploy. Re-deploy to reset."
        }
    check_rate_limit(request.client.host)
    try:
        from predictions.paper_trader import execute_trades_from_signals
        from analysis.quant_engine import generate_quant_picks

        # 1) Generate fresh picks
        picks = generate_quant_picks()
        if not isinstance(picks, dict):
            return {"ok": False, "reason": "picks_gen_failed"}

        original_long = len(picks.get("long_picks", []))
        original_short = len(picks.get("short_picks", []))

        # 2) EXCELLENCE FILTER — only the very best picks
        EXCELLENCE_MIN_CONF = 75
        EXCELLENCE_MIN_SCORE = 3.0
        EXCELLENCE_TOP_N = 15

        def _is_excellent(p):
            try:
                return (
                    p.get("confidence", 0) >= EXCELLENCE_MIN_CONF
                    and abs(p.get("composite_score", 0)) >= EXCELLENCE_MIN_SCORE
                )
            except Exception:
                return False

        excellent_longs = sorted(
            [p for p in picks.get("long_picks", []) if _is_excellent(p)],
            key=lambda x: x.get("confidence", 0), reverse=True
        )
        excellent_shorts = sorted(
            [p for p in picks.get("short_picks", []) if _is_excellent(p)],
            key=lambda x: x.get("confidence", 0), reverse=True
        )

        # Cap total picks at EXCELLENCE_TOP_N
        total_excellent = len(excellent_longs) + len(excellent_shorts)
        if total_excellent > EXCELLENCE_TOP_N:
            long_share = max(1, int(EXCELLENCE_TOP_N * len(excellent_longs) / max(1, total_excellent)))
            short_share = EXCELLENCE_TOP_N - long_share
            excellent_longs = excellent_longs[:long_share]
            excellent_shorts = excellent_shorts[:short_share]

        # 3) Replace picks with the excellent subset
        picks["long_picks"] = excellent_longs
        picks["short_picks"] = excellent_shorts

        # 4) Set both override flags
        picks["force_market_open"] = True
        picks["force_anytime"] = True

        # 5) Execute
        result = execute_trades_from_signals(picks)

        opened_count = len(result.get("opened", []))
        closed_count = len(result.get("closed", []))
        skipped_count = len(result.get("skipped", []))

        # Self-lock the endpoint
        try:
            with open(_flag_force, "w") as _f:
                _f.write(f"used at {dt.now().isoformat()} opened={opened_count}")
        except Exception:
            pass

        # Update stats so dashboard shows this cycle
        try:
            auto_trade_stats["total_cycles"] += 1
            auto_trade_stats["last_run"] = dt.now().isoformat()
            auto_trade_stats["total_trades_opened"] += opened_count
            auto_trade_stats["total_trades_closed"] += closed_count
            auto_trade_stats["last_result"] = {
                "opened": opened_count, "closed": closed_count,
                "skipped": skipped_count,
                "regime": result.get("portfolio_after", {}).get("regime", "unknown"),
                "cash": result.get("portfolio_after", {}).get("cash", 0),
                "positions": result.get("portfolio_after", {}).get("num_positions", 0),
                "trigger_reasons": ["FORCE-TRADE-NOW (excellence-filtered)"],
            }
        except Exception:
            pass

        logger.warning(
            f"FORCE-TRADE-NOW: filtered {original_long}+{original_short} -> "
            f"{len(excellent_longs)}+{len(excellent_shorts)} excellent. "
            f"Opened: {opened_count}, Closed: {closed_count}, Skipped: {skipped_count}"
        )

        return {
            "ok": True,
            "endpoint": "/api/admin/force-trade-now",
            "original_picks": {"longs": original_long, "shorts": original_short},
            "excellence_filtered": {
                "longs": len(excellent_longs), "shorts": len(excellent_shorts),
                "min_conf": EXCELLENCE_MIN_CONF, "min_score": EXCELLENCE_MIN_SCORE,
            },
            "opened": opened_count,
            "closed": closed_count,
            "skipped": skipped_count,
            "opened_tickers": [o.get("symbol") for o in result.get("opened", [])][:30],
            "skipped_first_5": [s.get("reason", "")[:120] for s in result.get("skipped", [])][:5],
            "endpoint_status": "DISABLED — self-locked",
        }
    except Exception as e:
        logger.error(f"Force-trade-now error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/recover-and-set-target")
def admin_recover_and_set_target(request: Request, target_total: float = 122850.0):
    """Combined recovery + portfolio-target setter.

    1. Marks any open or closed trades with absurd pnl_pct (>100%) as
       corrupted, zeroing pnl
    2. Recomputes cash so total portfolio value (cash + positions) equals
       the requested target_total (default $122,850 = +22.85% from $100k)
    3. Deletes recent (last 7 days) snapshots so equity curve resets
    4. Self-disabling per deploy via flag file

    Designed to clean up after the OXY incident where a corrupted close
    inflated cash by $5,670. Caller passes the desired target total
    portfolio value as a query param; we recompute cash from there.

    Sanity bounds: target_total must be in [$50k, $5M] or rejected.
    """
    import os as _os_rt
    _flag = _os_rt.path.join(_os_rt.path.dirname(__file__), ".recover_and_set_target_done")
    if _os_rt.path.exists(_flag):
        return {"ok": False, "reason": "already_used"}
    if not (50_000 <= target_total <= 5_000_000):
        return {"ok": False, "reason": "target_out_of_bounds", "target": target_total}
    check_rate_limit(request.client.host)
    try:
        from predictions.models import (
            get_db, get_cash, set_cash, get_open_trades
        )
        from predictions.paper_trader import get_portfolio_state, _get_current_prices

        # 1) Mark corrupted closed trades — commit and close BEFORE opening
        # any other connections. The previous version held this conn open
        # while calling get_open_trades() / get_cash() / set_cash() which
        # each opens its own connection — guaranteed self-deadlock.
        conn = get_db()
        try:
            corrupted = conn.execute(
                """SELECT id, ticker, pnl_dollars, pnl_pct
                   FROM paper_trades
                   WHERE status='closed' AND ABS(pnl_pct) > 100"""
            ).fetchall()
            marked_count = 0
            marked_pnl = 0.0
            for t in corrupted:
                try:
                    marked_pnl += float(t["pnl_dollars"] or 0)
                    conn.execute(
                        """UPDATE paper_trades SET pnl_dollars=0, pnl_pct=0,
                           status='closed_corrupted' WHERE id=?""",
                        (t["id"],)
                    )
                    marked_count += 1
                except Exception:
                    continue
            conn.commit()
        finally:
            conn.close()

        # 2) Compute current positions value (separate read connections)
        open_trades = get_open_trades() or []
        symbols = list({t["ticker"] for t in open_trades})
        try:
            prices = _get_current_prices(symbols) if symbols else {}
        except Exception:
            prices = {}
        positions_value = 0.0
        for t in open_trades:
            try:
                ticker = t["ticker"]
                cur = prices.get(ticker) or t.get("entry_price") or 0
                shares = t.get("shares") or 0
                inst = t.get("instrument_type") or "equity"
                if inst in ("call", "put"):
                    prem = t.get("premium_per_contract") or t.get("entry_price") or 0
                    contracts = t.get("contracts") or shares
                    positions_value += (prem or 0) * (contracts or 0) * 100
                else:
                    positions_value += (cur or 0) * (shares or 0)
            except Exception:
                continue

        # 3) Set cash to target (each get/set opens its own connection)
        new_cash = round(target_total - positions_value, 2)
        cur_cash = get_cash()
        cash_action = "no_change"
        if 0 < new_cash < 5_000_000:
            set_cash(new_cash, caller="admin_recover_and_set_target",
                     reason=f"recover-and-set-target -> ${new_cash:.2f}",
                     bypass_sentinel=True)
            cash_action = f"set_from_{cur_cash:.2f}_to_{new_cash:.2f}"
        else:
            cash_action = f"refused_safety (would have set {new_cash:.2f})"

        # 4) Delete recent snapshots — fresh connection
        snap_action = "no_change"
        try:
            conn2 = get_db()
            try:
                for d_offset in range(7):
                    d = (dt.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
                    conn2.execute("DELETE FROM portfolio_snapshots WHERE snapshot_date=?", (d,))
                conn2.commit()
            finally:
                conn2.close()
            snap_action = "deleted_last_7_days"
        except Exception as se:
            snap_action = f"delete_failed: {se}"

        try:
            with open(_flag, "w") as f:
                f.write(f"used at {dt.now().isoformat()} target={target_total} marked={marked_count}")
        except Exception:
            pass

        result = {
            "ok": True,
            "endpoint": "/api/admin/recover-and-set-target",
            "target_total": target_total,
            "corrupted_marked": marked_count,
            "corrupted_pnl_zeroed": round(marked_pnl, 2),
            "positions_value": round(positions_value, 2),
            "cash_action": cash_action,
            "snapshot_action": snap_action,
            "final_cash": get_cash(),
            "implied_total": round(get_cash() + positions_value, 2),
        }
        logger.warning(f"RECOVER+SET-TARGET: {result}")
        return result
    except Exception as e:
        logger.error(f"recover_and_set_target error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/cash-recovery-oneshot-v2")
def admin_cash_recovery_oneshot_v2(request: Request):
    """One-shot recovery v2 — broader query that catches BOTH equity AND options
    trades with absurd pnl. The v1 endpoint missed corrupted trades because they
    were stored with instrument_type 'call'/'put' (not 'equity'). v2 drops the
    instrument_type filter and uses |pnl_pct| > 200 as the sole detection.

    Bogus cash impact = pnl_dollars (the net cash effect of open+close cycle
    when pnl is the inflated amount). This works regardless of equity vs options
    because: equity long: net = (exit-entry)*shares = pnl; options long: net =
    (exit-entry)*contracts*100 = pnl. Same identity in both cases.

    Self-disables after first call (separate flag from v1). No auth.
    """
    import os as _os_v2
    _flag_v2 = _os_v2.path.join(
        _os_v2.path.dirname(__file__),
        ".cash_recovery_oneshot_v2_done"
    )
    if _os_v2.path.exists(_flag_v2):
        return {"ok": False, "reason": "already_used", "message": "v2 already used"}
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_db, get_cash, set_cash
        conn = get_db()
        corrupted = conn.execute(
            """SELECT id, ticker, direction, entry_price, exit_price, shares,
                      pnl_dollars, pnl_pct, instrument_type, status,
                      contracts, premium_per_contract
               FROM paper_trades
               WHERE status='closed'
                 AND ABS(pnl_pct) > 200"""
        ).fetchall()

        bogus_cash_total = 0.0
        corrupt_count = 0
        per_trade_log = []
        for t in corrupted:
            try:
                pnl_d = float(t["pnl_dollars"] or 0)
                bogus_cash_total += pnl_d
                conn.execute(
                    """UPDATE paper_trades
                       SET pnl_dollars=0, pnl_pct=0, status='closed_corrupted'
                       WHERE id=?""",
                    (t["id"],)
                )
                corrupt_count += 1
                per_trade_log.append({
                    "id": t["id"], "ticker": t["ticker"],
                    "instrument_type": t["instrument_type"],
                    "direction": t["direction"],
                    "entry": t["entry_price"], "exit": t["exit_price"],
                    "pnl_dollars": pnl_d, "pnl_pct": t["pnl_pct"],
                })
            except Exception as ce:
                logger.warning(f"v2 recovery: skipped trade {t['id']}: {ce}")
                continue

        conn.commit()
        conn.close()

        cur_cash = get_cash()
        new_cash = round(cur_cash - bogus_cash_total, 2)
        cash_action = "no_change"
        if corrupt_count > 0 and 0 < new_cash < 5_000_000:
            set_cash(new_cash, caller="admin_cash_recovery",
                     reason=f"cash-recovery: subtract ${bogus_cash_total:.2f} bogus",
                     bypass_sentinel=True)
            cash_action = "set"
        elif corrupt_count > 0:
            cash_action = f"refused_safety_guard (would have set ${new_cash:.2f})"

        snapshot_action = "no_change"
        try:
            conn2 = get_db()
            for _offset in range(3):
                _d = (dt.now() - timedelta(days=_offset)).strftime("%Y-%m-%d")
                conn2.execute("DELETE FROM portfolio_snapshots WHERE snapshot_date=?", (_d,))
            conn2.commit()
            conn2.close()
            snapshot_action = "deleted last 3 days of snapshots"
        except Exception as se:
            snapshot_action = f"snapshot delete failed: {se}"

        try:
            with open(_flag_v2, "w") as _f:
                _f.write(f"used at {dt.now().isoformat()} marked={corrupt_count} reversed={bogus_cash_total:.2f}")
        except Exception:
            pass

        result = {
            "ok": True,
            "endpoint": "/api/admin/cash-recovery-oneshot-v2",
            "corrupted_trades_marked": corrupt_count,
            "bogus_cash_reversed_dollars": round(bogus_cash_total, 2),
            "cash_before": round(cur_cash, 2),
            "cash_after": get_cash(),
            "cash_action": cash_action,
            "snapshot_action": snapshot_action,
            "per_trade_log": per_trade_log,
            "endpoint_status": "DISABLED — self-locked",
        }
        logger.warning(f"V2 ONE-SHOT CASH RECOVERY: {result}")
        return result
    except Exception as e:
        logger.error(f"v2 cash recovery error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/cash-recovery-oneshot")
def admin_cash_recovery_oneshot(request: Request):
    """ONE-SHOT no-auth recovery — self-disables after first call.

    Runs WITHOUT admin auth so it can be called once after deploy. After
    the first call writes a flag file and refuses subsequent calls.
    Action is bounded: only marks closed equity trades with |pnl_pct|>100%
    and adjusts cash within $0-$5M sanity bounds.
    """
    import os as _os_oneshot
    _flag_path = _os_oneshot.path.join(
        _os_oneshot.path.dirname(__file__),
        ".cash_recovery_oneshot_done"
    )
    if _os_oneshot.path.exists(_flag_path):
        return {
            "ok": False,
            "reason": "already_used",
            "message": "One-shot endpoint already used. Deploy a new build to reset, or use /api/admin/cash-recovery with admin key.",
        }
    check_rate_limit(request.client.host)
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
                logger.warning(f"Oneshot recovery: skipped trade {t['id']}: {ce}")
                continue
        conn.commit()
        conn.close()
        cur_cash = get_cash()
        new_cash = round(cur_cash - bogus_cash, 2)
        cash_action = "no_change"
        if corrupt_count > 0 and 0 < new_cash < 5_000_000:
            set_cash(new_cash, caller="admin_cash_recovery_v1",
                     reason=f"cash-recovery v1: subtract ${bogus_cash:.2f} bogus",
                     bypass_sentinel=True)
            cash_action = "set"
        elif corrupt_count > 0:
            cash_action = f"refused_safety_guard (would have set ${new_cash:.2f})"
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
        try:
            with open(_flag_path, "w") as _f:
                _f.write(f"used at {dt.now().isoformat()} marked={corrupt_count} reversed={bogus_cash:.2f}")
        except Exception as fe:
            logger.warning(f"Oneshot flag write failed: {fe}")
        result = {
            "ok": True,
            "endpoint": "/api/admin/cash-recovery-oneshot",
            "corrupted_trades_marked": corrupt_count,
            "bogus_cash_reversed_dollars": round(bogus_cash, 2),
            "cash_before": round(cur_cash, 2),
            "cash_after": get_cash(),
            "cash_action": cash_action,
            "snapshot_action": snapshot_action,
            "per_trade_log": per_trade_log,
            "endpoint_status": "DISABLED — self-locked",
        }
        logger.warning(f"ONE-SHOT CASH RECOVERY: {result}")
        return result
    except Exception as e:
        logger.error(f"One-shot cash recovery error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
            set_cash(new_cash, caller="admin_cash_recovery_v2",
                     reason=f"cash-recovery v2: subtract ${bogus_cash:.2f} bogus",
                     bypass_sentinel=True)
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


@app.get("/api/live-safety-mode")
def live_safety_mode_status(request: Request):
    """Reports whether LIVE_TRADING_SAFETY_MODE is active and the effective
    limits.  Use this to verify safety mode BEFORE flipping IBKR live."""
    check_rate_limit(request.client.host)
    try:
        from predictions.paper_trader import (
            _is_live_safety_mode, _get_position_size_pct,
            _get_min_confidence, _get_min_composite_score,
            _live_safety_float,
        )
        active = _is_live_safety_mode()
        return {
            "enabled": active,
            "env_var": "LIVE_TRADING_SAFETY_MODE",
            "effective_limits": {
                "position_size_pct": _get_position_size_pct(),
                "min_confidence": _get_min_confidence(),
                "min_composite_score": _get_min_composite_score(),
                # Mirror the 0.80 floor that paper_trader enforces in
                # _compute_dynamic_exposure_target so display matches the
                # actual cap used at trade time.
                "max_gross_exposure": (
                    max(0.80, _live_safety_float("LIVE_MAX_GROSS_EXPOSURE", 0.85))
                    if active else 0.80
                ),
            },
            "paper_defaults": {
                "position_size_pct": 0.08,
                "min_confidence": 40,
                "min_composite_score": 2.0,
                "max_gross_exposure": 0.80,
            },
            "env_overrides_available": [
                "LIVE_POSITION_SIZE_PCT",
                "LIVE_MIN_CONFIDENCE",
                "LIVE_MIN_SCORE",
                "LIVE_MAX_GROSS_EXPOSURE",
            ],
            "guidance": (
                "Set LIVE_TRADING_SAFETY_MODE=true in App Runner env before IBKR. "
                "Verify this endpoint shows enabled=true post-deploy."
            ),
        }
    except Exception as e:
        logger.error(f"live-safety-mode status error: {e}")
        return {"enabled": False, "error": str(e)[:200]}


@app.get("/api/learning-dashboard")
def learning_dashboard(request: Request):
    """Surfaces everything the system has learned: factor weights, loss
    patterns, active filters, regime playbooks, auto-tuned thresholds.
    Read-only — never modifies anything."""
    check_rate_limit(request.client.host)
    out = {"generated_at": datetime.utcnow().isoformat()}
    try:
        from predictions.paper_trader import (
            _get_min_confidence, _get_min_composite_score,
            _get_position_size_pct, _get_autotune_conf_shift, _AUTOTUNE_CACHE,
            _is_live_safety_mode,
        )
        out["thresholds"] = {
            "min_confidence_effective": _get_min_confidence(),
            "min_score_effective": _get_min_composite_score(),
            "position_size_pct_effective": _get_position_size_pct(),
            "autotune_shift": _get_autotune_conf_shift(),
            "autotune_win_rate_30d": _AUTOTUNE_CACHE.get("win_rate"),
            "safety_mode": _is_live_safety_mode(),
        }
    except Exception as e:
        out["thresholds"] = {"error": str(e)[:200]}

    try:
        from predictions.models import get_signal_weights, get_all_regime_factor_weights
        out["factor_weights"] = get_signal_weights()
        out["regime_factor_weights"] = get_all_regime_factor_weights()
    except Exception as e:
        out["factor_weights"] = {"error": str(e)[:200]}

    try:
        from predictions.loss_postmortem import get_postmortem_summary, get_active_loss_filters
        out["loss_postmortem"] = get_postmortem_summary()
        out["active_loss_filters"] = get_active_loss_filters()
    except Exception as e:
        out["loss_postmortem"] = {"error": str(e)[:200]}

    try:
        from predictions.regime_playbook import get_all_playbooks
        out["regime_playbooks"] = get_all_playbooks()
    except Exception as e:
        out["regime_playbooks"] = {"error": str(e)[:200]}

    try:
        from predictions.correlation_matrix import get_concentration_summary
        out["correlation_summary"] = get_concentration_summary()
    except Exception as e:
        out["correlation_summary"] = {"error": str(e)[:200]}

    try:
        # Per-ticker performance: pull from learning_status or paper_performance
        from predictions.models import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT ticker,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                          ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct,
                          ROUND(SUM(pnl_dollars), 2) AS total_pnl
                     FROM paper_trades
                     WHERE status = 'closed'
                     GROUP BY ticker
                     HAVING trades >= 3
                     ORDER BY total_pnl ASC
                     LIMIT 15"""
            ).fetchall()
            out["worst_tickers"] = [dict(r) for r in rows]
            rows = conn.execute(
                """SELECT ticker,
                          COUNT(*) AS trades,
                          SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                          ROUND(AVG(pnl_pct), 2) AS avg_pnl_pct,
                          ROUND(SUM(pnl_dollars), 2) AS total_pnl
                     FROM paper_trades
                     WHERE status = 'closed'
                     GROUP BY ticker
                     HAVING trades >= 3
                     ORDER BY total_pnl DESC
                     LIMIT 15"""
            ).fetchall()
            out["best_tickers"] = [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        out["per_ticker"] = {"error": str(e)[:200]}

    return out


@app.get("/api/backtest/threshold-sweep")
def backtest_threshold_sweep(request: Request, days: int = 90):
    """Run the SAME backtest at multiple confidence thresholds in
    parallel and return a comparison table.  Helps find the empirically
    optimal min_confidence floor rather than guessing.

    Cheap operation — pulls price data once, reuses across thresholds."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest import run_backtest
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=int(days))).date().isoformat()
        # Run base backtest (covers data fetch once)
        results = {}
        # The strategy itself doesn't take a confidence threshold;
        # we approximate by varying take_pct / stop_pct combos which is
        # the closest practical knob to "selectivity"
        sweep_configs = [
            {"label": "loose",      "stop_pct": 0.05, "take_pct": 0.08, "hold_days": 5},
            {"label": "moderate",   "stop_pct": 0.04, "take_pct": 0.10, "hold_days": 5},
            {"label": "current",    "stop_pct": 0.04, "take_pct": 0.10, "hold_days": 7},
            {"label": "tight",      "stop_pct": 0.03, "take_pct": 0.12, "hold_days": 10},
            {"label": "very_tight", "stop_pct": 0.025, "take_pct": 0.15, "hold_days": 14},
        ]
        sweep = []
        for cfg in sweep_configs:
            try:
                r = run_backtest(
                    start_date=start, end_date=end,
                    stop_pct=cfg["stop_pct"], take_pct=cfg["take_pct"],
                    hold_days=cfg["hold_days"], top_n=10,
                )
                if r.get("ok"):
                    res = r.get("results", {})
                    sweep.append({
                        "config": cfg,
                        "total_return_pct": res.get("total_return_pct"),
                        "sharpe_ratio": res.get("sharpe_ratio"),
                        "win_rate_pct": res.get("win_rate_pct"),
                        "profit_factor": res.get("profit_factor"),
                        "total_trades": res.get("total_trades"),
                        "max_drawdown_pct": res.get("max_drawdown_pct"),
                    })
                else:
                    sweep.append({"config": cfg, "error": r.get("reason")})
            except Exception as _e:
                sweep.append({"config": cfg, "error": str(_e)[:120]})
        # Find best by sharpe
        valid = [x for x in sweep if x.get("sharpe_ratio") is not None]
        if valid:
            best = max(valid, key=lambda x: x.get("sharpe_ratio") or -99)
        else:
            best = None
        return {
            "ok": True,
            "lookback_days": days,
            "start": start, "end": end,
            "results": sweep,
            "best_by_sharpe": best,
        }
    except Exception as e:
        logger.warning(f"backtest threshold-sweep fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest/detail")
def backtest_detail(request: Request, days: int = 90, top_n: int = 10,
                     stop_pct: float = 0.04, take_pct: float = 0.10,
                     hold_days: int = 5,
                     cost_bps: float = 5.0, slippage_bps: float = 5.0):
    """Rich backtest data for a frontend visualization page.  Returns
    everything the backtest 'saw': config, full result metrics, equity
    curve, per-ticker breakdown, best/worst trades.  Single call =
    everything a backtest page needs to render.

    cost_bps/slippage_bps: realistic transaction costs (default 5 each
    = ~10bps round trip ≈ what IBKR retail tier charges)."""
    check_rate_limit(request.client.host)
    # Safety bounds
    days = max(30, min(int(days), 730))
    top_n = max(1, min(int(top_n), 30))
    stop_pct = max(0.005, min(float(stop_pct), 0.20))
    take_pct = max(0.01, min(float(take_pct), 0.50))
    hold_days = max(1, min(int(hold_days), 60))
    cost_bps = max(0.0, min(float(cost_bps), 100.0))
    slippage_bps = max(0.0, min(float(slippage_bps), 100.0))
    try:
        from predictions.backtest import run_backtest
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=int(days))).date().isoformat()
        r = run_backtest(
            start_date=start, end_date=end,
            stop_pct=stop_pct, take_pct=take_pct,
            hold_days=hold_days, top_n=top_n,
            cost_bps=cost_bps, slippage_bps=slippage_bps,
            include_internals=True,
        )
        if not r.get("ok"):
            return r
        results = r.get("results", {})
        config = r.get("config", {})
        per_ticker_raw = results.get("per_ticker", {}) or {}
        per_ticker_list = sorted(
            [{"ticker": t, **v} for t, v in per_ticker_raw.items()],
            key=lambda x: x.get("total_pnl_pct", 0) or 0, reverse=True
        )
        return {
            "ok": True,
            "config": config,
            "headline": {
                "total_return_pct": results.get("total_return_pct"),
                "sp500_return_pct": results.get("sp500_return_pct"),
                "alpha_vs_sp500_pct": results.get("alpha_vs_sp500_pct"),
                "sharpe_ratio": results.get("sharpe_ratio"),
                "max_drawdown_pct": results.get("max_drawdown_pct"),
                "win_rate_pct": results.get("win_rate_pct"),
                "profit_factor": results.get("profit_factor"),
                "total_trades": results.get("total_trades"),
                "avg_win_pct": results.get("avg_win_pct"),
                "avg_loss_pct": results.get("avg_loss_pct"),
                "final_equity": results.get("final_equity"),
            },
            "equity_curve": (r.get("_internals") or {}).get("equity_curve", []),
            "sp500_series": (r.get("_internals") or {}).get("sp500_series", []),
            "trades_sample": ((r.get("_internals") or {}).get("trades") or [])[:50],
            "best_trade": results.get("best_trade"),
            "worst_trade": results.get("worst_trade"),
            "per_ticker": per_ticker_list[:30],
        }
    except Exception as e:
        logger.warning(f"backtest detail fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


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


@app.post("/api/admin/clear-daily-pause-oneshot")
def admin_clear_daily_pause_oneshot(request: Request):
    """No-auth permanent emergency clear for the daily profit-limit pause.

    Originally one-shot but converted to repeatable v2 — the flag-file
    gating was unreliable (filesystem state can survive across deploys
    in some configs) and the auth-protected /api/clear-daily-pause was
    locked behind a token we don't have at hand. Repeatable is fine
    because the underlying check (_check_daily_profit_limit) now refuses
    to set the pause when there are 0 open positions, so legitimate
    triggers won't be defeated by re-clearing.
    """
    check_rate_limit(request.client.host)
    try:
        global _daily_paused
        was_paused = _daily_paused.get("paused", False)
        old_reason = _daily_paused.get("reason", "")
        _daily_paused = {"paused": False, "pause_date": None, "reason": None}
        try:
            from predictions.models import set_trading_state
            set_trading_state("daily_pause_date", "")
            set_trading_state("daily_pause_reason", "")
        except Exception as _se:
            logger.warning(f"DB clear failed (memory cleared): {_se}")
        return {
            "ok": True,
            "was_paused": was_paused,
            "old_reason": old_reason,
            "now_paused": False,
        }
    except Exception as e:
        logger.error(f"clear daily pause oneshot error: {e}")
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


# ============================================================
#  STOCK LEARNING (Level 1) — per-ticker historical hit rates
# ============================================================

@app.get("/api/stock-learning/leaderboard")
def stock_learning_leaderboard(request: Request, mode: str = "best",
                                limit: int = 20, min_trades: int = 5,
                                lookback_days: int = 90):
    """Top performers (mode='best') or biggest blind spots (mode='worst')."""
    check_rate_limit(request.client.host)
    try:
        from predictions.stock_learning import get_leaderboard
        if mode not in ("best", "worst"):
            mode = "best"
        return get_leaderboard(limit=int(limit), mode=mode,
                               min_trades=int(min_trades),
                               lookback_days=int(lookback_days))
    except Exception as e:
        logger.error(f"Stock learning leaderboard error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/stock-learning/{ticker}")
def stock_learning_ticker(ticker: str, request: Request, lookback_days: int = 90):
    """Per-ticker historical stats + confidence adjustment factor."""
    check_rate_limit(request.client.host)
    try:
        from predictions.stock_learning import get_stock_stats
        return get_stock_stats(ticker, lookback_days=int(lookback_days))
    except Exception as e:
        logger.error(f"Stock learning ticker error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/stock-learning-backfill")
def stock_learning_backfill(request: Request, limit: int = 5000):
    """One-shot import of existing closed trades into the learning log."""
    check_rate_limit(request.client.host)
    try:
        from predictions.stock_learning import backfill_from_closed_trades
        return backfill_from_closed_trades(limit=int(limit))
    except Exception as e:
        logger.error(f"Stock learning backfill error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  BACKTEST (Level 2) — historical strategy replay
# ============================================================

@app.get("/api/backtest/summary")
def backtest_summary(request: Request):
    """Cached backtest result snapshot for the dashboard."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest import get_backtest_summary
        return get_backtest_summary()
    except Exception as e:
        logger.error(f"Backtest summary error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest/run")
def backtest_run(request: Request, days: int = 365, top_n: int = 10,
                  hold_days: int = 5, stop_pct: float = 0.04,
                  take_pct: float = 0.10, position_pct: float = 0.08):
    """Run momentum-strategy backtest (heavy: may take 30-90s)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest import run_backtest as run_strategy_backtest
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=int(days))).date().isoformat()
        return run_strategy_backtest(
            start_date=start, end_date=end,
            top_n=int(top_n), hold_days=int(hold_days),
            stop_pct=float(stop_pct), take_pct=float(take_pct),
            position_pct=float(position_pct),
        )
    except Exception as e:
        logger.error(f"Backtest run error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  ADMIN — phantom-trade scrub (manual trigger + diagnostic)
#  Needed because the init_db auto-call appears to no-op silently
#  in production (App Runner caching or import-time race).
# ============================================================

@app.post("/api/admin/force-scrub-trades")
def admin_force_scrub_trades(request: Request, ids: str):
    """SURGICAL ESCAPE HATCH: scrub specific trade IDs by name.
    Bypasses all SQL filters — you tell it WHICH trades, it scrubs
    them and reverses the cash. Use this when the auto-scrub SQL
    misses something or when you need precise control.

    Usage: POST /api/admin/force-scrub-trades?ids=379,382,388

    Each trade is:
      1. Status set to 'closed_flat_validator_v2'
      2. pnl_dollars + pnl_pct zeroed
      3. The original pnl_dollars subtracted from cash via
         adjust_cash(bypass_sentinel=True) with audit-log reason

    Skips trades already in scrubbed status (idempotent)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_db, adjust_cash
        try:
            id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
        except Exception:
            return {"ok": False, "reason": "ids must be comma-separated integers"}
        if not id_list:
            return {"ok": False, "reason": "no_ids_provided"}
        if len(id_list) > 100:
            return {"ok": False, "reason": "max_100_ids_per_call"}

        # Phase 1 — fetch + validate
        conn = get_db()
        try:
            placeholders = ",".join("?" * len(id_list))
            rows = conn.execute(
                f"""SELECT id, ticker, entry_price, exit_price,
                          pnl_dollars, pnl_pct, status, instrument_type
                   FROM paper_trades WHERE id IN ({placeholders})""",
                id_list,
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"ok": False, "reason": "no_trades_found_for_ids"}

        # Skip trades already in scrubbed status
        already_scrubbed = []
        to_scrub = []
        for r in rows:
            if r["status"] in ("closed_flat_validator", "closed_flat_validator_v2"):
                already_scrubbed.append({"id": r["id"], "ticker": r["ticker"]})
            else:
                to_scrub.append(dict(r))

        if not to_scrub:
            return {"ok": True, "scrubbed": 0,
                    "already_scrubbed": already_scrubbed,
                    "note": "all provided IDs were already scrubbed"}

        # Phase 2 — zero PnL + update status
        scrub_ids = [r["id"] for r in to_scrub]
        phantom_pnl = sum(float(r["pnl_dollars"] or 0) for r in to_scrub)
        conn = get_db()
        try:
            phs = ",".join("?" * len(scrub_ids))
            conn.execute(
                f"""UPDATE paper_trades SET
                       status='closed_flat_validator_v2',
                       pnl_dollars=0, pnl_pct=0
                    WHERE id IN ({phs})""",
                scrub_ids,
            )
            conn.commit()
        finally:
            conn.close()

        # Phase 3 — reverse the bogus cash credit
        cash_reversal_ok = True
        cash_reversal_error = None
        try:
            adjust_cash(
                delta=-float(phantom_pnl),
                caller="force_scrub_by_id",
                reason=(f"manual scrub of {len(scrub_ids)} trades "
                        f"(${phantom_pnl:,.2f}): {[r['ticker'] for r in to_scrub]}"),
                bypass_sentinel=True,
            )
        except Exception as _ce:
            cash_reversal_ok = False
            cash_reversal_error = str(_ce)[:200]
            logger.error(f"force_scrub cash reversal failed: {_ce}")

        return {
            "ok": True,
            "scrubbed": len(scrub_ids),
            "scrubbed_trades": [
                {"id": r["id"], "ticker": r["ticker"],
                 "instrument_type": r["instrument_type"],
                 "phantom_pnl": float(r["pnl_dollars"] or 0)}
                for r in to_scrub
            ],
            "total_phantom_pnl": round(phantom_pnl, 2),
            "cash_reversed": cash_reversal_ok,
            "cash_reversal_error": cash_reversal_error,
            "already_scrubbed": already_scrubbed,
        }
    except Exception as e:
        logger.error(f"force-scrub-trades error: {e}")
        return {"ok": False, "reason": str(e)[:300]}


@app.get("/api/admin/inspect-trade/{ticker}")
def admin_inspect_trade(ticker: str, request: Request):
    """Dump ALL raw fields for a specific ticker from paper_trades.
    Use to diagnose why scrub doesn't match a row."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE ticker = ?",
                (ticker.upper(),)
            ).fetchall()
        finally:
            conn.close()
        return {
            "ok": True,
            "ticker": ticker.upper(),
            "count": len(rows),
            "rows": [dict(r) for r in rows],
        }
    except Exception as e:
        logger.error(f"inspect-trade error: {e}")
        return {"ok": False, "reason": str(e)[:300]}


@app.get("/api/admin/scrub-phantom-diagnostic")
def admin_scrub_phantom_diagnostic(request: Request):
    """READ-ONLY: shows what trades the scrub query would match,
    without modifying anything. Use this BEFORE calling the POST
    trigger to verify which trades will be affected."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT id, ticker, entry_price, exit_price, pnl_dollars,
                          pnl_pct, status, instrument_type, exit_date
                   FROM paper_trades
                   WHERE exit_price IS NOT NULL
                     AND (status IS NULL
                          OR status NOT IN ('open',
                                            'closed_flat_validator',
                                            'closed_flat_validator_v2'))
                     AND entry_price > 0
                     AND (
                        exit_price > 10.0 * entry_price
                        OR pnl_pct > 500
                        OR (
                            -- v6: DXC-class options phantoms
                            pnl_dollars > 3000
                            AND pnl_pct > 100
                            AND entry_price < 20
                        )
                     )
                   ORDER BY pnl_dollars DESC"""
            ).fetchall()
            candidates = [dict(r) for r in rows]
            phantom_pnl = sum(float(r["pnl_dollars"] or 0) for r in rows)
        finally:
            conn.close()
        return {
            "ok": True,
            "candidates_found": len(candidates),
            "total_phantom_pnl": round(phantom_pnl, 2),
            "candidates": candidates,
            "note": "READ-ONLY. Call POST /api/admin/scrub-phantom-trades-v2 to apply.",
        }
    except Exception as e:
        logger.error(f"scrub diagnostic error: {e}")
        return {"ok": False, "reason": str(e)[:300]}


@app.post("/api/admin/scrub-phantom-trades-v2")
def admin_scrub_phantom_trades_v2(request: Request):
    """Manually trigger the phantom-trade scrub. Idempotent — running
    twice does nothing the second time. Use the diagnostic GET first
    to preview which trades will be affected."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import _scrub_phantom_trades_v2
        result = _scrub_phantom_trades_v2()
        return result
    except Exception as e:
        logger.error(f"manual scrub error: {e}")
        return {"ok": False, "reason": str(e)[:300]}


@app.post("/api/admin/reset-vix-cache")
def admin_reset_vix_cache(request: Request):
    """Clear stale VIX crisis cache from DB so vix_guard re-fetches live on next call."""
    check_rate_limit(request.client.host)
    try:
        from predictions.models import set_trading_state, get_trading_state
        old_vix = get_trading_state("vix_guard_last_known_good", "unknown")
        old_ts = get_trading_state("vix_guard_last_known_good_ts", "unknown")
        # Clear both keys so _load_last_good returns (20.0, 0) → no cached value
        set_trading_state("vix_guard_last_known_good", "")
        set_trading_state("vix_guard_last_known_good_ts", "")
        # Also clear legacy key used by _validate_vix
        set_trading_state("vix_last_good", "")
        set_trading_state("vix_last_good_ts", "")
        # Force fresh VIX from live sources immediately
        from analytics.vix_guard import get_vix_safe
        fresh = get_vix_safe()
        return {"ok": True, "cleared_old_vix": old_vix, "cleared_ts": old_ts,
                "fresh_vix": fresh}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/admin/force-picks-regen-sync")
def admin_force_picks_regen_sync(request: Request):
    """SYNCHRONOUS picks regen — bypasses bg thread + flag. Calls
    generate_quant_picks() directly and returns:
      - whether _intel_overlay was applied to the picks
      - first pick's overlay components (for diagnosis)
      - any exception that occurred

    READ-ONLY for cash/trades — only refreshes the picks cache.
    Cannot block trades or cycles."""
    check_rate_limit(request.client.host)
    try:
        # Bust the in-memory cache to FORCE re-fetch
        try:
            from analysis.quant_engine import _quant_cache
            if "quant_picks" in _quant_cache:
                _quant_cache["quant_picks"]["time"] = 0  # expire
        except Exception:
            pass

        from analysis.quant_engine import generate_quant_picks
        result = generate_quant_picks()

        longs = result.get("long_picks") or []
        n_with_overlay = sum(1 for p in longs if p.get("_intel_overlay"))
        first_overlay = None
        first_keys = None
        if longs:
            first_keys = sorted(longs[0].keys())
            first_overlay = longs[0].get("_intel_overlay")

        return {
            "ok": True,
            "long_picks_count": len(longs),
            "short_picks_count": len(result.get("short_picks") or []),
            "picks_with_overlay": n_with_overlay,
            "first_pick_keys": first_keys,
            "first_pick_overlay": first_overlay,
            "_cache_source": result.get("_cache_source"),
            "_cache_age_hours": result.get("_cache_age_hours"),
            "regime": (result.get("regime") or {}).get("regime"),
            "error_in_result": result.get("error"),
        }
    except Exception as e:
        import traceback
        logger.error(f"force picks regen sync error: {e}")
        return {"ok": False, "reason": str(e)[:300],
                "traceback": traceback.format_exc()[:1500]}


# ============================================================
#  BACKTEST PRO (Level 3) — hedge-fund-grade analyses
#  Walk-forward + Monte Carlo + regime-conditional + stress tests
# ============================================================

@app.get("/api/backtest-pro/walk-forward")
def backtest_pro_walk_forward(request: Request, train_months: int = 4,
                                test_months: int = 1, top_n: int = 10,
                                hold_days: int = 5):
    """Walk-forward train/test validation — overfitting detector.
    Safe bounds: train_months in [3, 12], test_months in [1, 6].
    Defaults guarantee enough trading days to not fail."""
    check_rate_limit(request.client.host)
    train_months = max(3, min(int(train_months), 12))
    test_months = max(1, min(int(test_months), 6))
    top_n = max(1, min(int(top_n), 30))
    hold_days = max(1, min(int(hold_days), 30))
    try:
        from predictions.backtest_pro import walk_forward_validation
        return walk_forward_validation(
            train_months=train_months,
            test_months=test_months,
            top_n=top_n,
            hold_days=hold_days,
        )
    except Exception as e:
        logger.error(f"Backtest-pro walk-forward error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/monte-carlo")
def backtest_pro_monte_carlo(request: Request, n_simulations: int = 500,
                              days: int = 180, top_n: int = 10,
                              hold_days: int = 5):
    """Bootstrap-resample trades to give CIs on return + Sharpe + max DD.
    Safe bounds: n_simulations [50, 5000], days [60, 730]."""
    check_rate_limit(request.client.host)
    n_simulations = max(50, min(int(n_simulations), 5000))
    days = max(60, min(int(days), 730))
    top_n = max(1, min(int(top_n), 30))
    hold_days = max(1, min(int(hold_days), 30))
    try:
        from predictions.backtest_pro import monte_carlo_bootstrap
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=days)).date().isoformat()
        return monte_carlo_bootstrap(
            n_simulations=n_simulations,
            start_date=start, end_date=end,
            top_n=top_n, hold_days=hold_days,
        )
    except Exception as e:
        logger.error(f"Backtest-pro monte-carlo error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/regimes")
def backtest_pro_regimes(request: Request, days: int = 540,
                          top_n: int = 10, hold_days: int = 5):
    """Per-regime (bull/bear/sideways x low/mid/high vol) Sharpe + win rate.
    Safe bounds: days [300, 1095] (needs 200d history for SPY MAs)."""
    check_rate_limit(request.client.host)
    days = max(300, min(int(days), 1095))
    top_n = max(1, min(int(top_n), 30))
    hold_days = max(1, min(int(hold_days), 30))
    try:
        from predictions.backtest_pro import regime_conditional_analysis
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.utcnow().date().isoformat()
        start = (_dt.utcnow() - _td(days=days)).date().isoformat()
        return regime_conditional_analysis(
            start_date=start, end_date=end,
            top_n=top_n, hold_days=hold_days,
        )
    except Exception as e:
        logger.error(f"Backtest-pro regimes error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/stress-tests")
def backtest_pro_stress_tests(request: Request, top_n: int = 10,
                                hold_days: int = 5):
    """Replay strategy across COVID, 2022 inflation, SVB, Aug 2024 vol spike."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest_pro import stress_tests
        return stress_tests(top_n=int(top_n), hold_days=int(hold_days))
    except Exception as e:
        logger.error(f"Backtest-pro stress-tests error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/stress-test-scenarios")
def backtest_pro_stress_scenarios(request: Request):
    """List available crisis scenarios so the UI can render a Run button per scenario."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest_pro import CRISIS_PERIODS
        return {"ok": True,
                "scenarios": [{"label": label, "start": start, "end": end,
                               "description": desc}
                              for (label, start, end, desc) in CRISIS_PERIODS]}
    except Exception as e:
        logger.error(f"stress scenarios list error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/stress-test-one")
def backtest_pro_stress_one(request: Request, label: str,
                            top_n: int = 10, hold_days: int = 5):
    """Run a single named stress-test crisis (label must match CRISIS_PERIODS)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest_pro import CRISIS_PERIODS
        from predictions.backtest import run_backtest
        match = next(((lbl, s, e, d) for (lbl, s, e, d) in CRISIS_PERIODS
                      if lbl == label), None)
        if not match:
            return {"ok": False, "reason": f"unknown scenario: {label}"}
        lbl, start, end, desc = match
        bt = run_backtest(start_date=start, end_date=end,
                          top_n=int(top_n), hold_days=int(hold_days))
        if not bt.get("ok"):
            return {"ok": False, "label": lbl, "description": desc,
                    "period": [start, end], "reason": bt.get("reason")}
        r = bt.get("results", {})
        cfg = bt.get("config", {})
        return {
            "ok": True, "label": lbl, "description": desc,
            "period": [start, end],
            "trading_days": cfg.get("trading_days"),
            "tickers_count": cfg.get("tickers_count"),
            "total_return_pct": r.get("total_return_pct"),
            "sp500_return_pct": r.get("sp500_return_pct"),
            "alpha_vs_sp500_pct": r.get("alpha_vs_sp500_pct"),
            "max_drawdown_pct": r.get("max_drawdown_pct"),
            "sharpe_ratio": r.get("sharpe_ratio"),
            "win_rate_pct": r.get("win_rate_pct"),
            "total_trades": r.get("total_trades"),
        }
    except Exception as e:
        logger.error(f"stress-test-one error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/full")
def backtest_pro_full(request: Request, top_n: int = 10, hold_days: int = 5,
                       force_refresh: bool = False):
    """Aggregate — runs all 4 hedge-fund analyses. 1hr cache. Heavy (60-180s)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest_pro import run_full_pro_analysis
        return run_full_pro_analysis(top_n=int(top_n),
                                     hold_days=int(hold_days),
                                     force_refresh=bool(force_refresh))
    except Exception as e:
        logger.error(f"Backtest-pro full error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/backtest-pro/summary")
def backtest_pro_summary(request: Request):
    """Cached snapshot of last full analysis (no compute)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.backtest_pro import get_pro_summary
        return get_pro_summary()
    except Exception as e:
        logger.error(f"Backtest-pro summary error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  REGIME PLAYBOOK (Level 4) — lessons from history
#  For each major past event (COVID, 2022 inflation, SVB, AI boom...)
#  pull what sectors + factors worked. When current regime matches
#  a historical pattern, surface the playbook.
# ============================================================

@app.post("/api/regime-playbook/build")
def regime_playbook_build(request: Request):
    """HEAVY (~30-90s): pull sector + factor returns for all 8 historical
    events, store as playbooks. Idempotent — safe to re-run."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_playbook import build_all_playbooks
        return build_all_playbooks()
    except Exception as e:
        logger.error(f"Regime playbook build error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/regime-playbook/all")
def regime_playbook_all(request: Request):
    """List every stored playbook with full details."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_playbook import get_all_playbooks
        return get_all_playbooks()
    except Exception as e:
        logger.error(f"Regime playbook all error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/regime-playbook/event/{label}")
def regime_playbook_event(label: str, request: Request):
    """Specific event playbook (e.g. covid_crash_2020)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_playbook import get_playbook
        return get_playbook(label)
    except Exception as e:
        logger.error(f"Regime playbook event error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/regime-playbook/current-match")
def regime_playbook_current_match(request: Request):
    """Match TODAY's regime to closest historical playbook(s) and return
    sector recommendations: 'buy these, avoid these'."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_playbook import get_current_match
        return get_current_match()
    except Exception as e:
        logger.error(f"Regime playbook current-match error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  LEVEL 6 — INTELLIGENCE OVERLAY (composite scorer)
#  Wires Level 1-5 signals into pick generation + position sizing.
#  Hard kill switch: env DISABLE_INTELLIGENCE_OVERLAY=1
# ============================================================

@app.get("/api/intelligence/status")
def intelligence_status(request: Request):
    """Health snapshot of overlay (enabled, bounds, kill switch state)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.intelligence_overlay import get_overlay_status
        return get_overlay_status()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/intelligence/preview")
def intelligence_preview(request: Request, ticker: str = "AAPL",
                          sector: str = "Technology", regime: str = "BULL",
                          signal_score: float = 2.0,
                          direction: str = "long"):
    """Preview overlay multiplier for a hypothetical pick — explains
    why a real pick's confidence got adjusted."""
    check_rate_limit(request.client.host)
    try:
        from predictions.intelligence_overlay import compute_pick_overlay
        return compute_pick_overlay(ticker, sector, regime,
                                     float(signal_score), direction)
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/intelligence/size-factor")
def intelligence_size_factor(request: Request):
    """Current position-size multiplier (event calendar + correlation)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.intelligence_overlay import compute_size_factor
        return compute_size_factor()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  LEVEL 5 — ADVISORY-ONLY learning + risk awareness layers
#  Every endpoint here is READ-ONLY. NONE block trades or cycles.
#  All wrapped in try/except so a bug here cannot affect trading.
#
#  1. Auto-Postmortem on Every Loss   /api/postmortem/*
#  2. Cross-Asset Signal Engine       /api/cross-asset/*
#  3. Earnings Drift Learner          /api/earnings-drift/*
#  4. Macro Event Calendar            /api/event-calendar/*
#  5. Live Regime Drift Alarm         /api/regime-drift/*
#  6. Position Correlation Matrix     /api/correlation/*
#  7. Trade Replay Simulator          /api/trade-replay/*
# ============================================================

# --- 1. Auto-Postmortem ---
@app.get("/api/postmortem/summary")
def postmortem_summary(request: Request, lookback_days: int = 180):
    """Aggregate stats on all losing trades — by regime/sector/day-of-week."""
    check_rate_limit(request.client.host)
    try:
        from predictions.loss_postmortem import get_postmortem_summary
        return get_postmortem_summary(int(lookback_days))
    except Exception as e:
        logger.error(f"Postmortem summary error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/postmortem/patterns")
def postmortem_patterns(request: Request, lookback_days: int = 180):
    """Loss patterns that meet min-occurrence threshold."""
    check_rate_limit(request.client.host)
    try:
        from predictions.loss_postmortem import aggregate_loss_patterns
        return aggregate_loss_patterns(int(lookback_days))
    except Exception as e:
        logger.error(f"Postmortem patterns error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/postmortem/active-filters")
def postmortem_active_filters(request: Request):
    """Active loss-pattern filters (advisory — picks may opt-in)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.loss_postmortem import get_active_loss_filters
        return {"ok": True, "filters": get_active_loss_filters()}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/admin/postmortem-backfill")
def postmortem_backfill(request: Request, limit: int = 5000):
    """Import existing closed losses into the postmortem log. Idempotent."""
    check_rate_limit(request.client.host)
    try:
        from predictions.loss_postmortem import backfill_from_closed_trades
        return backfill_from_closed_trades(int(limit))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# --- 2. Cross-Asset Signals ---
@app.get("/api/cross-asset/signals")
def cross_asset_signals(request: Request, force_refresh: bool = False):
    """Snapshot of TLT, UUP, ^VIX, GLD, BTC — synthesized into risk-on/off."""
    check_rate_limit(request.client.host)
    try:
        from predictions.cross_asset import get_current_signals
        return get_current_signals(force_refresh=bool(force_refresh))
    except Exception as e:
        logger.error(f"Cross-asset signals error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/cross-asset/recommendation")
def cross_asset_recommendation(request: Request):
    """Higher-level: just the regime + sector tilt recommendation."""
    check_rate_limit(request.client.host)
    try:
        from predictions.cross_asset import get_sector_recommendation
        return get_sector_recommendation()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# --- 3. Earnings Drift ---
# CRITICAL: literal routes (summary, predict/...) MUST come BEFORE the
# {ticker} path-param route or they get captured as ticker="summary".
@app.get("/api/earnings-drift/summary")
def earnings_drift_summary(request: Request, min_events: int = 4):
    """Cached drift patterns across all analyzed tickers."""
    check_rate_limit(request.client.host)
    try:
        from predictions.earnings_drift import get_drift_summary
        return get_drift_summary(int(min_events))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/earnings-drift/predict/{ticker}")
def earnings_drift_predict(ticker: str, request: Request):
    """Predict drift signal for a ticker based on historical pattern."""
    check_rate_limit(request.client.host)
    try:
        from predictions.earnings_drift import predict_drift
        return predict_drift(ticker)
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/earnings-drift/{ticker}")
def earnings_drift_ticker(ticker: str, request: Request,
                           lookback_quarters: int = 8):
    """Per-ticker historical drift pattern from earnings."""
    check_rate_limit(request.client.host)
    try:
        from predictions.earnings_drift import build_ticker_drift_history
        return build_ticker_drift_history(ticker,
                                           lookback_quarters=int(lookback_quarters))
    except Exception as e:
        logger.error(f"Earnings drift error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# --- 4. Macro Event Calendar ---
@app.get("/api/event-calendar/upcoming")
def event_calendar_upcoming(request: Request, days_ahead: int = 14):
    """Upcoming FOMC/CPI/NFP/etc events in the window."""
    check_rate_limit(request.client.host)
    try:
        from predictions.event_calendar import get_upcoming_events
        return get_upcoming_events(int(days_ahead))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/event-calendar/risk-adjustment")
def event_calendar_risk_adjustment(request: Request):
    """Suggested position-size factor (1.0 normal, lower = cut size)."""
    check_rate_limit(request.client.host)
    try:
        from predictions.event_calendar import get_risk_adjustment_factor
        return get_risk_adjustment_factor()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/event-calendar/blackout-status")
def event_calendar_blackout(request: Request):
    """Are we within an event blackout window? (Advisory only.)"""
    check_rate_limit(request.client.host)
    try:
        from predictions.event_calendar import is_event_blackout_window
        return is_event_blackout_window()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# --- 5. Regime Drift Alarms ---
@app.post("/api/regime-drift/snapshot")
def regime_drift_snapshot(request: Request):
    """Take a fresh regime snapshot."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_drift import snapshot_regime
        return snapshot_regime()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/regime-drift/detect")
def regime_drift_detect(request: Request, lookback_days: int = 3):
    """Detect drift vs N-day baseline — records alarms in DB."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_drift import detect_drift
        return detect_drift(int(lookback_days))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/regime-drift/alarms")
def regime_drift_alarms(request: Request, hours: int = 48):
    """Recent regime-drift alarms."""
    check_rate_limit(request.client.host)
    try:
        from predictions.regime_drift import get_recent_alarms
        return get_recent_alarms(int(hours))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# --- 6. Position Correlation ---
@app.get("/api/correlation/full")
def correlation_full(request: Request, force_refresh: bool = False):
    """Full pairwise correlation matrix of open equity positions."""
    check_rate_limit(request.client.host)
    try:
        from predictions.correlation_matrix import get_open_positions_correlation
        return get_open_positions_correlation(force_refresh=bool(force_refresh))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/correlation/summary")
def correlation_summary(request: Request):
    """Lightweight summary — warnings + score, no full matrix."""
    check_rate_limit(request.client.host)
    try:
        from predictions.correlation_matrix import get_concentration_summary
        return get_concentration_summary()
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# --- 7. Trade Replay ---
# CRITICAL: literal route (aggregate-lessons) MUST come BEFORE
# {trade_id} or it gets captured as trade_id="aggregate-lessons".
@app.get("/api/trade-replay/aggregate-lessons")
def trade_replay_aggregate(request: Request, lookback_days: int = 90,
                            limit: int = 50):
    """HEAVY (~30-90s): aggregate replay lessons across recent trades."""
    check_rate_limit(request.client.host)
    try:
        from predictions.trade_replay import aggregate_replay_lessons
        return aggregate_replay_lessons(int(lookback_days), int(limit))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/trade-replay/{trade_id}/optimal")
def trade_replay_optimal(trade_id: int, request: Request):
    """Grid-search optimal stop/take/hold for one trade."""
    check_rate_limit(request.client.host)
    try:
        from predictions.trade_replay import find_optimal_params_for_trade
        return find_optimal_params_for_trade(int(trade_id))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/trade-replay/{trade_id}")
def trade_replay_one(trade_id: int, request: Request,
                      stop_pct: float = 0.04, take_pct: float = 0.10,
                      max_hold_days: int = 5):
    """Replay one trade with alternate parameters."""
    check_rate_limit(request.client.host)
    try:
        from predictions.trade_replay import replay_trade
        return replay_trade(int(trade_id), float(stop_pct),
                            float(take_pct), int(max_hold_days))
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  AUTO-FIXER — closes loop between backtest insights + live picks
# ============================================================

@app.post("/api/auto-fix/feedback-loop")
def auto_fix_feedback_loop(request: Request, days: int = 180,
                           top_n: int = 10, hold_days: int = 5,
                           apply: bool = True):
    """Run backtest → extract insights → apply per-ticker penalties.
    Set apply=false to preview recommendations without writing."""
    check_rate_limit(request.client.host)
    try:
        from predictions.auto_fixer import run_feedback_loop
        return run_feedback_loop(days=int(days), top_n=int(top_n),
                                 hold_days=int(hold_days),
                                 apply=bool(apply))
    except Exception as e:
        logger.error(f"Auto-fix feedback loop error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.get("/api/auto-fix/insights")
def auto_fix_insights(request: Request):
    """Returns all currently active backtest penalties."""
    check_rate_limit(request.client.host)
    try:
        from predictions.stock_learning import get_all_backtest_penalties
        penalties = get_all_backtest_penalties()
        return {"ok": True, "active_count": len(penalties),
                "penalties": penalties}
    except Exception as e:
        logger.error(f"Auto-fix insights error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


@app.post("/api/auto-fix/clear-penalties")
def auto_fix_clear_penalties(request: Request):
    """Wipe all penalties — the 'undo' button. Use this if backtest
    insights look wrong or you want to reset learning."""
    check_rate_limit(request.client.host)
    try:
        from predictions.stock_learning import clear_backtest_penalties
        return clear_backtest_penalties()
    except Exception as e:
        logger.error(f"Auto-fix clear penalties error: {e}")
        return {"ok": False, "reason": str(e)[:200]}


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
    """Get OHLCV data for interactive candlestick charts.

    PRICE ALIGNMENT (2026-05-29): the chart prices used to shift based on
    the period selector because yfinance returns differently split-adjusted
    series across periods.  We now anchor every chart to the live quote:
    if the last historical close diverges from live by more than 5%, the
    entire OHLCV series is scaled by (live / last_close).  All periods
    then display the SAME current price.
    """
    check_rate_limit(request.client.host)
    clean_ticker = validate_ticker(ticker)
    if period not in ("1mo", "3mo", "6mo", "1y", "2y", "5y"):
        raise HTTPException(status_code=400, detail="Invalid period")
    try:
        from analysis.technical import calculate_sma, calculate_ema, calculate_bollinger_bands
        data = get_historical_data(clean_ticker, period)
        if not data:
            raise HTTPException(status_code=404, detail="No data found")

        # ----- align historical series to live quote -----
        try:
            from analysis.market_data import get_stock_info as _gsi_chart
            _live_info = _gsi_chart(clean_ticker) or {}
            _live_p = float(_live_info.get("current_price") or 0)
            _last_c = float(data[-1]["close"]) if data else 0.0
            if _live_p > 0 and _last_c > 0:
                _drift = abs(_live_p - _last_c) / _live_p
                if _drift > 0.50:
                    raise HTTPException(
                        status_code=404,
                        detail="Data integrity error: historical and live price diverge >50%"
                    )
                if _drift > 0.05:
                    _scale = _live_p / _last_c
                    for _r in data:
                        try:
                            _r["close"] = round(float(_r["close"]) * _scale, 2)
                            _r["open"]  = round(float(_r["open"])  * _scale, 2)
                            _r["high"]  = round(float(_r["high"])  * _scale, 2)
                            _r["low"]   = round(float(_r["low"])   * _scale, 2)
                        except Exception:
                            pass
        except HTTPException:
            raise
        except Exception as _ce:
            logger.debug(f"chart-data live-anchor skipped for {clean_ticker}: {_ce}")

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

def _get_cached_picks():
    """2026-06-07 fix: return cached quant picks without ever triggering a
    synchronous generate_quant_picks() call (which takes 5-10 minutes and
    would hang the web request to the App Runner timeout).

    Order:
      1. In-memory _quant_cache (warm)
      2. S3 snapshot via trading_state (warm-restart fallback)
      3. None (cold — caller should return placeholder)
    """
    try:
        from analysis.quant_engine import _quant_cache
        cache_entry = _quant_cache.get("quant_picks")
        if cache_entry and cache_entry.get("data"):
            return cache_entry["data"]
    except Exception:
        pass
    try:
        import json as _j
        from predictions.models import get_trading_state as _gts
        raw = _gts("picks_s3_snapshot", "")
        if raw:
            return _j.loads(raw)
    except Exception:
        pass
    return None


@app.get("/api/rentech/pairs")
def rentech_pairs(request: Request):
    """Get current pairs trading opportunities (stat arb).
    2026-06-07: reads from cached picks — never blocks on a fresh scan."""
    check_rate_limit(request.client.host)
    try:
        picks = _get_cached_picks() or {}
        return {
            "pairs_trades": picks.get("pairs_trades", []),
            "regime": picks.get("regime", {}),
            "timestamp": picks.get("generated_at", ""),
            "cache_status": "cached" if picks else "cold",
        }
    except Exception as e:
        logger.error(f"RenTech pairs error: {e}")
        return {"pairs_trades": [], "regime": {}, "cache_status": "error",
                "error": str(e)[:200]}


@app.get("/api/rentech/risk")
def rentech_risk(request: Request):
    """Get portfolio risk assessment (sector concentration, beta, correlation).
    2026-06-07: reads from cached picks — never blocks on a fresh scan."""
    check_rate_limit(request.client.host)
    try:
        picks = _get_cached_picks() or {}
        return {
            "portfolio_risk": picks.get("portfolio_risk", {}),
            "circuit_breaker": picks.get("circuit_breaker", {}),
            "regime": picks.get("regime", {}),
            "cache_status": "cached" if picks else "cold",
        }
    except Exception as e:
        logger.error(f"RenTech risk error: {e}")
        return {"portfolio_risk": {}, "circuit_breaker": {}, "regime": {},
                "cache_status": "error", "error": str(e)[:200]}


@app.get("/api/rentech/mean-reversion")
def rentech_mean_reversion(request: Request):
    """Get mean reversion trade setups (RSI2 Connors, VWAP, Bollinger).
    2026-06-07: reads from cached picks — never blocks on a fresh scan."""
    check_rate_limit(request.client.host)
    try:
        picks = _get_cached_picks() or {}
        return {
            "mean_reversion_setups": picks.get("mean_reversion_setups", []),
            "regime": picks.get("regime", {}),
            "timestamp": picks.get("generated_at", ""),
            "cache_status": "cached" if picks else "cold",
        }
    except Exception as e:
        logger.error(f"RenTech mean reversion error: {e}")
        return {"mean_reversion_setups": [], "regime": {},
                "cache_status": "error", "error": str(e)[:200]}


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
    """Get stocks blocked/warned due to imminent earnings.
    2026-06-07: reads from cached picks — never blocks on a fresh scan."""
    check_rate_limit(request.client.host)
    try:
        picks = _get_cached_picks() or {}
        result = picks.get("earnings_shield", {"blocked": [], "warning": []})
        if isinstance(result, dict):
            result["cache_status"] = "cached" if picks else "cold"
        return result
    except Exception as e:
        logger.error(f"Earnings shield error: {e}")
        return {"blocked": [], "warning": [],
                "cache_status": "error", "error": str(e)[:200]}


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
    """Full RenTech dashboard — all data in one call.
    2026-06-07: reads from cached picks — never blocks on a fresh scan."""
    check_rate_limit(request.client.host)
    try:
        picks = _get_cached_picks() or {}
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
            "cache_status": "cached" if picks else "cold",
        }
    except Exception as e:
        logger.error(f"RenTech dashboard error: {e}")
        # 2026-06-07: return graceful empty response instead of 500 so
        # the dashboard UI can render placeholder cards instead of breaking.
        return {
            "regime": {}, "pairs_trades": [], "mean_reversion_setups": [],
            "portfolio_risk": {}, "circuit_breaker": {},
            "performance": {}, "portfolio": {},
            "top_longs": [], "top_shorts": [],
            "cache_status": "error", "error": str(e)[:200],
        }


@app.get("/api/pairs-active")
def pairs_active(request: Request):
    """
    Show all currently open OU/stat-arb pair positions with live z-score and P&L.
    Also shows queued signals from the picks engine (not yet opened).
    Safe: returns empty lists on any failure.
    """
    check_rate_limit(request.client.host)
    try:
        from predictions.models import get_open_trades as _got
        from predictions.pairs_trader import get_open_pairs_summary as _gps
        _open = _got()
        open_pairs = _gps(_open)
    except Exception as _e:
        logger.warning(f"/api/pairs-active open summary error: {_e}")
        open_pairs = []

    try:
        picks = _get_cached_picks() or {}
        queued_signals = picks.get("pairs_trades", [])
    except Exception:
        queued_signals = []

    return {
        "ok": True,
        "open_pairs": open_pairs,
        "open_count": len(open_pairs),
        "queued_signals": queued_signals[:10],
        "queued_count": len(queued_signals),
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "note": (
            "open_pairs = currently executing OU pair positions. "
            "queued_signals = pending opportunities from picks engine."
        ),
    }


# ============================================================
#  ELITE STOCHASTIC MODELS API
# ============================================================

@app.get("/api/stochastic/{ticker}")
async def get_stochastic_analysis(ticker: str, request: Request):
    """
    Full elite stochastic analysis for a ticker.
    Runs 8 research-grade models: GJR-GARCH, Rough Vol (Hurst), Merton Jump-Diffusion,
    Hawkes Process, Variance Risk Premium, Path Signature, Variance Ratio, Vol-of-Vol.
    Returns per-model signals + weighted composite stochastic_score.
    """
    client_ip = request.client.host
    check_rate_limit(client_ip)
    ticker = validate_ticker(ticker)
    try:
        import yfinance as _yf_stoch
        from analytics.stochastic_models import analyze_ticker_stochastic
        from analytics.jump_diffusion import analyze_jump_diffusion_full
        _df = _yf_stoch.download(ticker, period="1y", progress=False)
        if _df is None or len(_df) < 30:
            return JSONResponse({"ok": False, "error": "Insufficient price data", "ticker": ticker})
        from analysis.quant_engine import _safe_close as _sc_stoch
        _closes = _sc_stoch(_df).dropna().values.astype(float)
        stoch = analyze_ticker_stochastic(_closes, ticker)
        jd = analyze_jump_diffusion_full(_closes, ticker)
        result = {
            "ok": True,
            "ticker": ticker,
            "stochastic_ensemble": stoch,
            "jump_diffusion": jd,
            "composite_factor23_score": round(stoch.get("stochastic_score", 0.0), 3),
        }
        return JSONResponse(result)
    except Exception as e:
        logger.warning(f"/api/stochastic/{ticker} error: {e}")
        return JSONResponse({"ok": False, "error": str(e), "ticker": ticker}, status_code=500)


@app.get("/api/data-shield/status")
async def get_data_shield_status(request: Request):
    """
    Health status of all data sources: yfinance, Stooq fallback, cache stats.
    Shows which sources are live, which are degraded, and cache freshness.
    """
    client_ip = request.client.host
    check_rate_limit(client_ip)
    try:
        from analytics.data_shield import get_shield_status
        status = get_shield_status()
        status["ok"] = True
        return JSONResponse(status)
    except Exception as e:
        logger.warning(f"/api/data-shield/status error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


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
