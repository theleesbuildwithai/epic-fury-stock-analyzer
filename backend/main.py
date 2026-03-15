"""
Epic Fury Stock Analyzer — Backend API
Built with FastAPI (Python)

This is the "engine" of our app. It receives requests from the website,
fetches real stock data, runs the analysis, and sends back results.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os

from analysis.report import generate_full_report
from analysis.market_data import get_stock_info, get_historical_data, get_benchmark_data
from predictions.models import init_db, save_prediction, get_all_predictions
from predictions.tracker import get_performance_stats, check_and_resolve_predictions

# Create the app
app = FastAPI(
    title="Epic Fury Stock Analyzer",
    description="Real-time stock analysis with technical indicators and performance tracking",
    version="1.0.0",
)

# Allow the frontend to talk to the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.get("/api/analyze/{ticker}")
def analyze_stock(ticker: str, period: str = "1y"):
    """
    Full stock analysis — the main endpoint.
    Takes a stock ticker (like AAPL) and returns a complete report
    with technical indicators, signals, and risk score.
    """
    try:
        report = generate_full_report(ticker.upper(), period)
        if "error" in report:
            raise HTTPException(status_code=404, detail=report["error"])
        return report
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error analyzing {ticker}: {str(e)}")


@app.get("/api/quote/{ticker}")
def get_quote(ticker: str):
    """Get current quote and basic info for a stock."""
    try:
        info = get_stock_info(ticker.upper())
        if not info.get("current_price"):
            raise HTTPException(status_code=404, detail=f"No data found for {ticker}")
        return info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching {ticker}: {str(e)}")


@app.get("/api/history/{ticker}")
def get_history(ticker: str, period: str = "6mo"):
    """Get historical price data for charting."""
    try:
        data = get_historical_data(ticker.upper(), period)
        if not data:
            raise HTTPException(status_code=404, detail=f"No history found for {ticker}")
        return {"ticker": ticker.upper(), "period": period, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching history: {str(e)}")


@app.get("/api/benchmarks")
def get_benchmarks(period: str = "1y"):
    """Get performance data for S&P 500, Nasdaq, and Dow Jones."""
    try:
        return get_benchmark_data(period)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching benchmarks: {str(e)}")


@app.post("/api/predictions")
def create_prediction(pred: PredictionRequest):
    """Save a new prediction to track."""
    try:
        prediction_id = save_prediction(
            ticker=pred.ticker,
            direction=pred.predicted_direction,
            confidence=pred.confidence_score,
            entry_price=pred.entry_price,
            target_price=pred.target_price,
            check_after_days=pred.check_after_days,
            notes=pred.notes,
        )
        return {"id": prediction_id, "message": "Prediction saved!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving prediction: {str(e)}")


@app.get("/api/predictions")
def list_predictions():
    """Get all saved predictions."""
    return {"predictions": get_all_predictions()}


@app.get("/api/performance")
def get_performance():
    """Get overall performance stats and comparison vs market indices."""
    try:
        # Auto-check pending predictions
        check_and_resolve_predictions()
        return get_performance_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error calculating performance: {str(e)}")


# --- Serve Frontend (in production, the built React app is here) ---

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(frontend_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        """Serve the React frontend for any non-API route."""
        file_path = os.path.join(frontend_dir, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_dir, "index.html"))
