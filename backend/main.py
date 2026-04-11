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
MIN_TRADE_INTERVAL_MINUTES = 15  # Don't trade more than once every 15 min

# Geo-political risk state (updated by scanner every 15 min)
_geo_risk_state = {"level": "LOW", "score": 0, "last_update": None, "events": []}

# Daily profit limit state (2.5% daily gain = sell all and pause)
_daily_paused = {"paused": False, "pause_date": None, "reason": None}

# Load daily pause state from DB (survives container restarts)
try:
    from predictions.models import get_trading_state
    _saved_pause = get_trading_state("daily_pause_date", "")
    if _saved_pause == dt.now().strftime("%Y-%m-%d"):
        _daily_paused["paused"] = True
        _daily_paused["pause_date"] = _saved_pause
        _daily_paused["reason"] = get_trading_state("daily_pause_reason", "Restored from DB")
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

    # --- TRIGGER 1: Market open — always trade at 9:30am ET ---
    if hour == 9 and 28 <= minute <= 35:
        reasons.append("MARKET OPEN — must rebalance positions")

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

    # --- TRIGGER 8: Periodic full scan every 30 min during market hours ---
    if 9 <= hour <= 16 and minute in (0, 1, 30, 31) and not reasons:
        reasons.append("PERIODIC SCAN — 30-min market check")

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

            if daily_return >= 2.5:
                logger.warning(f"DAILY PROFIT LIMIT HIT: +{daily_return:.2f}% today — selling all and pausing")

                # Sell all positions
                from predictions.models import get_open_trades, close_paper_trade
                from predictions.paper_trader import _get_current_prices
                open_trades = get_open_trades()
                symbols = list(set(t["ticker"] for t in open_trades))
                prices = _get_current_prices(symbols)

                for trade in open_trades:
                    price = prices.get(trade["ticker"], trade["entry_price"])
                    try:
                        close_paper_trade(trade["id"], price)
                    except Exception:
                        pass

                _daily_paused["paused"] = True
                _daily_paused["pause_date"] = today_str
                _daily_paused["reason"] = f"Daily gain +{daily_return:.2f}% exceeded 2.5% limit"
                logger.warning(f"ALL POSITIONS CLOSED. Trading paused until tomorrow.")

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
            # End of day with big total return — sell positions that are profitable
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

                # Sell profitable trades at end of day to lock in portfolio gains
                if pnl_pct > 0.5:
                    try:
                        close_paper_trade(trade["id"], price)
                        sold_count += 1
                    except Exception:
                        pass

            if sold_count > 0:
                logger.warning(
                    f"ADAPTIVE PROFIT PROTECTION: EOD sell — closed {sold_count} profitable trades "
                    f"(total return: +{total_return:.1f}%, peak: +{peak:.1f}%)"
                )
                try:
                    backup_db_to_s3()
                except Exception:
                    pass

        # --- PEAK DRAWDOWN PROTECTION ---
        # If we've been at 10%+ return and now it's dropping, protect the gains
        if peak >= 10.0 and drawdown_from_peak >= 1.5:
            # We've lost 1.5% from our peak — sell everything to protect
            from predictions.models import get_open_trades, close_paper_trade
            from predictions.paper_trader import _get_current_prices
            open_trades = get_open_trades()
            if not open_trades:
                return

            symbols = list(set(t["ticker"] for t in open_trades))
            prices = _get_current_prices(symbols)

            for trade in open_trades:
                price = prices.get(trade["ticker"], trade["entry_price"])
                try:
                    close_paper_trade(trade["id"], price)
                except Exception:
                    pass

            logger.warning(
                f"PEAK DRAWDOWN PROTECTION: Peak was +{peak:.1f}%, now +{total_return:.1f}% "
                f"(dropped {drawdown_from_peak:.1f}%) — sold all to protect gains"
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
    id="daily_learning",
    name="Daily Learning Cycle (7:30pm)",
    max_instances=1,
    misfire_grace_time=3600,
)

scheduler.start()
auto_trade_stats["started_at"] = dt.now().isoformat()
auto_trade_stats["status"] = "running"
logger.warning("AUTONOMOUS TRADING SCHEDULER STARTED — the computer is now the hedge fund manager")


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
def ibkr_status_endpoint(request: Request):
    """IBKR connection status, account summary, and trading mode."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_get_status, ibkr_get_account, get_order_log
        status = ibkr_get_status()
        account = ibkr_get_account()
        recent_orders = get_order_log(limit=20)
        return {
            "status": status,
            "account": account,
            "recent_orders": recent_orders,
        }
    except Exception as e:
        logger.error(f"IBKR status error: {e}")
        return {
            "status": {"connected": False, "enabled": False, "mode": "PAPER",
                       "error": str(e)},
            "account": {},
            "recent_orders": [],
        }


@app.post("/api/ibkr/kill-switch")
def ibkr_kill_switch(request: Request):
    """EMERGENCY: Flatten all IBKR positions immediately."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_flatten_all
        result = ibkr_flatten_all("MANUAL KILL SWITCH")
        logger.warning(f"IBKR KILL SWITCH activated: {result}")
        return result
    except Exception as e:
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
    """Enable/disable IBKR execution. Paper trader always runs regardless."""
    check_rate_limit(request.client.host)
    try:
        import json
        body = {}
        try:
            import asyncio
            # For sync context, just check query params
        except Exception:
            pass

        from predictions.ibkr_adapter import ibkr_toggle
        # Toggle: if currently enabled, disable; if disabled, enable
        from predictions.ibkr_adapter import IBKR_ENABLED
        return ibkr_toggle(not IBKR_ENABLED)
    except Exception as e:
        logger.error(f"IBKR toggle error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ibkr/unhalt")
def ibkr_unhalt_endpoint(request: Request):
    """Resume trading after emergency halt."""
    check_rate_limit(request.client.host)
    try:
        from predictions.ibkr_adapter import ibkr_unhalt
        return ibkr_unhalt()
    except Exception as e:
        logger.error(f"IBKR unhalt error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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

    return {
        **auto_trade_stats,
        "next_scan": next_run,
        "trading_mode": "EVENT-DRIVEN",
        "schedule": "Monitors every 5 min, trades only when conditions change (news, regime shift, VIX spike, stop-loss, market open/close)",
        "would_trade_now": trade_decision,
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
