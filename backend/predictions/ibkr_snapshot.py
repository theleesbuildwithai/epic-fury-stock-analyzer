"""
IBKR Cross-Pollination via S3.

The EC2 backend (which has IB Gateway) periodically pushes account snapshots
to S3. The App Runner backend (public dashboard) reads from S3 to display
real IBKR data without needing direct Gateway access.

Architecture:
    [EC2 backend with Gateway]
            |
            | every 30s: push_ibkr_snapshot()
            v
    [S3: epic-fury-portfolio-db/ibkr_snapshot.json]
            ^
            | every 10s (cached): pull_ibkr_snapshot()
            |
    [App Runner backend / public dashboard]

Setup:
  - EC2 sets env var IBKR_PUSH_SNAPSHOT=true to enable the pusher thread.
  - App Runner needs IAM read access to s3://epic-fury-portfolio-db/.
  - EC2 needs IAM write access to s3://epic-fury-portfolio-db/.

Failure modes:
  - If S3 has no snapshot yet, pull returns {"available": False}.
  - If EC2 disconnects from IBKR, snapshot still uploads but with "connected": False.
  - If S3 itself is down, pull returns last cached snapshot (10s TTL).
"""
import os
import json
import time
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("DB_BACKUP_BUCKET", "epic-fury-portfolio-db")
S3_KEY = "ibkr_snapshot.json"

# Local cache to avoid hammering S3 on every dashboard hit
_snapshot_cache = {"data": None, "fetched_at": 0}
_SNAPSHOT_TTL = 10  # 10s cache so multiple dashboard hits don't all call S3
_snapshot_lock = threading.Lock()

# Track pusher state
_pusher_state = {
    "running": False,
    "last_push_at": None,
    "last_push_success": None,
    "push_count": 0,
    "error_count": 0,
    "last_error": None,
}


def _get_s3_client():
    """Lazy import + create boto3 client. Returns None if boto3 unavailable."""
    try:
        import boto3
        return boto3.client("s3", region_name="us-east-1")
    except Exception as e:
        logger.error(f"boto3 client init failed: {e}")
        return None


def push_ibkr_snapshot() -> dict:
    """Push current IBKR state to S3. Called from EC2 backend every 30s.

    Returns dict with status info. Safe to call when not connected (uploads
    a snapshot with "connected": False so the dashboard can show that state).
    """
    try:
        from predictions.ibkr_adapter import (
            get_ibkr_adapter, ibkr_get_account, ibkr_get_positions,
            ibkr_get_orders, get_order_log
        )

        adapter = get_ibkr_adapter()
        is_connected = False
        try:
            is_connected = adapter.is_connected()
        except Exception:
            pass

        snapshot = {
            "schema_version": 1,
            "pushed_at": datetime.now().isoformat(),
            "pushed_at_unix": time.time(),
            "source": "ec2_backend",
            "connected": is_connected,
        }

        # Always include account (returns {connected: False, ...} if disconnected)
        try:
            snapshot["account"] = ibkr_get_account(force_refresh=True)
        except Exception as e:
            snapshot["account"] = {"error": str(e), "connected": False}

        # Positions and orders only if connected
        if is_connected:
            try:
                snapshot["positions"] = ibkr_get_positions()
            except Exception as e:
                snapshot["positions"] = []
                snapshot["positions_error"] = str(e)

            try:
                snapshot["open_orders"] = ibkr_get_orders()
            except Exception as e:
                snapshot["open_orders"] = []
                snapshot["open_orders_error"] = str(e)
        else:
            snapshot["positions"] = []
            snapshot["open_orders"] = []

        # Order log (recent 20 — works even if disconnected)
        try:
            snapshot["recent_order_log"] = get_order_log(limit=20)
        except Exception:
            snapshot["recent_order_log"] = []

        # Upload to S3
        s3 = _get_s3_client()
        if s3 is None:
            _pusher_state["error_count"] += 1
            _pusher_state["last_error"] = "boto3 not available"
            return {"pushed": False, "error": "boto3 not available"}

        body_bytes = json.dumps(snapshot, default=str).encode("utf-8")
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=S3_KEY,
            Body=body_bytes,
            ContentType="application/json",
            CacheControl="max-age=10",
        )

        _pusher_state["last_push_at"] = datetime.now().isoformat()
        _pusher_state["last_push_success"] = True
        _pusher_state["push_count"] += 1

        logger.info(
            f"IBKR snapshot pushed | connected={is_connected} | "
            f"account={snapshot.get('account', {}).get('net_liquidation', 0)} | "
            f"positions={len(snapshot.get('positions', []))} | "
            f"size={len(body_bytes)}b"
        )
        return {
            "pushed": True,
            "size_bytes": len(body_bytes),
            "connected": is_connected,
        }

    except Exception as e:
        _pusher_state["error_count"] += 1
        _pusher_state["last_error"] = str(e)
        _pusher_state["last_push_success"] = False
        logger.error(f"push_ibkr_snapshot failed: {e}")
        return {"pushed": False, "error": str(e)}


