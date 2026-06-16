"""
Daily Performance Email — Epic Fury / Sentinel Quant
Sends a market-close summary every trading day at 4:30 PM ET.

From: sentinelquantreports@gmail.com
To:   jacksonwhanglee@gmail.com

Requires env var: GMAIL_APP_PASSWORD
"""

import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger(__name__)

FROM_EMAIL = "jacksonwhanglee@gmail.com"
TO_EMAIL   = "jacksonwhanglee@gmail.com"
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587


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
        data["nav"]          = stats.get("total_value", 0)
        data["cash"]         = stats.get("cash", 0)
        data["positions_val"]= stats.get("positions_value", 0)
        data["return_pct"]   = stats.get("total_return_pct", 0)
        data["n_positions"]  = len(stats.get("positions", []))
        data["positions"]    = stats.get("positions", [])
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
    except Exception as e:
        logger.warning(f"Email: VIX error — {e}")
        data["vix"] = None

    try:
        from analysis.quant_engine import detect_market_regime
        regime = detect_market_regime() or {}
        data["regime"]     = regime.get("regime", "UNKNOWN")
        data["sp500_price"]= regime.get("sp500_price")
        data["breadth"]    = regime.get("breadth_pct")
    except Exception as e:
        logger.warning(f"Email: regime error — {e}")
        data["regime"] = "UNKNOWN"

    # Top picks for tomorrow
    try:
        from analysis.quant_engine import _quant_cache
        cache_entry = _quant_cache.get("quant_picks") or {}
        picks_data  = cache_entry.get("data") or {}
        longs = picks_data.get("long_picks") or []
        data["top_longs"] = longs[:8]
    except Exception as e:
        logger.warning(f"Email: picks error — {e}")
        data["top_longs"] = []

    # Win rate (performance stats)
    try:
        from predictions.paper_trader import get_performance_metrics
        perf = get_performance_metrics() or {}
        data["win_rate"]      = perf.get("win_rate", 0)
        data["total_trades"]  = perf.get("total_trades", 0)
        data["avg_win_pct"]   = perf.get("avg_win_pct", 0)
        data["avg_loss_pct"]  = perf.get("avg_loss_pct", 0)
    except Exception as e:
        logger.warning(f"Email: performance metrics error — {e}")
        data.update({"win_rate": 0, "total_trades": 0,
                     "avg_win_pct": 0, "avg_loss_pct": 0})

    return data


# ============================================================
#  HTML template
# ============================================================

