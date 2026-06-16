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
from datetime import datetime, date

logger = logging.getLogger(__name__)

FROM_EMAIL = "jacksonwhanglee@gmail.com"
TO_EMAIL   = "jacksonwhanglee@gmail.com"
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587


# ============================================================
#  Data collection — always reads from the same source as the website
# ============================================================

def _collect_report_data() -> dict:
    data = {}

    # ── Portfolio: use paper-portfolio path (same as website) ──
    try:
        from predictions.paper_trader import get_portfolio_stats
        stats = get_portfolio_stats()
        data["nav"]        = float(stats.get("total_value") or 0)
        data["cash"]       = float(stats.get("cash") or 0)
        data["return_pct"] = float(stats.get("total_return_pct") or 0)
        data["positions"]  = stats.get("positions") or []
    except Exception as e:
        logger.warning(f"Email portfolio error: {e}")
        data.update({"nav": 0, "cash": 0, "return_pct": 0, "positions": []})

    # ── Today P&L: sum closed trades opened/closed today ──
    try:
        from predictions.models import get_closed_trades
        today_str  = date.today().isoformat()
        all_closed = get_closed_trades(limit=500) or []
        closed_today = [t for t in all_closed
                        if (t.get("closed_at") or "").startswith(today_str)]
        data["today_pnl"]    = sum(float(t.get("pnl") or t.get("realized_pnl") or 0)
                                   for t in closed_today)
        data["closed_count"] = len(closed_today)
    except Exception as e:
        logger.warning(f"Email closed trades error: {e}")
        data["today_pnl"]    = 0.0
        data["closed_count"] = 0

    # ── VIX: read from the cached regime (same as website, never a fresh fetch) ──
    try:
        from analysis.quant_engine import _quant_cache
        entry   = _quant_cache.get("quant_picks") or {}
        regime  = (entry.get("data") or {}).get("regime") or {}
        vix_val = regime.get("vix_level")
        # Only trust it if it's a sane value (5–60). If sanitized/None, fall back.
        if vix_val and 5 <= float(vix_val) <= 60:
            data["vix"] = round(float(vix_val), 2)
        else:
            # Secondary: try detect_market_regime cache (1-min TTL)
            from analysis.quant_engine import detect_market_regime
            reg2 = detect_market_regime() or {}
            v2   = reg2.get("vix_level")
            data["vix"] = round(float(v2), 2) if v2 and 5 <= float(v2) <= 60 else None
        data["regime"] = regime.get("regime") or "UNKNOWN"
    except Exception as e:
        logger.warning(f"Email VIX/regime error: {e}")
        data["vix"]    = None
        data["regime"] = "UNKNOWN"

    # ── Top 5 picks: from in-memory quant cache (same as STB page) ──
    try:
        from analysis.quant_engine import _quant_cache
        entry      = _quant_cache.get("quant_picks") or {}
        picks_data = entry.get("data") or {}
        longs      = picks_data.get("long_picks") or []
        data["top_picks"] = longs[:5]
    except Exception as e:
        logger.warning(f"Email picks error: {e}")
        data["top_picks"] = []

    return data


# ============================================================
#  System health checks (for subject line flag only)
# ============================================================

