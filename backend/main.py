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
from analysis.extras import get_banner_data, get_daily_picks, get_earnings_calendar, get_daily_summary
from analysis.news_sentiment import get_market_news
from analysis.ai_analyst import answer_question
from predictions.models import init_db, save_prediction, get_all_predictions
from predictions.tracker import get_performance_stats, check_and_resolve_predictions

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

# --- Rate Limiting with Auto-Ban ---
rate_limit_store = defaultdict(list)   # IP -> [timestamps]
banned_ips = {}                         # IP -> ban_expire_time
strike_counter = defaultdict(int)       # IP -> number of violations
RATE_LIMIT = 60          # max requests per window
RATE_WINDOW = 60         # 60 second window
BAN_DURATION = 300       # 5 minute ban after repeated violations
MAX_STRIKES = 3          # strikes before auto-ban

def check_rate_limit(client_ip: str):
    """Rate limiter with auto-ban for repeat offenders."""
    now = time.time()

    # Check if IP is banned
    if client_ip in banned_ips:
        if now < banned_ips[client_ip]:
            raise HTTPException(status_code=403, detail="Access denied")
        else:
            del banned_ips[client_ip]
            strike_counter[client_ip] = 0

    # Clean old entries
    rate_limit_store[client_ip] = [t for t in rate_limit_store[client_ip] if now - t < RATE_WINDOW]
    if len(rate_limit_store[client_ip]) >= RATE_LIMIT:
        strike_counter[client_ip] += 1
        if strike_counter[client_ip] >= MAX_STRIKES:
            banned_ips[client_ip] = now + BAN_DURATION
            logger.warning(f"FIREWALL: Auto-banned IP {client_ip} for {BAN_DURATION}s")
            raise HTTPException(status_code=403, detail="Access denied")
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    rate_limit_store[client_ip].append(now)

# --- Malicious Pattern Detection ---
ATTACK_PATTERNS = [
    re.compile(r"\.\./"),                           # Path traversal
    re.compile(r"\.\.\%2[fF]"),                     # Encoded path traversal
    re.compile(r"<script", re.IGNORECASE),          # XSS attempt
    re.compile(r"javascript:", re.IGNORECASE),      # XSS via JS protocol
    re.compile(r"(union|select|insert|drop|delete|update)\s", re.IGNORECASE),  # SQL injection
    re.compile(r"(\%27|\'|\-\-)", re.IGNORECASE),   # SQL injection chars
    re.compile(r"\{[\{%]"),                          # Template injection
    re.compile(r"(etc/passwd|etc/shadow|proc/self)", re.IGNORECASE),  # Linux file access
    re.compile(r"(\.env|\.git|\.aws|wp-admin|wp-login|phpmyadmin)", re.IGNORECASE),  # Scanner probes
    re.compile(r"(eval|exec|import|__import__|os\.)", re.IGNORECASE),  # Python injection
    re.compile(r"\x00"),                             # Null byte injection
]

# Known malicious bot user agents
BOT_PATTERNS = [
    re.compile(r"(sqlmap|nikto|nmap|masscan|dirbuster|gobuster|wfuzz|hydra|metasploit)", re.IGNORECASE),
    re.compile(r"(scrapy|python-requests/2\.\d+\.\d+$)", re.IGNORECASE),
    re.compile(r"(curl/\d|wget/\d)", re.IGNORECASE),
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

# --- Firewall Middleware (processes EVERY request) ---
class FirewallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path
        query = str(request.url.query)
        user_agent = request.headers.get("user-agent", "")
        now = time.time()

        # 1. Check IP ban list
        if client_ip in banned_ips:
            if now < banned_ips[client_ip]:
                return JSONResponse(status_code=403, content={"detail": "Access denied"})
            else:
                del banned_ips[client_ip]
                strike_counter[client_ip] = 0

        # 2. Check for malicious patterns
        attack = is_malicious_request(path, query, user_agent)
        if attack:
            logger.warning(f"FIREWALL BLOCKED: {client_ip} | {attack} | {path}")
            # Auto-ban on honeypot hits or repeated attacks
            strike_counter[client_ip] += 2
            if strike_counter[client_ip] >= MAX_STRIKES:
                banned_ips[client_ip] = now + BAN_DURATION * 2  # Double ban for attacks
                logger.warning(f"FIREWALL: Auto-banned attacker {client_ip} for {BAN_DURATION * 2}s")
            return JSONResponse(status_code=403, content={"detail": "Access denied"})

        # 3. Block requests with no user agent (bots/scanners)
        if not user_agent and not path.startswith("/health"):
            return JSONResponse(status_code=403, content={"detail": "Access denied"})

        # 4. Method restriction — only GET and POST allowed
        if request.method not in ("GET", "POST", "OPTIONS", "HEAD"):
            return JSONResponse(status_code=405, content={"detail": "Method not allowed"})

        # 5. Request size limit (1MB max body)
        content_length = request.headers.get("content-length", "0")
        try:
            if int(content_length) > 1_048_576:
                return JSONResponse(status_code=413, content={"detail": "Request too large"})
        except ValueError:
            pass

        # Process request and add security headers
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'"
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
    """Get live prices for the scrolling ticker banner."""
    check_rate_limit(request.client.host)
    try:
        return {"tickers": get_banner_data()}
    except Exception as e:
        logger.error(f"Banner error: {e}")
        return {"tickers": []}


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
