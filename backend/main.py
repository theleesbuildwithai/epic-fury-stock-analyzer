"""
Epic Fury Stock Analyzer — Backend API
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
from collections import defaultdict
from datetime import datetime as dt

from analysis.report import generate_full_report
from analysis.market_data import get_stock_info, get_historical_data, get_benchmark_data
from analysis.ticker_search import search_tickers
from analysis.extras import get_banner_data, get_daily_picks, get_earnings_calendar, get_daily_summary, get_sector_heatmap
from analysis.news_sentiment import get_market_news, get_stock_sentiment, assess_geopolitical_risk, assess_tariff_risk
from analysis.ai_analyst import answer_question
from analysis.quant_engine import generate_quant_picks, detect_market_regime, scan_overnight_intelligence, analyze_watchlist_stock, _throttle
from predictions.models import init_db, save_prediction, get_all_predictions
from predictions.tracker import get_performance_stats, check_and_resolve_predictions
from predictions.paper_trader import get_portfolio_state, execute_trades_from_signals, run_backtest, get_performance_analytics, check_and_exit_positions
from predictions.learner import generate_intelligence_report, auto_adjust_weights

logger = logging.getLogger("epic-fury")
logging.basicConfig(level=logging.WARNING)

# ============================================================
#  EPIC FURY APPLICATION FIREWALL (WAF)
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
    # Clean old entries
    rate_limit_store[client_ip] = [t for t in rate_limit_store[client_ip] if now - t < RATE_WINDOW]
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
    title="Epic Fury Stock Analyzer",
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
        news_score = sentiment.get("composite_score", 0)
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

    # --- TRIGGER 7: Periodic full scan every 30 min during market hours ---
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

# Startup scan (after 60s warm-up)
scheduler.add_job(
    _run_auto_trade_cycle,
    "date",
    run_date=dt.now() + __import__("datetime").timedelta(seconds=60),
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
                    new_stop = round(entry * 1.10, 2)    # 10% stop
                    new_target = round(entry * 0.80, 2)  # 20% target

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
    return {"status": "healthy", "app": "Epic Fury Stock Analyzer"}


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
    """Geopolitical risk assessment — military events, wars, sanctions, and their market impact."""
    check_rate_limit(request.client.host)
    try:
        return assess_geopolitical_risk()
    except Exception as e:
        logger.error(f"Geopolitical risk error: {e}")
        return {"risk_level": "UNKNOWN", "risk_score": 0, "error": str(e)}


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
    """Get quantitative LONG/SHORT picks with regime, macro, and factor breakdown."""
    check_rate_limit(request.client.host)
    try:
        return generate_quant_picks()
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
    """Get equity curve data — fund vs S&P 500 since March 30, 2026."""
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

        # Get S&P 500 comparison data since March 30
        sp500_curve = []
        try:
            _throttle()
            spy_df = yf.download("SPY", start="2026-03-28", progress=False)
            if spy_df is not None and not spy_df.empty:
                # Handle MultiIndex columns from yfinance
                if isinstance(spy_df.columns, pd.MultiIndex):
                    spy_df.columns = spy_df.columns.get_level_values(0)
                closes = spy_df["Close"].dropna()
                if isinstance(closes, pd.DataFrame):
                    closes = closes.iloc[:, 0]
                if len(closes) >= 2:
                    base_price = float(closes.iloc[0])
                    for idx, price in closes.items():
                        date_str = str(idx.date()) if hasattr(idx, 'date') else str(idx)[:10]
                        ret = ((float(price) / base_price) - 1) * 100
                        sp500_curve.append({
                            "date": date_str,
                            "value": round(100000 * (1 + ret / 100), 2),
                            "return_pct": round(ret, 2),
                        })
        except Exception as e:
            logger.error(f"S&P 500 data error: {e}")

        # Get current portfolio state for latest data point
        try:
            portfolio = get_portfolio_state()
            today = dt.now().strftime("%Y-%m-%d")
            current_ret = portfolio.get("total_return_pct", 0)
            # Add or update today's point
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

        return {
            "fund": fund_curve,
            "sp500": sp500_curve,
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
        from predictions.learner import analyze_factor_performance, analyze_sector_performance, analyze_mistake_patterns
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
            result["mistakes_learned"] = analyze_mistake_patterns()
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
    """Show what the AI is planning to trade next — the trades it's waiting to execute."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
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