def _run_health_checks() -> list:
    checks = []

    # VIX sanity (use cached regime, not fresh fetch)
    try:
        from analysis.quant_engine import _quant_cache
        entry  = _quant_cache.get("quant_picks") or {}
        regime = (entry.get("data") or {}).get("regime") or {}
        vix    = regime.get("vix_level")
        if vix is None:
            checks.append({"label": "VIX", "status": "warn",
                           "detail": "VIX not yet in cache — scan still warming up"})
        elif float(vix) > 35:
            checks.append({"label": "VIX", "status": "warn",
                           "detail": f"VIX={vix:.1f} (CRISIS zone) — position sizing reduced"})
        else:
            checks.append({"label": "VIX", "status": "ok",
                           "detail": f"VIX={vix:.1f}"})
    except Exception as e:
        checks.append({"label": "VIX", "status": "warn", "detail": str(e)[:60]})

    # Picks engine
    try:
        from analysis.quant_engine import _quant_cache, _SCAN_RUNNING
        entry  = _quant_cache.get("quant_picks") or {}
        longs  = len((entry.get("data") or {}).get("long_picks") or [])
        age_m  = (time.time() - entry.get("time", 0)) / 60 if entry.get("time") else None
        if longs == 0:
            checks.append({"label": "Picks Engine", "status": "error",
                           "detail": "0 long picks in cache — STB empty"})
        elif _SCAN_RUNNING:
            checks.append({"label": "Picks Engine", "status": "warn",
                           "detail": f"{longs} picks (scan running, will refresh soon)"})
        else:
            checks.append({"label": "Picks Engine", "status": "ok",
                           "detail": f"{longs} long picks"})
    except Exception as e:
        checks.append({"label": "Picks Engine", "status": "error", "detail": str(e)[:60]})

    # Portfolio NAV sanity
    try:
        from predictions.paper_trader import get_portfolio_stats
        nav = float(get_portfolio_stats().get("total_value") or 0)
        if nav <= 0:
            checks.append({"label": "Portfolio", "status": "error",
                           "detail": "NAV = $0 — data missing"})
        else:
            checks.append({"label": "Portfolio", "status": "ok",
                           "detail": f"NAV = ${nav:,.0f}"})
    except Exception as e:
        checks.append({"label": "Portfolio", "status": "error", "detail": str(e)[:60]})

    # Auto-trading
    try:
        from predictions.models import get_trading_state
        if get_trading_state("auto_trading_disabled", "0") == "1":
            checks.append({"label": "Auto-Trading", "status": "warn",
                           "detail": "DISABLED — no new trades will open"})
        elif get_trading_state("trading_paused", "0") == "1":
            checks.append({"label": "Auto-Trading", "status": "warn",
                           "detail": "PAUSED"})
        else:
            checks.append({"label": "Auto-Trading", "status": "ok",
                           "detail": "Active"})
    except Exception as e:
        checks.append({"label": "Auto-Trading", "status": "warn", "detail": str(e)[:60]})

    return checks


# ============================================================
#  HTML template — clean, focused
# ============================================================