def _build_html(d: dict, date_str: str) -> str:
    # Color helpers
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

    # VIX badge
    vix_val  = d.get("vix")
    vix_str  = f"{vix_val:.1f}" if vix_val else "N/A"
    vix_conf = d.get("vix_confidence", "")
    vix_src  = d.get("vix_source", "")

    # Positions rows
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
        <tr>
          <td style="padding:8px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:8px 12px">{side_badge}</td>
          <td style="padding:8px 12px;color:#bdc3c7">{fmt_usd(entry)}</td>
          <td style="padding:8px 12px;color:#bdc3c7">{fmt_usd(cur)}</td>
          <td style="padding:8px 12px;color:{pct_color(pnl_pct)};font-weight:700">{fmt_pct(pnl_pct)}</td>
          <td style="padding:8px 12px;color:{pct_color(pnl_val)};font-weight:700">{fmt_usd(pnl_val)}</td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="6" style="padding:12px;color:#7f8c8d;text-align:center">No open positions</td></tr>'

    # Closed today rows
    closed_rows = ""
    today_pnl = 0.0
    for t in (d.get("closed_today") or []):
        sym     = t.get("ticker") or t.get("symbol", "?")
        entry   = t.get("entry_price", 0)
        ex      = t.get("exit_price") or t.get("close_price") or entry
        pnl_pct = ((ex - entry) / entry * 100) if entry else 0
        pnl_val = t.get("pnl") or t.get("realized_pnl") or 0
        today_pnl += float(pnl_val or 0)
        reason  = t.get("close_reason") or t.get("exit_reason") or "—"
        closed_rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:8px 12px;color:#bdc3c7">{fmt_usd(entry)}</td>
          <td style="padding:8px 12px;color:#bdc3c7">{fmt_usd(ex)}</td>
          <td style="padding:8px 12px;color:{pct_color(pnl_pct)};font-weight:700">{fmt_pct(pnl_pct)}</td>
          <td style="padding:8px 12px;color:{pct_color(pnl_val)};font-weight:700">{fmt_usd(pnl_val)}</td>
          <td style="padding:8px 12px;color:#7f8c8d;font-size:12px">{reason}</td>
        </tr>"""

    if not closed_rows:
        closed_rows = '<tr><td colspan="6" style="padding:12px;color:#7f8c8d;text-align:center">No trades closed today</td></tr>'

    # Top picks rows
    picks_rows = ""
    for i, p in enumerate(d.get("top_longs") or [], 1):
        sym  = p.get("symbol") or p.get("ticker", "?")
        conf = p.get("confidence") or p.get("confidence_score") or 0
        scr  = p.get("composite_score") or p.get("score") or 0
        sec  = p.get("sector") or "—"
        picks_rows += f"""
        <tr>
          <td style="padding:8px 12px;color:#7f8c8d">{i}</td>
          <td style="padding:8px 12px;font-weight:700;color:#ecf0f1">{sym}</td>
          <td style="padding:8px 12px;color:#bdc3c7">{sec}</td>
          <td style="padding:8px 12px;color:#2ecc71;font-weight:700">{conf:.0f}%</td>
          <td style="padding:8px 12px;color:#3498db">{scr:.2f}</td>
        </tr>"""

    if not picks_rows:
        picks_rows = '<tr><td colspan="5" style="padding:12px;color:#7f8c8d;text-align:center">Picks updating — check dashboard</td></tr>'

    today_pnl_color = pct_color(today_pnl)
    return_color    = pct_color(d.get("return_pct", 0))

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:'Helvetica Neue',Arial,sans-serif;color:#ecf0f1">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:700px;margin:0 auto;padding:24px 16px">

  <!-- HEADER -->
  <tr><td>
    <table width="100%" style="background:linear-gradient(135deg,#1a1a2e,#16213e);border-radius:12px;padding:28px;margin-bottom:20px">
      <tr>
        <td>
          <div style="font-size:11px;color:#7f8c8d;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px">Sentinel Quant · Epic Fury Capital</div>
          <div style="font-size:26px;font-weight:800;color:#ecf0f1">Daily Report</div>
          <div style="font-size:14px;color:#7f8c8d;margin-top:4px">{date_str} · Market Close</div>
        </td>
        <td align="right">
          <div style="background:{reg_bg};color:{reg_fg};padding:10px 18px;border-radius:8px;font-weight:800;font-size:16px;border:1px solid {reg_fg}40">
            {regime}
          </div>
          <div style="color:#7f8c8d;font-size:12px;margin-top:6px;text-align:right">
            VIX {vix_str} <span style="color:#555">· {vix_conf}</span>
          </div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- NAV CARDS -->
  <tr><td>
    <table width="100%" cellspacing="12" style="margin-bottom:20px">
      <tr>
        <td width="33%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Total NAV</div>
          <div style="font-size:28px;font-weight:800;color:#ecf0f1;margin-top:6px">{fmt_usd(d.get('nav'))}</div>
        </td>
        <td width="33%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Total Return</div>
          <div style="font-size:28px;font-weight:800;color:{return_color};margin-top:6px">{fmt_pct(d.get('return_pct'))}</div>
          <div style="font-size:11px;color:#555;margin-top:2px">vs $100k baseline</div>
        </td>
        <td width="33%" style="background:#161b22;border-radius:10px;padding:20px;border:1px solid #21262d;text-align:center">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px">Today P&amp;L</div>
          <div style="font-size:28px;font-weight:800;color:{today_pnl_color};margin-top:6px">{fmt_usd(today_pnl)}</div>
          <div style="font-size:11px;color:#555;margin-top:2px">{len(d.get('closed_today',[]))} trades closed</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- STATS ROW -->
  <tr><td>
    <table width="100%" style="background:#161b22;border-radius:10px;padding:18px 20px;border:1px solid #21262d;margin-bottom:20px">
      <tr>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 20px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Win Rate</div>
          <div style="font-size:22px;font-weight:700;color:#2ecc71;margin-top:4px">{d.get('win_rate',0):.0f}%</div>
        </td>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 20px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Total Trades</div>
          <div style="font-size:22px;font-weight:700;color:#ecf0f1;margin-top:4px">{d.get('total_trades',0)}</div>
        </td>
        <td style="text-align:center;border-right:1px solid #21262d;padding:0 20px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Avg Win</div>
          <div style="font-size:22px;font-weight:700;color:#2ecc71;margin-top:4px">{fmt_pct(d.get('avg_win_pct',0))}</div>
        </td>
        <td style="text-align:center;padding:0 20px">
          <div style="font-size:11px;color:#7f8c8d;text-transform:uppercase">Avg Loss</div>
          <div style="font-size:22px;font-weight:700;color:#e74c3c;margin-top:4px">{fmt_pct(d.get('avg_loss_pct',0))}</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- OPEN POSITIONS -->
  <tr><td>
    <div style="font-size:13px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Open Positions ({d.get('n_positions',0)})
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse;margin-bottom:20px">
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
  <tr><td>
    <div style="font-size:13px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Closed Today ({len(d.get('closed_today',[]))})
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse;margin-bottom:20px">
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
  <tr><td>
    <div style="font-size:13px;font-weight:700;color:#7f8c8d;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">
      Top Picks for Tomorrow
    </div>
    <table width="100%" style="background:#161b22;border-radius:10px;border:1px solid #21262d;border-collapse:collapse;margin-bottom:20px">
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

  <!-- FOOTER -->
  <tr><td>
    <div style="text-align:center;padding:20px;font-size:11px;color:#555;line-height:1.8">
      Sentinel Quant · Epic Fury Capital · Automated Report<br>
      S&amp;P 500: {fmt_usd(d.get('sp500_price'))} · Market Breadth: {d.get('breadth') or '—'}% above 50-SMA<br>
      <span style="color:#333">This report is auto-generated. Not financial advice.</span>
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
    Called by APScheduler at 4:30 PM ET Mon-Fri.
    Also callable from /api/admin/send-daily-report for manual trigger.

    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not app_password:
        return {"ok": False, "error": "GMAIL_APP_PASSWORD env var not set"}

    try:
        date_str = datetime.now().strftime("%A, %B %-d %Y")
        d        = _collect_report_data()
        html     = _build_html(d, date_str)

        nav_str    = f"${d.get('nav', 0):,.0f}"
        ret_str    = f"{d.get('return_pct', 0):+.1f}%"
        subject    = f"Epic Fury Daily | {date_str} | NAV {nav_str} | {ret_str}"

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
            f"DAILY REPORT: sent to {TO_EMAIL} | "
            f"NAV={nav_str} return={ret_str} "
            f"regime={d.get('regime')} VIX={d.get('vix')}"
        )
        return {"ok": True, "nav": nav_str, "return": ret_str}

    except smtplib.SMTPAuthenticationError:
        msg = "Gmail auth failed — check GMAIL_APP_PASSWORD env var"
        logger.error(f"DAILY REPORT: {msg}")
        return {"ok": False, "error": msg}
    except Exception as e:
        logger.error(f"DAILY REPORT: send failed — {e}")
        return {"ok": False, "error": str(e)[:200]}
