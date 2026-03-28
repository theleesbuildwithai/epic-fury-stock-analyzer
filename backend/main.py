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
from analysis.news_sentiment import get_market_news
from analysis.ai_analyst import answer_question
from analysis.quant_engine import generate_quant_picks, detect_market_regime, scan_overnight_intelligence, analyze_watchlist_stock, _throttle
from predictions.models import init_db, save_prediction, get_all_predictions
from predictions.tracker import get_performance_stats, check_and_resolve_predictions
from predictions.paper_trader import get_portfolio_state, execute_trades_from_signals, run_backtest, get_performance_analytics
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

def _run_auto_trade_cycle():
    """
    Autonomous trade cycle — the computer decides what to buy/sell.
    Runs every 2 hours during market hours, once at night for after-hours analysis.
    No human intervention needed.
    """
    global auto_trade_stats
    cycle_start = dt.now()
    auto_trade_stats["total_cycles"] += 1
    auto_trade_stats["last_run"] = cycle_start.isoformat()
    auto_trade_stats["status"] = "trading"

    try:
        logger.warning(f"AUTO-TRADE CYCLE #{auto_trade_stats['total_cycles']} starting at {cycle_start}")

        # 1. Generate fresh quant picks (analyzes 100+ stocks)
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
        }
        auto_trade_stats["status"] = "idle"

        # Log the cycle
        log_entry = {
            "time": cycle_start.isoformat(),
            "cycle": auto_trade_stats["total_cycles"],
            "opened": opened,
            "closed": closed,
            "regime": result.get("portfolio_after", {}).get("regime"),
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
            f"AUTO-TRADE CYCLE #{auto_trade_stats['total_cycles']} complete: "
            f"{opened} opened, {closed} closed"
        )

    except Exception as e:
        auto_trade_stats["errors"] += 1
        auto_trade_stats["status"] = "error"
        auto_trade_stats["last_error"] = str(e)
        logger.error(f"AUTO-TRADE ERROR: {e}")

# Start the scheduler
scheduler = BackgroundScheduler(timezone="US/Eastern")

# Trade every hour during extended market hours (7am-7pm ET)
# More frequent = more responsive to market moves = better returns
scheduler.add_job(
    _run_auto_trade_cycle,
    "cron",
    hour="7,8,9,10,11,12,13,14,15,16,17,18,19",
    minute=30,
    id="auto_trade_cycle",
    name="Autonomous Trading Cycle",
    max_instances=1,
    misfire_grace_time=3600,
)

# Also run once at startup (after 60s delay to let app warm up)
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


# --- Request/Response Models ---

class PredictionRequest(BaseModel):
    ticker: str
    predicted_direction: str  # "Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"
    confidence_score: float
    entry_price: float
    target_price: Optional[float] = None
    check_after_days: int = 30
    notes: Optional[str] = None


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
        job = scheduler.get_job("auto_trade_cycle")
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

    return {
        **auto_trade_stats,
        "next_scheduled_run": next_run,
        "schedule": "Every hour (7:30am-7:30pm ET) + 6:30am pre-market scan + Sunday 8pm scan",
        "overnight_intel": overnight_summary,
        "recent_activity": auto_trade_log[-20:],  # Last 20 cycles
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

        _throttle()
        # Download enough history to cover the earliest add date
        df = yf.download(ticker_list, period="2y", progress=False, group_by="ticker")
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="No data")

        # Build returns matrix — trim each stock to its add date
        returns_data = {}
        price_series = {}
        for sym in ticker_list:
            try:
                if isinstance(df.columns, pd.MultiIndex) and len(ticker_list) > 1:
                    if sym in df.columns.get_level_values(0):
                        sym_df = df[sym]["Close"].dropna()
                    else:
                        continue
                else:
                    sym_df = df["Close"].dropna()

                # Trim to add date if available
                if sym in stock_add_dates and stock_add_dates[sym]:
                    try:
                        add_date = parse_dt.fromisoformat(stock_add_dates[sym].replace('Z', '+00:00'))
                        add_date_naive = add_date.replace(tzinfo=None)
                        # Filter to only data from add date onward
                        sym_df = sym_df[sym_df.index >= pd.Timestamp(add_date_naive)]
                    except Exception:
                        pass

                closes = sym_df.values.astype(float).flatten()

                if len(closes) < 2:
                    continue

                daily_rets = np.diff(closes) / closes[:-1]
                returns_data[sym] = daily_rets
                # Normalize to 100 for chart
                price_series[sym] = (closes / closes[0] * 100).tolist()
            except Exception:
                continue

        if not returns_data:
            raise HTTPException(status_code=404, detail="No valid data for tickers")

        # Calculate stats per stock
        stock_stats = {}
        for sym, rets in returns_data.items():
            total_ret = float((np.prod(1 + rets) - 1) * 100)
            ann_ret = float(total_ret * (252 / len(rets))) if len(rets) > 0 else 0
            ann_vol = float(np.std(rets) * np.sqrt(252) * 100)
            sharpe = round(ann_ret / ann_vol, 2) if ann_vol > 0 else 0
            max_dd = 0
            peak = 1.0
            for r in rets:
                peak = max(peak, peak * (1 + r))
                dd = (peak * (1 + r) - peak) / peak * 100
                max_dd = min(max_dd, dd)

            stock_stats[sym] = {
                "total_return": round(total_ret, 2),
                "annualized_return": round(ann_ret, 1),
                "annualized_vol": round(ann_vol, 1),
                "sharpe_ratio": sharpe,
                "max_drawdown": round(max_dd, 1),
                "trading_days": len(rets),
                "days_held": len(rets),
            }

        # Correlation matrix
        symbols = list(returns_data.keys())
        min_len = min(len(returns_data[s]) for s in symbols)
        corr_matrix = {}
        for i, s1 in enumerate(symbols):
            corr_matrix[s1] = {}
            for j, s2 in enumerate(symbols):
                r1 = returns_data[s1][-min_len:]
                r2 = returns_data[s2][-min_len:]
                corr = float(np.corrcoef(r1, r2)[0, 1])
                corr_matrix[s1][s2] = round(corr, 3)

        # Equal-weight portfolio performance
        if len(symbols) >= 2:
            port_rets = np.zeros(min_len)
            for sym in symbols:
                port_rets += returns_data[sym][-min_len:] / len(symbols)
            port_total = float((np.prod(1 + port_rets) - 1) * 100)
            port_vol = float(np.std(port_rets) * np.sqrt(252) * 100)
            port_sharpe = round((port_total * 252 / min_len) / port_vol, 2) if port_vol > 0 else 0
            portfolio_stats = {
                "total_return": round(port_total, 2),
                "annualized_vol": round(port_vol, 1),
                "sharpe_ratio": port_sharpe,
                "diversification_benefit": round(
                    np.mean([stock_stats[s]["annualized_vol"] for s in symbols]) - port_vol, 1
                ),
            }
        else:
            portfolio_stats = stock_stats.get(symbols[0], {})

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