def _build_html(d: dict, date_str: str) -> str:

    def signed_pct(v, dec=2):
        try:    return f"{float(v):+.{dec}f}%"
        except: return "—"

    def usd(v):
        try:    return f"${float(v):,.0f}"
        except: return "—"

    def color(v):
        try:    return "#2ecc71" if float(v) >= 0 else "#e74c3c"
        except: return "#aaa"

    regime = (d.get("regime") or "UNKNOWN").upper()
    reg_colors = {
        "BULL":    ("#1a472a", "#2ecc71"),
        "BEAR":    ("#4a0000", "#e74c3c"),
        "SIDEWAYS":("#2c3e50", "#f39c12"),
        "CRISIS":  ("#4a0000", "#c0392b"),
    }
    reg_bg, reg_fg = reg_colors.get(regime, ("#2c3e50", "#ecf0f1"))

    vix_str = f"{d['vix']:.2f}" if d.get("vix") else "Updating..."

    # Health checks
    checks   = d.get("health_checks") or []
    n_errors = sum(1 for c in checks if c["status"] == "error")
    n_warns  = sum(1 for c in checks if c["status"] == "warn")
    icons    = {"ok": "✅", "warn": "⚠️", "error": "❌"}
    bgs      = {"ok": "#0d2b1a", "warn": "#2b2200", "error": "#2b0000"}
    fgs      = {"ok": "#2ecc71", "warn": "#f39c12", "error": "#e74c3c"}

    if n_errors:
        health_color = "#e74c3c"
        health_label = f"{n_errors} Problem{'s' if n_errors>1 else ''}" + (f" · {n_warns} Warning{'s' if n_warns>1 else ''}" if n_warns else "")
    elif n_warns:
        health_color = "#f39c12"
        health_label = f"{n_warns} Warning{'s' if n_warns>1 else ''}"
    else:
        health_color = "#2ecc71"
        health_label = "All Systems Nominal"

    health_rows = "".join(f"""
      <tr><td style="padding:8px 14px;background:{bgs[c['status']]};border-radius:6px;margin-bottom:3px;display:block">
        <span style="font-size:14px">{icons[c['status']]}</span>
        <span style="color:{fgs[c['status']]};font-weight:700;margin-left:8px">{c['label']}</span>
        <span style="color:#7f8c8d;font-size:12px;margin-left:8px">{c['detail']}</span>
      </td></tr><tr><td style="height:4px"></td></tr>""" for c in checks)

    # Top 5 picks rows
    pick_rows = ""
    for i, p in enumerate(d.get("top_picks") or [], 1):
        sym  = p.get("symbol") or p.get("ticker", "?")
        conf = p.get("confidence") or 0
        sec  = p.get("sector") or "—"
        pick_rows += f"""
        <tr style="border-bottom:1px solid #21262d">
          <td style="padding:11px 14px;color:#7f8c8d;font-size:13px">{i}</td>
          <td style="padding:11px 14px;font-weight:800;color:#ecf0f1;font-size:15px">{sym}</td>
          <td style="padding:11px 14px;color:#bdc3c7;font-size:13px">{sec}</td>
          <td style="padding:11px 14px;font-weight:700;font-size:15px;color:#2ecc71">{conf:.0f}%</td>
        </tr>"""
    if not pick_rows:
        pick_rows = '<tr><td colspan="4" style="padding:14px;color:#7f8c8d;text-align:center">Picks updating — check dashboard</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:'Helvetica Neue',Arial,sans-serif;color:#ecf0f1">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:620px;margin:0 auto;padding:24px 16px">

  <!-- HEADER -->
  <tr><td style="padding-bottom:20px">
    <table width="100%" style="background:linear-gradient(135deg,#1a1a2e,#16213e);border-radius:12px;padding:26px 28px">
      <tr>
        <td>
          <div style="font-size:11px;color:#7f8c8d;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px">Sentinel Quant · Epic Fury Capital</div>
          <div style="font-size:24px;font-weight:800;color:#ecf0f1">Daily Report</div>
          <div style="font-size:13px;color:#7f8c8d;margin-top:3px">{date_str} · 3:00 PM ET</div>
        </td>
        <td align="right" valign="top">
          <div style="background:{reg_bg};color:{reg_fg};padding:9px 16px;border-radius:8px;font-weight:800;font-size:15px;border:1px solid {reg_fg}40;text-align:center">{regime}</div>
          <div style="color:#7f8c8d;font-size:12px;margin-top:6px;text-align:right">VIX {vix_str}</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- KEY NUMBERS -->
  <tr><td style="padding-bottom:16px">
    <table width="100%" cellspacing="0">
      <tr>
        <!-- Total Return -->
        <td width="31%" style="background:#161b22;border-radius:10px;padding:20px 16px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Total Return</div>
          <div style="font-size:30px;font-weight:800;color:{color(d.get('return_pct'))}">{signed_pct(d.get('return_pct'),1)}</div>
          <div style="font-size:11px;color:#555;margin-top:3px">vs $100k baseline</div>
        </td>
        <td width="3%"></td>
        <!-- Cash -->
        <td width="31%" style="background:#161b22;border-radius:10px;padding:20px 16px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Cash</div>
          <div style="font-size:30px;font-weight:800;color:#ecf0f1">{usd(d.get('cash'))}</div>
          <div style="font-size:11px;color:#555;margin-top:3px">NAV {usd(d.get('nav'))}</div>
        </td>
        <td width="3%"></td>
        <!-- Today P&L -->
        <td width="31%" style="background:#161b22;border-radius:10px;padding:20px 16px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Today P&amp;L</div>
          <div style="font-size:30px;font-weight:800;color:{color(d.get('today_pnl'))}">{usd(d.get('today_pnl'))}</div>
          <div style="font-size:11px;color:#555;margin-top:3px">{d.get('closed_count',0)} trade{'s' if d.get('closed_count',0)!=1 else ''} closed</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- VIX CALLOUT -->
  <tr><td style="padding-bottom:16px">
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;padding:16px 20px">
      <tr>
        <td>
          <span style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">VIX (Fear Index)</span>
        </td>
        <td align="right">
          <span style="font-size:24px;font-weight:800;color:{'#2ecc71' if d.get('vix') and d['vix'] < 20 else '#f39c12' if d.get('vix') and d['vix'] < 30 else '#e74c3c'}">{vix_str}</span>
          <span style="font-size:13px;color:#7f8c8d;margin-left:8px">{'Low volatility · Good for longs' if d.get('vix') and d['vix'] < 20 else 'Elevated · Trade smaller' if d.get('vix') and d['vix'] < 30 else 'High · CRISIS mode'}</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- TOP 5 PICKS -->
  <tr><td style="padding-bottom:16px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">Top 5 Picks (STB)</div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse">
      <tr style="border-bottom:1px solid #21262d">
        <th style="padding:10px 14px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">#</th>
        <th style="padding:10px 14px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SYMBOL</th>
        <th style="padding:10px 14px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">SECTOR</th>
        <th style="padding:10px 14px;text-align:left;font-size:11px;color:#7f8c8d;font-weight:600">CONFIDENCE</th>
      </tr>
      {pick_rows}
    </table>
  </td></tr>

  <!-- SYSTEM HEALTH -->
  <tr><td style="padding-bottom:16px">
    <div style="font-size:11px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">System Health</div>
    <div style="background:#161b22;border-radius:10px;border:1px solid #21262d;padding:14px 16px">
      <div style="font-size:14px;font-weight:800;color:{health_color};margin-bottom:12px">{health_label}</div>
      <table width="100%" cellspacing="0">{health_rows}</table>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td>
    <div style="text-align:center;padding:16px;font-size:11px;color:#555;line-height:2">
      Sentinel Quant · Epic Fury Capital · Daily Report · 3:00 PM ET<br>
      <span style="color:#333">Not financial advice.</span>
    </div>
  </td></tr>

