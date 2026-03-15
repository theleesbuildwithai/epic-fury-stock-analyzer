"""
Performance Tracker — tracks how accurate our predictions are
and compares us against the major stock indices.
"""

from predictions.models import (
    get_all_predictions,
    get_pending_predictions,
    resolve_prediction,
    save_benchmark_snapshot,
    get_benchmark_snapshots,
)
from analysis.market_data import get_stock_info, get_benchmark_data


def get_performance_stats() -> dict:
    """
    Calculate overall performance stats.
    Win rate, average return, comparison vs indices.
    """
    predictions = get_all_predictions()
    if not predictions:
        return {
            "total_predictions": 0,
            "message": "No predictions yet. Analyze a stock and save a prediction to start tracking!",
        }

    resolved = [p for p in predictions if p["actual_outcome"] != "pending"]
    pending = [p for p in predictions if p["actual_outcome"] == "pending"]
    wins = [p for p in resolved if p["actual_outcome"] == "hit"]
    losses = [p for p in resolved if p["actual_outcome"] == "miss"]

    win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else 0
    avg_return = round(
        sum(p["actual_return_pct"] for p in resolved if p["actual_return_pct"] is not None)
        / len(resolved), 2
    ) if resolved else 0

    # Best and worst predictions
    sorted_by_return = sorted(
        [p for p in resolved if p["actual_return_pct"] is not None],
        key=lambda x: x["actual_return_pct"],
        reverse=True,
    )
    best = sorted_by_return[0] if sorted_by_return else None
    worst = sorted_by_return[-1] if sorted_by_return else None

    # Calculate streak
    streak = 0
    streak_type = None
    for p in sorted(resolved, key=lambda x: x["predicted_at"], reverse=True):
        if streak_type is None:
            streak_type = p["actual_outcome"]
            streak = 1
        elif p["actual_outcome"] == streak_type:
            streak += 1
        else:
            break

    # Get benchmark comparison
    benchmarks = get_benchmark_data("1y")

    return {
        "total_predictions": len(predictions),
        "resolved": len(resolved),
        "pending": len(pending),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "best_prediction": {
            "ticker": best["ticker"],
            "return_pct": best["actual_return_pct"],
            "date": best["predicted_at"],
        } if best else None,
        "worst_prediction": {
            "ticker": worst["ticker"],
            "return_pct": worst["actual_return_pct"],
            "date": worst["predicted_at"],
        } if worst else None,
        "current_streak": {
            "count": streak,
            "type": streak_type,
        } if streak_type else None,
        "benchmarks": {
            name: {
                "total_return_pct": data["total_return_pct"],
            }
            for name, data in benchmarks.items()
        } if benchmarks else {},
        "predictions": predictions,
    }


def check_and_resolve_predictions():
    """
    Check pending predictions against current prices.
    Auto-resolves predictions that have passed their check_after_days.
    """
    from datetime import datetime, timedelta

    pending = get_pending_predictions()
    resolved_count = 0

    for pred in pending:
        predicted_at = datetime.fromisoformat(pred["predicted_at"])
        check_date = predicted_at + timedelta(days=pred["check_after_days"])

        if datetime.now() >= check_date:
            try:
                info = get_stock_info(pred["ticker"])
                current_price = info.get("current_price", 0)
                if current_price > 0:
                    entry = pred["entry_price"]
                    direction = pred["predicted_direction"]
                    return_pct = ((current_price - entry) / entry) * 100

                    if direction in ("Strong Buy", "Buy") and return_pct > 0:
                        outcome = "hit"
                    elif direction in ("Strong Sell", "Sell") and return_pct < 0:
                        outcome = "hit"
                    elif direction == "Hold" and abs(return_pct) < 5:
                        outcome = "hit"
                    else:
                        outcome = "miss"

                    resolve_prediction(pred["id"], current_price, outcome)
                    resolved_count += 1
            except Exception:
                pass

    return {"resolved": resolved_count, "remaining_pending": len(pending) - resolved_count}
