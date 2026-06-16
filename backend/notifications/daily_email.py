"""
Daily Performance Email — Epic Fury / Sentinel Quant
Fires every weekday at 3:00 PM ET.

From: jacksonwhanglee@gmail.com
To:   jacksonwhanglee@gmail.com

Requires env var: GMAIL_APP_PASSWORD
"""

import os
import smtplib
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger(__name__)

FROM_EMAIL = "jacksonwhanglee@gmail.com"
TO_EMAIL   = "jacksonwhanglee@gmail.com"
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587


# ============================================================
#  System health diagnostics
# ============================================================

def _run_health_checks() -> list:
    """
    Auto-detect system problems. Returns a list of dicts:
      {"label": str, "status": "ok"|"warn"|"error", "detail": str}
    """
    checks = []

    # 1. VIX reading
    try:
        from analytics.vix_guard import get_vix_safe
        vr = get_vix_safe()
        vix_val  = vr.get("value")
        vix_conf = vr.get("confidence", "")
        vix_src  = vr.get("source", "")
        rejected = vr.get("rejected_reason")
        if vix_val is None:
            checks.append({"label": "VIX Feed", "status": "error",
                           "detail": f"No VIX value returned. Reason: {rejected or 'unknown'}"})
        elif vix_val > 35:
            checks.append({"label": "VIX Feed", "status": "warn",
                           "detail": f"VIX={vix_val:.1f} (CRISIS zone) · source={vix_src} · conf={vix_conf}"})
        elif vix_conf == "LOW":
            checks.append({"label": "VIX Feed", "status": "warn",
                           "detail": f"VIX={vix_val:.1f} using fallback ({vix_src}) · live feeds failed"})
        else:
            checks.append({"label": "VIX Feed", "status": "ok",
                           "detail": f"VIX={vix_val:.1f} · source={vix_src} · conf={vix_conf}"})
    except Exception as e:
        checks.append({"label": "VIX Feed", "status": "error", "detail": str(e)[:80]})

    # 2. Quant picks / STB cache
    try:
        from analysis.quant_engine import _quant_cache, _SCAN_RUNNING
        entry = _quant_cache.get("quant_picks") or {}
        picks = (entry.get("data") or {})
        n_longs  = len(picks.get("long_picks") or [])
        n_shorts = len(picks.get("short_picks") or [])
        cache_age_min = (time.time() - entry.get("time", 0)) / 60 if entry.get("time") else None

        if _SCAN_RUNNING:
            checks.append({"label": "Picks Engine", "status": "warn",
                           "detail": "Scan currently running — picks may be stale"})
        elif n_longs == 0:
            checks.append({"label": "Picks Engine", "status": "error",
                           "detail": "0 long picks in cache — STB is empty"})
        elif cache_age_min and cache_age_min > 60:
            checks.append({"label": "Picks Engine", "status": "warn",
                           "detail": f"{n_longs}L {n_shorts}S picks · cache is {cache_age_min:.0f} min old"})
        else:
            age_str = f"{cache_age_min:.0f} min old" if cache_age_min else "age unknown"
            checks.append({"label": "Picks Engine", "status": "ok",
                           "detail": f"{n_longs} longs · {n_shorts} shorts · {age_str}"})
    except Exception as e:
        checks.append({"label": "Picks Engine", "status": "error", "detail": str(e)[:80]})

    # 3. Market regime
    try:
        from analysis.quant_engine import detect_market_regime
        regime = detect_market_regime() or {}
        reg_name = regime.get("regime", "UNKNOWN")
        reg_conf = regime.get("confidence", 0)
        if reg_name in ("UNKNOWN", "ERROR", ""):
            checks.append({"label": "Market Regime", "status": "error",
                           "detail": f"Regime detection failed: '{reg_name}'"})
        elif reg_name == "CRISIS":
            checks.append({"label": "Market Regime", "status": "warn",
                           "detail": f"CRISIS regime detected · conf={reg_conf}% · position sizing reduced"})
        else:
            checks.append({"label": "Market Regime", "status": "ok",
                           "detail": f"{reg_name} · confidence={reg_conf}%"})
    except Exception as e:
        checks.append({"label": "Market Regime", "status": "error", "detail": str(e)[:80]})

    # 4. Portfolio NAV sanity
    try:
        from predictions.paper_trader import get_portfolio_stats
        stats = get_portfolio_stats()
        nav = float(stats.get("total_value") or 0)
        ret = float(stats.get("total_return_pct") or 0)
        if nav <= 0:
            checks.append({"label": "Portfolio NAV", "status": "error",
                           "detail": "NAV = $0 — portfolio data missing or reset needed"})
        elif nav < 50_000:
            checks.append({"label": "Portfolio NAV", "status": "warn",
                           "detail": f"NAV = ${nav:,.0f} — significant drawdown from $100k baseline"})
        elif ret > 200:
            checks.append({"label": "Portfolio NAV", "status": "warn",
                           "detail": f"NAV = ${nav:,.0f} · return={ret:.1f}% — suspiciously high, check for phantom P&L"})
        else:
            checks.append({"label": "Portfolio NAV", "status": "ok",
                           "detail": f"NAV = ${nav:,.0f} · return = {ret:+.1f}%"})
    except Exception as e:
        checks.append({"label": "Portfolio NAV", "status": "error", "detail": str(e)[:80]})

    # 5. Auto-trading status
    try:
        from predictions.models import get_trading_state
        paused   = get_trading_state("trading_paused", "0")
        disabled = get_trading_state("auto_trading_disabled", "0")
        if disabled == "1":
            checks.append({"label": "Auto-Trading", "status": "warn",
                           "detail": "Auto-trading is DISABLED — no new trades will open"})
        elif paused == "1":
            checks.append({"label": "Auto-Trading", "status": "warn",
                           "detail": "Auto-trading is PAUSED (daily limit hit or manual pause)"})
        else:
            checks.append({"label": "Auto-Trading", "status": "ok",
                           "detail": "Active — trading normally"})
    except Exception as e:
        checks.append({"label": "Auto-Trading", "status": "error", "detail": str(e)[:80]})

    # 6. Short options check (should always be 0)
    try:
        from predictions.models import get_open_trades
        open_trades = get_open_trades() or []
        short_opts  = [t for t in open_trades
                       if t.get("direction", "").lower() == "short"
                       and t.get("asset_type", "") in ("option", "put", "call")]
        if short_opts:
            checks.append({"label": "Short Options", "status": "error",
                           "detail": f"{len(short_opts)} short option(s) open — should be 0 (NAV inflation risk)"})
        else:
            checks.append({"label": "Short Options", "status": "ok",
                           "detail": "None open — all options are long only"})
    except Exception as e:
        checks.append({"label": "Short Options", "status": "warn", "detail": str(e)[:80]})

    # 7. yfinance connectivity
    try:
        import threading as _thr
        _result = [None]
        def _ping():
            import yfinance as yf
            df = yf.download("SPY", period="1d", progress=False)
            _result[0] = df
        t = _thr.Thread(target=_ping, daemon=True)
        t.start(); t.join(timeout=10)
        if _result[0] is not None and not _result[0].empty:
            checks.append({"label": "yfinance Feed", "status": "ok",
                           "detail": "Live data reachable (SPY ping OK)"})
        else:
            checks.append({"label": "yfinance Feed", "status": "warn",
                           "detail": "SPY ping returned empty — Yahoo Finance may be rate-limiting"})
    except Exception as e:
        checks.append({"label": "yfinance Feed", "status": "error", "detail": str(e)[:80]})

    return checks