</table>
</body>
</html>"""


# ============================================================
#  Send
# ============================================================

def send_daily_report() -> dict:
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not app_password:
        return {"ok": False, "error": "GMAIL_APP_PASSWORD env var not set"}

    try:
        date_str = datetime.now().strftime("%A, %B %-d %Y")
        d        = _collect_report_data()
        d["health_checks"] = _run_health_checks()
        html     = _build_html(d, date_str)

        checks   = d["health_checks"]
        n_errors = sum(1 for c in checks if c["status"] == "error")
        n_warns  = sum(1 for c in checks if c["status"] == "warn")
        flag     = (" 🚨 Problems" if n_errors else " ⚠️ Warnings" if n_warns else " ✅")

        ret_str  = f"{d.get('return_pct', 0):+.1f}%"
        nav_str  = f"${d.get('nav', 0):,.0f}"
        vix_str  = f"VIX {d['vix']:.2f}" if d.get("vix") else "VIX updating"
        subject  = f"Epic Fury | {date_str} | {ret_str} | {vix_str}{flag}"

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

        logger.warning(f"DAILY REPORT sent: return={ret_str} nav={nav_str} vix={d.get('vix')} errors={n_errors} warns={n_warns}")
        return {"ok": True, "nav": nav_str, "return": ret_str,
                "vix": d.get("vix"), "health_errors": n_errors, "health_warns": n_warns}

    except smtplib.SMTPAuthenticationError:
        err = "Gmail auth failed — check GMAIL_APP_PASSWORD"
        logger.error(f"DAILY REPORT: {err}")
        return {"ok": False, "error": err}
    except Exception as e:
        logger.error(f"DAILY REPORT send failed: {e}")
        return {"ok": False, "error": str(e)[:200]}
