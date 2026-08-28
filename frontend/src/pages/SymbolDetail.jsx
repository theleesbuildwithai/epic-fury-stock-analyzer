import { useState, useEffect } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import AnalysisDashboard from '../components/AnalysisDashboard'
import { addPickToWatchlist } from '../lib/watchlist'

// ─── Quant HF Signal Card ─────────────────────────────────────────────────────
function QuantHFCard({ sig }) {
  if (!sig) return null
  const isLong  = sig.direction === 'LONG'
  const isShort = sig.direction === 'SHORT'
  const dirCol  = isLong ? 'text-emerald-300' : isShort ? 'text-rose-300' : 'text-neutral-400'
  const borderCol = isLong ? 'border-emerald-800/40' : isShort ? 'border-rose-800/40' : 'border-neutral-700'
  const bgCol     = isLong ? 'bg-emerald-950/20' : isShort ? 'bg-rose-950/20' : 'bg-neutral-900/20'
  const arrow     = isLong ? '▲' : isShort ? '▼' : '·'

  const topFactors = sig.factors
    ? Object.entries(sig.factors)
        .map(([k, v]) => ({ name: k, contrib: v?.contribution ?? 0 }))
        .filter(e => Math.abs(e.contrib) > 0.02)
        .sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib))
        .slice(0, 6)
    : []
  const maxAbs = topFactors.length ? Math.max(...topFactors.map(e => Math.abs(e.contrib)), 0.01) : 0.01

  return (
    <div className={`${bgCol} border ${borderCol} rounded-xl p-5 mb-6`}>
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 mb-1">
            Quant HF · 22-Factor Technical Model
          </p>
          <div className="flex items-center gap-3">
            <span className={`text-2xl font-black ${dirCol}`}>{arrow} {sig.direction}</span>
            <span className="text-neutral-600">|</span>
            <div>
              <div className="text-[10px] text-neutral-500">Confidence</div>
              <div className={`text-xl font-black font-mono ${dirCol}`}>{sig.confidence ?? '—'}%</div>
            </div>
            <div>
              <div className="text-[10px] text-neutral-500">Composite score</div>
              <div className="text-xl font-black font-mono text-white">{sig.composite_score?.toFixed(2) ?? '—'}</div>
            </div>
          </div>
        </div>
        <div className="text-[10px] text-neutral-600 text-right">
          <div>Regime-aware multi-factor</div>
          <div>momentum · value · quality</div>
          <div>RSI · vol · smart money</div>
        </div>
      </div>

      {topFactors.length > 0 && (
        <div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 mb-2">Factor breakdown</p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5">
            {topFactors.map(e => {
              const bull = e.contrib > 0
              const pct  = Math.abs(e.contrib) / maxAbs * 100
              return (
                <div key={e.name} className="flex items-center gap-2">
                  <span className="text-[10px] text-neutral-400 w-28 truncate capitalize">{e.name.replace(/_/g, ' ')}</span>
                  <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${bull ? 'bg-emerald-500/70' : 'bg-rose-500/70'}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className={`text-[10px] font-mono w-10 text-right ${bull ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {e.contrib > 0 ? '+' : ''}{e.contrib.toFixed(2)}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// SYMBOL DETAIL — drill-down for a Symbols-to-Buy pick.
// Shows the why-to-buy (entry/stop/target/R-R/reasons from the picks engine)
// PLUS the full /api/analyze view (chart, signal, indicators, risk, forecast).

function formatPrice(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Number(v).toFixed(2)
}
function formatPct(v) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}

// Carry an STB pick into the Watchlist with its entry/stop/target intact, so the
function PickSummaryCard({ pick, side }) {
  if (!pick) return null
  const isLong = side === 'long'
  const dirColor = isLong ? 'text-emerald-300' : 'text-rose-300'
  const dirBg = isLong ? 'from-emerald-950/30 to-emerald-900/10 border-emerald-700/40' : 'from-rose-950/30 to-rose-900/10 border-rose-700/40'
  const [wlState, setWlState] = useState(null) // null | 'added' | 'exists' | 'error'

  return (
    <div className={`bg-gradient-to-br ${dirBg} border rounded-xl p-6 mb-6`}>
      <div className="flex flex-wrap items-start justify-between gap-4 mb-5">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-3xl font-black text-white">{pick.ticker}</h2>
            <span className={`px-2 py-1 rounded text-xs font-bold border ${
              isLong ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-300'
                     : 'bg-rose-500/15 border-rose-500/40 text-rose-300'
            }`}>
              {isLong ? 'LONG' : 'SHORT'}
            </span>
          </div>
          <p className="text-sm text-neutral-400 mt-1">
            {pick.sector || 'Unknown sector'} · Hold {pick.recommended_hold || '3-5 days minimum'}
          </p>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-neutral-500">Model confidence</div>
          <div className={`text-2xl font-black ${dirColor}`}>
            {pick.confidence != null ? Math.round(pick.confidence) + '%' : '—'}
          </div>
          <div className="text-[10px] text-neutral-500">
            Score {pick.composite_score?.toFixed(2) ?? '—'}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-neutral-500">Entry</div>
          <div className="text-lg font-bold text-white font-mono">{formatPrice(pick.entry_price)}</div>
        </div>
        <div className="bg-neutral-900/60 border border-rose-800/40 rounded-lg px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-rose-400">Stop loss</div>
          <div className="text-lg font-bold text-rose-200 font-mono">{formatPrice(pick.stop_loss)}</div>
          <div className="text-[10px] text-neutral-500">
            {pick.stop_distance_pct != null
              ? (isLong ? '-' : '+') + pick.stop_distance_pct.toFixed(1) + '%'
              : '—'}
          </div>
        </div>
        <div className="bg-neutral-900/60 border border-emerald-800/40 rounded-lg px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-emerald-400">Target</div>
          <div className="text-lg font-bold text-emerald-200 font-mono">{formatPrice(pick.target_price)}</div>
          <div className="text-[10px] text-neutral-500">
            {pick.target_distance_pct != null
              ? (isLong ? '+' : '-') + pick.target_distance_pct.toFixed(1) + '%'
              : '—'}
          </div>
        </div>
        <div className="bg-neutral-900/60 border border-neutral-700/60 rounded-lg px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-neutral-400">Reward / Risk</div>
          <div className="text-lg font-bold text-white font-mono">
            {pick.reward_risk_ratio != null ? pick.reward_risk_ratio.toFixed(2) + 'x' : '—'}
          </div>
          <div className="text-[10px] text-neutral-500">2.0x minimum</div>
        </div>
      </div>

      {pick.reasons && pick.reasons.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-2 font-bold">
            Why {isLong ? 'BUY' : 'SHORT'} — model reasoning
          </div>
          <ul className="space-y-1.5">
            {pick.reasons.map((r, i) => (
              <li key={i} className="text-sm text-neutral-200 flex items-start gap-2">
                <span className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${isLong ? 'bg-emerald-400' : 'bg-rose-400'}`}></span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-5 grid grid-cols-3 gap-3 text-xs">
        {pick.rsi14 != null && (
          <div className="bg-neutral-900/60 border border-neutral-800 rounded px-3 py-2">
            <span className="text-neutral-500">RSI(14)</span>
            <span className="ml-2 text-white font-mono font-bold">{pick.rsi14.toFixed(1)}</span>
          </div>
        )}
        {pick.volatility_60d_pct != null && (
          <div className="bg-neutral-900/60 border border-neutral-800 rounded px-3 py-2">
            <span className="text-neutral-500">60d vol</span>
            <span className="ml-2 text-white font-mono font-bold">{pick.volatility_60d_pct.toFixed(1)}%</span>
          </div>
        )}
        {pick.momentum_pct != null && (
          <div className="bg-neutral-900/60 border border-neutral-800 rounded px-3 py-2">
            <span className="text-neutral-500">Momentum</span>
            <span className={`ml-2 font-mono font-bold ${pick.momentum_pct >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
              {formatPct(pick.momentum_pct)}
            </span>
          </div>
        )}
      </div>

      {isLong && (
        <div className="mt-5">
          <button
            onClick={() => setWlState(addPickToWatchlist(pick))}
            disabled={wlState === 'added' || wlState === 'exists'}
            className={`w-full px-4 py-3 rounded-lg font-bold text-sm transition-colors ${
              wlState === 'added'
                ? 'bg-emerald-600/20 border border-emerald-600/50 text-emerald-300 cursor-default'
                : wlState === 'exists'
                ? 'bg-neutral-800 border border-neutral-700 text-neutral-400 cursor-default'
                : wlState === 'error'
                ? 'bg-rose-600/20 border border-rose-600/50 text-rose-300 hover:bg-rose-600/30'
                : 'bg-white text-black hover:bg-neutral-200'
            }`}
          >
            {wlState === 'added'
              ? '✓ Added to watchlist — stop & target carried over'
              : wlState === 'exists'
              ? 'Already in your watchlist'
              : wlState === 'error'
              ? 'Could not add — try again'
              : 'Add to watchlist (buy this pick)'}
          </button>
          <p className="text-[11px] text-neutral-500 mt-2 text-center">
            Adds {pick.ticker} with the entry, stop {formatPrice(pick.stop_loss)} & target {formatPrice(pick.target_price)} above.
            Watchlist will alert you to SELL when price hits either level.
          </p>
        </div>
      )}

      <div className="mt-5 px-3 py-2 bg-neutral-900/50 border border-neutral-700/50 rounded text-xs text-neutral-300">
        <span className="font-bold text-white">Position discipline.</span> Minimum hold horizon of 3–5
        trading days; leading positions typically run 4–8 weeks. Honor the predefined stop without exception.
      </div>
    </div>
  )
}

// ─── Why-to-buy evidence panel (detail page only) ─────────────────────────────
// Turns the STB pick's real fundamentals + momentum + risk into a grouped,
// plain-English case so the user can SEE why to buy, not just be told to.
// Every metric is optional: shows "—" with a neutral note when unavailable.
// Palette discipline: green = favorable/bullish, red = unfavorable/bearish,
// everything else white/neutral.
function num1(v, suffix = '') {
  return (v == null || isNaN(v)) ? '—' : Number(v).toFixed(1) + suffix
}
function num2(v, suffix = '') {
  return (v == null || isNaN(v)) ? '—' : Number(v).toFixed(2) + suffix
}

function MetricRow({ label, value, tone = 'neutral', note }) {
  const valCol = tone === 'good' ? 'text-emerald-300' : tone === 'bad' ? 'text-rose-300' : 'text-white'
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-neutral-800/50 last:border-0">
      <span className="text-[11px] uppercase tracking-wider text-neutral-500 shrink-0">{label}</span>
      <div className="text-right min-w-0">
        <span className={`text-sm font-mono font-bold ${valCol}`}>{value}</span>
        {note && <div className="text-[10px] text-neutral-500 leading-tight">{note}</div>}
      </div>
    </div>
  )
}

function Group({ title, children }) {
  return (
    <div className="bg-neutral-900/50 border border-neutral-800 rounded-lg px-4 py-3">
      <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 mb-2">{title}</div>
      {children}
    </div>
  )
}

function WhyToBuyPanel({ pick }) {
  if (!pick) return null
  const n = (v) => (v == null || isNaN(v)) ? null : Number(v)
  const pe = n(pick.pe), fpe = n(pick.fwd_pe), peg = n(pick.peg_ratio)
  const roe = n(pick.roe_pct), margin = n(pick.profit_margin_pct), de = n(pick.debt_equity)
  const rev = n(pick.revenue_growth_pct), eps = n(pick.earnings_growth_pct)
  const mom = n(pick.momentum_12m_pct) ?? n(pick.momentum_pct)
  const vol = n(pick.volatility_60d_pct)
  const rr = n(pick.reward_risk_ratio)

  // Nothing to show if the enrichment + risk fields are all missing.
  const hasFund = [pe, fpe, peg, roe, margin, de, rev, eps].some(v => v != null)
  const hasMom = mom != null || vol != null
  if (!hasFund && !hasMom && rr == null) return null

  // valuation
  const peTone = pe == null ? 'neutral' : pe <= 20 ? 'good' : 'neutral'
  const pegTone = peg == null ? 'neutral' : (peg > 0 && peg <= 1.5) ? 'good' : 'neutral'
  const growsEarn = (fpe != null && pe != null && fpe > 0 && pe > 0 && fpe < pe)

  return (
    <div className="bg-neutral-900/30 border border-neutral-700/50 rounded-xl p-6 mb-6">
      <div className="mb-4">
        <h3 className="text-lg font-black text-white">Why to buy — the evidence</h3>
        <p className="text-xs text-neutral-500 mt-0.5">
          Fundamentals, growth, trend and risk behind the model call. Sourced from live financials;
          blanks show where data is unavailable.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Group title="Valuation">
          <MetricRow label="P/E (ttm)" value={num1(pe)} tone={peTone}
            note={pe == null ? 'not available' : pe <= 20 ? 'reasonably valued' : pe <= 35 ? 'fair valuation' : 'premium valuation'} />
          <MetricRow label="Forward P/E" value={num1(fpe)} tone={growsEarn ? 'good' : 'neutral'}
            note={fpe == null ? 'not available' : growsEarn ? 'earnings expected to grow' : 'vs trailing P/E'} />
          <MetricRow label="PEG" value={num2(peg)} tone={pegTone}
            note={peg == null ? 'not available' : (peg > 0 && peg <= 1) ? 'growth cheaply priced' : (peg <= 1.5) ? 'growth reasonably priced' : 'growth fully priced'} />
        </Group>

        <Group title="Quality">
          <MetricRow label="ROE" value={num1(roe, '%')} tone={roe == null ? 'neutral' : roe >= 15 ? 'good' : 'neutral'}
            note={roe == null ? 'not available' : roe >= 15 ? 'efficient capital returns' : 'return on equity'} />
          <MetricRow label="Profit margin" value={num1(margin, '%')} tone={margin == null ? 'neutral' : margin >= 15 ? 'good' : 'neutral'}
            note={margin == null ? 'not available' : margin >= 15 ? 'strong pricing power' : 'net margin'} />
          <MetricRow label="Debt / equity" value={num2(de)} tone={de == null ? 'neutral' : de <= 1 ? 'good' : de > 2 ? 'bad' : 'neutral'}
            note={de == null ? 'not available' : de <= 1 ? 'solid balance sheet' : de > 2 ? 'elevated leverage' : 'moderate leverage'} />
        </Group>

        <Group title="Growth">
          <MetricRow label="Revenue growth" value={num1(rev, '%')} tone={rev == null ? 'neutral' : rev > 0 ? 'good' : 'bad'}
            note={rev == null ? 'not available' : rev >= 8 ? 'expanding top line (recent qtr)' : rev > 0 ? 'modest growth (recent qtr)' : 'contracting (recent qtr)'} />
          <MetricRow label="EPS growth" value={num1(eps, '%')} tone={eps == null ? 'neutral' : eps > 0 ? 'good' : 'bad'}
            note={eps == null ? 'not available' : eps >= 10 ? 'earnings accelerating (recent qtr)' : eps > 0 ? 'earnings rising (recent qtr)' : 'earnings falling (recent qtr)'} />
        </Group>

        <Group title="Trend & risk">
          <MetricRow label="12m momentum" value={num1(mom, '%')} tone={mom == null ? 'neutral' : mom > 0 ? 'good' : 'bad'}
            note={mom == null ? 'not available' : mom >= 20 ? 'strong sustained uptrend' : mom > 0 ? 'positive trend' : 'downtrend'} />
          <MetricRow label="60d volatility" value={num1(vol, '%')} tone={vol == null ? 'neutral' : vol <= 25 ? 'good' : 'neutral'}
            note={vol == null ? 'not available' : vol <= 25 ? 'stable — lower drawdown risk' : vol <= 50 ? 'moderate volatility' : 'high volatility'} />
          <MetricRow label="Reward / risk" value={rr == null ? '—' : num2(rr, 'x')} tone={rr == null ? 'neutral' : rr >= 2 ? 'good' : 'neutral'}
            note={rr == null ? 'not available' : rr >= 2 ? 'meets 2x minimum' : 'below 2x target'} />
        </Group>
      </div>
    </div>
  )
}

export default function SymbolDetail() {
  const { ticker } = useParams()
  const [params] = useSearchParams()
  const side = params.get('side') || 'long'
  const [analysis, setAnalysis] = useState(null)
  const [pick, setPick] = useState(null)
  const [quantSignal, setQuantSignal] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true); setError(null); setAnalysis(null); setPick(null); setQuantSignal(null)
      // Each source is INDEPENDENT and fail-isolated: a failure in /api/analyze
      // must never prevent the STB pick card + Why-to-buy evidence from rendering.
      // We only surface the error banner if EVERY source fails.
      let analyzeOk = false, pickOk = false, quantOk = false
      let analyzeErr = null

      // 1) Standard analysis (non-blocking) — quant-picks/STB never gated on this.
      try {
        const aRes = await fetch(`/api/analyze/${ticker}`)
        if (aRes.ok) {
          const aJson = await aRes.json()
          if (!cancelled) { setAnalysis(aJson); analyzeOk = true }
        } else {
          let m = `HTTP ${aRes.status}`
          try { const e = await aRes.json(); m = e.detail || m } catch {}
          analyzeErr = m
        }
      } catch (e) { analyzeErr = e.message }

      // 2) STB pick + Why-to-buy fundamentals (independent of analyze).
      try {
        const sRes = await fetch(`/api/symbols-to-buy`)
        if (sRes.ok) {
          const sJson = await sRes.json()
          if (!cancelled && sJson.ok) {
            const list = side === 'short' ? (sJson.short_picks || []) : (sJson.long_picks || [])
            const found = list.find(p => p.ticker === ticker)
              || (sJson.long_picks || []).find(p => p.ticker === ticker)
              || (sJson.short_picks || []).find(p => p.ticker === ticker)
            if (found) { setPick(found); pickOk = true }
          }
        }
      } catch { /* pick stays null — page still renders analysis + quant */ }

      // 3) Quant HF signal (cache-only, no rate-limit risk), independent.
      try {
        const qRes = await fetch(`/api/quant-picks`)
        let qp = null
        if (!cancelled && qRes.ok) {
          const qJson = await qRes.json()
          const allPicks = [...(qJson.long_picks || []), ...(qJson.short_picks || [])]
          qp = allPicks.find(p => p.ticker === ticker) || null
          if (qp) { setQuantSignal({
            direction: qp.direction,
            confidence: qp.confidence,
            composite_score: qp.composite_score,
            factors: qp.factors,
          }); quantOk = true }
        }
        // Fallback: ticker not in the pre-scanned pick universe. Run the
        // per-ticker quant scorer so a QHF signal ALWAYS shows for any symbol.
        if (!cancelled && !qp) {
          const wRes = await fetch(`/api/watchlist-analysis/${ticker}`)
          if (wRes.ok) {
            const wJson = await wRes.json()
            if (!cancelled && wJson && wJson.analyzed && wJson.direction) {
              setQuantSignal({
                direction: wJson.direction,
                confidence: wJson.confidence,
                composite_score: wJson.composite_score,
                factors: wJson.factors,
              }); quantOk = true
            }
          }
        }
      } catch { /* quant card stays hidden if scorer itself fails */ }

      // Only show the error banner when NOTHING rendered.
      if (!cancelled) {
        if (!analyzeOk && !pickOk && !quantOk) setError(analyzeErr || 'Unable to load analysis.')
        setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [ticker, side])

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
      <div className="mb-4">
        <Link to="/symbols-to-buy" className="text-sm text-neutral-400 hover:text-white inline-flex items-center gap-1">
          <span>←</span>
          <span>Back to Symbols to Buy</span>
        </Link>
      </div>

      {loading && (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Loading analysis for {ticker}…
        </div>
      )}

      {error && !loading && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 mb-6">
          {error}
        </div>
      )}

      {pick && !loading && <PickSummaryCard pick={pick} side={side} />}

      {/* Why-to-buy evidence — detail page only; keeps the STB list uncrowded */}
      {pick && !loading && side === 'long' && <WhyToBuyPanel pick={pick} />}

      {!pick && analysis && !loading && (
        <div className="bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-4 py-3 text-sm text-neutral-300 mb-6">
          <span className="font-bold text-white">{ticker}</span> is not currently in the Symbols-to-Buy
          selection set — displaying standard analysis only. The screening engine refreshes every ~30 minutes.
        </div>
      )}

      {/* Quant HF 22-factor signal card — shown when ticker appears in the quant engine queue */}
      {quantSignal && !loading && <QuantHFCard sig={quantSignal} />}

      {analysis && !loading && (
        <div className="mt-4">
          <AnalysisDashboard data={analysis} />
        </div>
      )}
    </div>
  )
}