# ============================================================
#  Data collection
# ============================================================

def _collect_report_data() -> dict:
    """Pull live data from all relevant endpoints/modules."""
    data = {}

    # Portfolio snapshot
    try:
        from predictions.paper_trader import get_portfolio_stats
        stats = get_portfolio_stats()
        data["nav"]           = stats.get("total_value", 0)
        data["cash"]          = stats.get("cash", 0)
        data["positions_val"] = stats.get("positions_value", 0)
        data["return_pct"]    = stats.get("total_return_pct", 0)
        data["n_positions"]   = len(stats.get("positions", []))
        data["positions"]     = stats.get("positions", [])
    except Exception as e:
        logger.warning(f"Email: portfolio stats error — {e}")
        data.update({"nav": 0, "cash": 0, "positions_val": 0,
                     "return_pct": 0, "n_positions": 0, "positions": []})

    # Closed trades today
    try:
        from predictions.models import get_closed_trades
        from datetime import date
        today_str = date.today().isoformat()
        all_closed = get_closed_trades(limit=200) or []
        data["closed_today"] = [
            t for t in all_closed
            if (t.get("closed_at") or "").startswith(today_str)
        ]
    except Exception as e:
        logger.warning(f"Email: closed trades error — {e}")
        data["closed_today"] = []

    # VIX + regime
    try:
        from analytics.vix_guard import get_vix_safe
        vix_result = get_vix_safe()
        data["vix"]            = vix_result.get("value")
        data["vix_source"]     = vix_result.get("source")
        data["vix_confidence"] = vix_result.get("confidence")
        data["vix_rejected"]   = vix_result.get("rejected_reason")
    except Exception as e:
        logger.warning(f"Email: VIX error — {e}")
        data["vix"] = None

    try:
        from analysis.quant_engine import detect_market_regime
        regime = detect_market_regime() or {}
        data["regime"]      = regime.get("regime", "UNKNOWN")
        data["reg_conf"]    = regime.get("confidence", 0)
        data["sp500_price"] = regime.get("sp500_price")
        data["breadth"]     = regime.get("breadth_pct")
        data["reg_details"] = regime.get("details", [])
    except Exception as e:
        logger.warning(f"Email: regime error — {e}")
        data["regime"] = "UNKNOWN"

    # Top picks for tomorrow
    try:
        from analysis.quant_engine import _quant_cache
        import time as _t
        entry = _quant_cache.get("quant_picks") or {}
        picks_data = entry.get("data") or {}
        data["top_longs"]       = (picks_data.get("long_picks") or [])[:8]
        data["cache_age_min"]   = round((_t.time() - entry.get("time", 0)) / 60, 0) if entry.get("time") else None
    except Exception as e:
        logger.warning(f"Email: picks error — {e}")
        data["top_longs"] = []

    # Performance stats
    try:
        from predictions.paper_trader import get_performance_metrics
        perf = get_performance_metrics() or {}
        data["win_rate"]     = perf.get("win_rate", 0)
        data["total_trades"] = perf.get("total_trades", 0)
        data["avg_win_pct"]  = perf.get("avg_win_pct", 0)
        data["avg_loss_pct"] = perf.get("avg_loss_pct", 0)
    except Exception as e:
        logger.warning(f"Email: performance metrics error — {e}")
        data.update({"win_rate": 0, "total_trades": 0,
                     "avg_win_pct": 0, "avg_loss_pct": 0})

    # System health checks
    data["health_checks"] = _run_health_checks()

    return data