def pull_ibkr_snapshot(force_refresh: bool = False) -> dict:
    """Pull latest IBKR snapshot from S3. Called from App Runner backend.

    Args:
        force_refresh: bypass 10s local cache and fetch fresh from S3.

    Returns dict with snapshot data or {"available": False} if no snapshot exists.
    """
    now = time.time()

    with _snapshot_lock:
        # Cache hit
        if (not force_refresh and _snapshot_cache["data"]
                and now - _snapshot_cache["fetched_at"] < _SNAPSHOT_TTL):
            cached = dict(_snapshot_cache["data"])
            cached["from_local_cache"] = True
            cached["local_cache_age_seconds"] = round(now - _snapshot_cache["fetched_at"], 1)
            return cached

        # Cache miss — fetch from S3
        s3 = _get_s3_client()
        if s3 is None:
            return {
                "available": False,
                "error": "boto3 not available on this backend",
            }

        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
            raw = obj["Body"].read().decode("utf-8")
            snapshot = json.loads(raw)

            # Calculate snapshot age (how stale is the data from EC2)
            pushed_at_unix = snapshot.get("pushed_at_unix", 0)
            if pushed_at_unix:
                snapshot["snapshot_age_seconds"] = round(now - pushed_at_unix, 1)
                # Flag if snapshot is too stale (EC2 might be down)
                if now - pushed_at_unix > 120:  # 2 min stale = problem
                    snapshot["snapshot_stale"] = True
                    snapshot["snapshot_stale_reason"] = (
                        f"Snapshot is {round(now - pushed_at_unix, 0)}s old. "
                        f"EC2 pusher may be down or disconnected."
                    )
                else:
                    snapshot["snapshot_stale"] = False
            else:
                snapshot["snapshot_age_seconds"] = None

            snapshot["from_local_cache"] = False
            snapshot["available"] = True

            # Update cache
            _snapshot_cache["data"] = snapshot
            _snapshot_cache["fetched_at"] = now

            return snapshot

        except s3.exceptions.NoSuchKey:
            return {
                "available": False,
                "reason": "no_snapshot",
                "message": ("IBKR snapshot does not exist yet in S3. "
                            "EC2 backend hasn't pushed one. Either EC2 isn't running "
                            "or IBKR_PUSH_SNAPSHOT env var isn't set."),
                "expected_path": f"s3://{S3_BUCKET}/{S3_KEY}",
            }
        except Exception as e:
            error_str = str(e)
            # Common case: 404 NoSuchKey
            if "NoSuchKey" in error_str or "Not Found" in error_str:
                return {
                    "available": False,
                    "reason": "no_snapshot",
                    "message": "IBKR snapshot not yet available in S3.",
                }
            return {
                "available": False,
                "reason": "fetch_error",
                "error": error_str,
            }


def start_snapshot_pusher_thread(interval_seconds: int = 30):
    """Start a background thread that pushes snapshots to S3 every N seconds.

    Call this from EC2 backend startup (gated by IBKR_PUSH_SNAPSHOT env var).
    Safe to call once — uses a daemon thread that dies with the process.
    """
    if _pusher_state["running"]:
        logger.warning("Snapshot pusher already running — skipping duplicate start")
        return None

    def _pusher_loop():
        logger.warning(f"IBKR snapshot pusher STARTED (interval: {interval_seconds}s)")
        _pusher_state["running"] = True
        # Initial sleep so the IBKR adapter has time to connect on boot
        time.sleep(20)
        while True:
            try:
                push_ibkr_snapshot()
            except Exception as e:
                logger.error(f"Snapshot pusher loop error: {e}")
                _pusher_state["error_count"] += 1
                _pusher_state["last_error"] = str(e)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_pusher_loop, daemon=True, name="ibkr-snapshot-pusher")
    thread.start()
    return thread


def get_pusher_state() -> dict:
    """Return current state of the snapshot pusher (for debugging)."""
    return dict(_pusher_state)
