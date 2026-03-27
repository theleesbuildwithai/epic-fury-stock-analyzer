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
import os, re, logging, time
from collections import defaultdict

from analysis.report import generate_full_report
from analysis.market_data import get_stock_info, get_historical_data, get_benchmark_data
from analysis.ticker_search import search_tickers
from analysis.extras import get_banner_data, get_daily_picks, get_earnings_calendar, get_daily_summary, get_sector_heatmap
from analysis.news_sentiment import get_market_news
from analysis.ai_analyst import answer_question
from analysis.quant_engine import generate_quant_picks, detect_market_regime
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

# Initialize the database when the app starts
init_db()


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


@app.post("/api/paper-trade/rebalance")
def paper_rebalance(request: Request):
    """Trigger a trade cycle: close expired positions, open new ones from quant signals."""
    check_rate_limit(request.client.host)
    try:
        picks = generate_quant_picks()
        result = execute_trades_from_signals(picks)
        # Auto-adjust weights if enough trades
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