# ============================================================
#  HTML template
# ============================================================

def _build_html(d: dict, date_str: str) -> str:

    def pct_color(v):
        try:
            return "#2ecc71" if float(v) >= 0 else "#e74c3c"
        except Exception:
            return "#aaa"

    def fmt_pct(v, decimals=2):
        try:
            return f"{float(v):+.{decimals}f}%"
        except Exception:
            return "—"

    def fmt_usd(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "—"

    # Regime badge
    regime = (d.get("regime") or "UNKNOWN").upper()
    regime_colors = {
        "BULL":    ("#1a472a", "#2ecc71"),
        "BEAR":    ("#4a0000", "#e74c3c"),
        "SIDEWAYS":("#2c3e50", "#f39c12"),
        "CRISIS":  ("#4a0000", "#c0392b"),
    }
    reg_bg, reg_fg = regime_colors.get(regime, ("#2c3e50", "#ecf0f1"))

    vix_val = d.get("vix")
    vix_str = f"{vix_val:.1f}" if vix_val else "N/A"

    # ── System Health section ──
    checks = d.get("health_checks") or []
    n_errors  = sum(1 for c in checks if c["status"] == "error")
    n_warns   = sum(1 for c in checks if c["status"] == "warn")

    if n_errors > 0:
        health_summary_color = "#e74c3c"
        health_summary_label = f"{n_errors} Problem{'s' if n_errors>1 else ''} Detected"
        if n_warns:
            health_summary_label += f" · {n_warns} Warning{'s' if n_warns>1 else ''}"
    elif n_warns > 0:
        health_summary_color = "#f39c12"
        health_summary_label = f"All Clear · {n_warns} Warning{'s' if n_warns>1 else ''}"
    else:
        health_summary_color = "#2ecc71"
        health_summary_label = "All Systems Nominal"

    status_icons = {"ok": "✅", "warn": "⚠️", "error": "❌"}
    status_bg    = {"ok": "#0d2b1a", "warn": "#2b2200", "error": "#2b0000"}
    status_fg    = {"ok": "#2ecc71", "warn": "#f39c12", "error": "#e74c3c"}

    health_rows = ""
    for c in checks:
        icon  = status_icons.get(c["status"], "•")
        bg    = status_bg.get(c["status"], "#161b22")
        fg    = status_fg.get(c["status"], "#ecf0f1")
        health_rows += f"""
        <tr>
          <td style="padding:10px 14px;background:{bg};border-radius:6px;margin-bottom:4px">
            <span style="font-size:15px">{icon}</span>
            <span style="color:{fg};font-weight:700;margin-left:8px">{c['label']}</span>
            <span style="color:#7f8c8d;font-size:12px;margin-left:10px">{c['detail']}</span>
          </td>
        </tr>
        <tr><td style="height:4px"></td></tr>"""

    # ── Open positions ──
    pos_rows = ""
    for p in (d.get("positions") or []):
        sym     = p.get("ticker") or p.get("symbol", "?")
        side    = (p.get("direction") or "LONG").upper()
        entry   = p.get("entry_price", 0)
        cur     = p.get("current_price") or entry
        pnl_pct = ((cur - entry) / entry * 100) if entry else 0
        pnl_val = p.get("unrealized_pnl") or ((cur - entry) * p.get("shares", 0))
        side_badge = (
            '<span style="background:#2ecc71;color:#000;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700">LONG</span>'
            if side == "LONG" else
            '<span style="background:#e74c3c;color:#fff;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700">SHORT</span>'
        )
        pos_rows += f"""
        <tr style="border-bottom:1px solid #21262d">
          <td style="padding:9px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:9px 12px">{side_badge}</td>
          <td style="padding:9px 12px;color:#bdc3c7">{fmt_usd(entry)}</td>
          <td style="padding:9px 12px;color:#bdc3c7">{fmt_usd(cur)}</td>
          <td style="padding:9px 12px;color:{pct_color(pnl_pct)};font-weight:700">{fmt_pct(pnl_pct)}</td>
          <td style="padding:9px 12px;color:{pct_color(pnl_val)};font-weight:700">{fmt_usd(pnl_val)}</td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="6" style="padding:14px;color:#7f8c8d;text-align:center">No open positions</td></tr>'

    # ── Closed today ──
    closed_rows = ""
    today_pnl = 0.0
    for t in (d.get("closed_today") or []):
        sym     = t.get("ticker") or t.get("symbol", "?")
        entry   = t.get("entry_price", 0)
        ex      = t.get("exit_price") or t.get("close_price") or entry
        pnl_pct = ((ex - entry) / entry * 100) if entry else 0
        pnl_val = float(t.get("pnl") or t.get("realized_pnl") or 0)
        today_pnl += pnl_val
        reason  = t.get("close_reason") or t.get("exit_reason") or "—"
        closed_rows += f"""
        <tr style="border-bottom:1px solid #21262d">
          <td style="padding:9px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:9px 12px;color:#bdc3c7">{fmt_usd(entry)}</td>
          <td style="padding:9px 12px;color:#bdc3c7">{fmt_usd(ex)}</td>
          <td style="padding:9px 12px;color:{pct_color(pnl_pct)};font-weight:700">{fmt_pct(pnl_pct)}</td>
          <td style="padding:9px 12px;color:{pct_color(pnl_val)};font-weight:700">{fmt_usd(pnl_val)}</td>
          <td style="padding:9px 12px;color:#7f8c8d;font-size:12px">{reason}</td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="6" style="padding:14px;color:#7f8c8d;text-align:center">No trades closed today</td></tr>'

    # ── Picks for tomorrow ──
    picks_rows = ""
    for i, p in enumerate(d.get("top_longs") or [], 1):
        sym  = p.get("symbol") or p.get("ticker", "?")
        conf = p.get("confidence") or p.get("confidence_score") or 0
        scr  = p.get("composite_score") or p.get("score") or 0
        sec  = p.get("sector") or "—"
        picks_rows += f"""
        <tr style="border-bottom:1px solid #21262d">
          <td style="padding:9px 12px;color:#7f8c8d">{i}</td>
          <td style="padding:9px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:9px 12px;color:#bdc3c7">{sec}</td>
          <td style="padding:9px 12px;color:#2ecc71;font-weight:700">{conf:.0f}%</td>
          <td style="padding:9px 12px;color:#3498db">{scr:.2f}</td>
        </tr>"""

    if not picks_rows:
        picks_rows = '<tr><td colspan="5" style="padding:14px;color:#7f8c8d;text-align:center">Picks updating — check dashboard</td></tr>'

    # Regime details
    reg_details_html = ""
    for det in (d.get("reg_details") or [])[:4]:
        reg_details_html += f'<div style="color:#7f8c8d;font-size:12px;margin-top:3px">· {det}</div>'

    today_pnl_color = pct_color(today_pnl)
    return_color    = pct_color(d.get("return_pct", 0))
    cache_age_str   = f"{int(d.get('cache_age_min', 0))} min ago" if d.get("cache_age_min") else "unknown"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:'Helvetica Neue',Arial,sans-serif;color:#ecf0f1">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;padding:24px 16px">

  <!-- HEADER -->
  <tr><td style="padding-bottom:20px">
    <table width="100%" style="background:linear-gradient(135deg,#1a1a2e,#16213e);border-radius:12px;padding:28px">
      <tr>
        <td>
          <div style="font-size:11px;color:#7f8c8d;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px">Sentinel Quant · Epic Fury Capital</div>
          <div style="font-size:26px;font-weight:800;color:#ecf0f1">Daily Report</div>
          <div style="font-size:13px;color:#7f8c8d;margin-top:4px">{date_str} · 3:00 PM ET</div>
        </td>
        <td align="right" valign="top">
          <div style="background:{reg_bg};color:{reg_fg};padding:10px 18px;border-radius:8px;font-weight:800;font-size:16px;border:1px solid {reg_fg}40;text-align:center">
            {regime}
          </div>
          <div style="color:#7f8c8d;font-size:12px;margin-top:6px;text-align:right">
            VIX {vix_str} · {d.get('vix_confidence','—')}
          </div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- SYSTEM HEALTH -->
  <tr><td style="padding-bottom:20px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      System Health
    </div>
    <div style="background:#161b22;border-radius:10px;border:1px solid #21262d;padding:16px 18px;margin-bottom:10px">
      <div style="font-size:15px;font-weight:800;color:{health_summary_color};margin-bottom:14px">
        {health_summary_label}
      </div>
      <table width="100%" cellspacing="0">{health_rows}</table>
    </div>
  </td></tr>

  <!-- NAV CARDS -->
  <tr><td style="padding-bottom:20px">
    <table width="100%" cellspacing="0">
      <tr>
        <td width="32%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Total NAV</div>
          <div style="font-size:26px;font-weight:800;color:#ecf0f1;margin-top:6px">{fmt_usd(d.get('nav'))}</div>
        </td>
        <td width="4%"></td>
        <td width="32%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Total Return</div>
          <div style="font-size:26px;font-weight:800;color:{return_color};margin-top:6px">{fmt_pct(d.get('return_pct'))}</div>
          <div style="font-size:11px;color:#555;margin-top:2px">vs $100k baseline</div>
        </td>
        <td width="4%"></td>
        <td width="32%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Today P&amp;L</div>
          <div style="font-size:26px;font-weight:800;color:{today_pnl_color};margin-top:6px">{fmt_usd(today_pnl)}</div>
          <div style="font-size:11px;color:#555;margin-top:2px">{len(d.get('closed_today',[]))} trades closed</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- STATS ROW -->
  <tr><td style="padding-bottom:20px">
    <table width="100%" style="background:#161b22;border-radius:10px;padding:18px 20px;border:1px solid #21262d">
      <tr>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 16px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Win Rate</div>
          <div style="font-size:22px;font-weight:700;color:#2ecc71;margin-top:4px">{d.get('win_rate',0):.0f}%</div>
        </td>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 16px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Total Trades</div>
          <div style="font-size:22px;font-weight:700;color:#ecf0f1;margin-top:4px">{d.get('total_trades',0)}</div>
        </td>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 16px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Avg Win</div>
          <div style="font-size:22px;font-weight:700;color:#2ecc71;margin-top:4px">{fmt_pct(d.get('avg_win_pct',0))}</div>
        </td>
        <td style="text-align:center;padding:0 16px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Avg Loss</div>
          <div style="font-size:22px;font-weight:700;color:#e74c3c;margin-top:4px">{fmt_pct(d.get('avg_loss_pct',0))}</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- OPEN POSITIONS -->
  <tr><td style="padding-bottom:20px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Open Positions ({d.get('n_positions',0)})
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse">
      <tr style="border-bottom:1px solid #21262d">
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SYMBOL</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SIDE</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">ENTRY</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">CURRENT</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">RETURN</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">P&amp;L</th>
      </tr>
      {pos_rows}
    </table>
  </td></tr>

  <!-- CLOSED TODAY -->
  <tr><td style="padding-bottom:20px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Closed Today ({len(d.get('closed_today',[]))})
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse">
      <tr style="border-bottom:1px solid #21262d">
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SYMBOL</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">ENTRY</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">EXIT</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">RETURN</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">P&amp;L</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">REASON</th>
      </tr>
      {closed_rows}
    </table>
  </td></tr>

  <!-- TOP PICKS TOMORROW -->
  <tr><td style="padding-bottom:20px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Top Picks for Tomorrow <span style="color:#555;font-weight:400;font-size:10px">(cache {cache_age_str})</span>
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse">
      <tr style="border-bottom:1px solid #21262d">
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">#</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SYMBOL</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SECTOR</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">CONFIDENCE</th>
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SCORE</th>
      </tr>
      {picks_rows}
    </table>
  </td></tr>

  <!-- MARKET CONDITIONS -->
  <tr><td style="padding-bottom:20px">
    <div style="background:#161b22;border-radius:10px;border:1px solid #21262d;padding:18px 20px">
      <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Market Conditions</div>
      <table width="100%">
        <tr>
          <td style="color:#bdc3c7;font-size:13px;padding:3px 0">S&amp;P 500</td>
          <td style="color:#ecf0f1;font-weight:700;font-size:13px;text-align:right">{fmt_usd(d.get('sp500_price'))}</td>
        </tr>
        <tr>
          <td style="color:#bdc3c7;font-size:13px;padding:3px 0">VIX</td>
          <td style="color:#ecf0f1;font-weight:700;font-size:13px;text-align:right">{vix_str} <span style="color:#555;font-weight:400">({d.get('vix_source','—')})</span></td>
        </tr>
        <tr>
          <td style="color:#bdc3c7;font-size:13px;padding:3px 0">Market Breadth</td>
          <td style="color:#ecf0f1;font-weight:700;font-size:13px;text-align:right">{d.get('breadth') or '—'}% above 50-SMA</td>
        </tr>
        <tr>
          <td style="color:#bdc3c7;font-size:13px;padding:3px 0">Regime Confidence</td>
          <td style="color:#ecf0f1;font-weight:700;font-size:13px;text-align:right">{d.get('reg_conf',0)}%</td>
        </tr>
      </table>
      {reg_details_html}
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td>
    <div style="text-align:center;padding:16px;font-size:11px;color:#555;line-height:2">
      Sentinel Quant · Epic Fury Capital · Automated Daily Report<br>
      Fires every weekday at 3:00 PM ET<br>
      <span style="color:#333">Not financial advice. Do your own research.</span>
    </div>
  </td></tr>

</table>
</body>
</html>"""


# ============================================================
#  Send
# ============================================================

def send_daily_report() -> dict:
    """
    Build and send the daily report email.
    Called by APScheduler at 3:00 PM ET Mon-Fri.
    Also callable from POST /api/admin/send-daily-report.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not app_password:
        return {"ok": False, "error": "GMAIL_APP_PASSWORD env var not set"}

    try:
        date_str = datetime.now().strftime("%A, %B %-d %Y")
        d        = _collect_report_data()
        html     = _build_html(d, date_str)

        # Build subject with health flag
        checks   = d.get("health_checks") or []
        n_errors = sum(1 for c in checks if c["status"] == "error")
        n_warns  = sum(1 for c in checks if c["status"] == "warn")
        if n_errors:
            flag = f" 🚨 {n_errors} Problem{'s' if n_errors>1 else ''}"
        elif n_warns:
            flag = f" ⚠️ {n_warns} Warning{'s' if n_warns>1 else ''}"
        else:
            flag = " ✅ All Clear"

        nav_str = f"${d.get('nav', 0):,.0f}"
        ret_str = f"{d.get('return_pct', 0):+.1f}%"
        subject = f"Epic Fury | {date_str} | {nav_str} {ret_str}{flag}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL
        msg["To"]      = TO_EMAIL
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(FROM_EMAIL, app_password)
            server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())

        logger.warning(
            f"DAILY REPORT: sent — NAV={nav_str} return={ret_str} "
            f"errors={n_errors} warns={n_warns}"
        )
        return {"ok": True, "nav": nav_str, "return": ret_str,
                "health_errors": n_errors, "health_warns": n_warns}

    except smtplib.SMTPAuthenticationError:
        err = "Gmail auth failed — check GMAIL_APP_PASSWORD env var"
        logger.error(f"DAILY REPORT: {err}")
        return {"ok": False, "error": err}
    except Exception as e:
        logger.error(f"DAILY REPORT: send failed — {e}")
        return {"ok": False, "error": str(e)[:200]}
