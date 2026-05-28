import { useState, useEffect } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import AnalysisDashboard from '../components/AnalysisDashboard'

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

function PickSummaryCard({ pick, side }) {
  if (!pick) return null
  const isLong = side === 'long'
  const dirColor = isLong ? 'text-emerald-300' : 'text-rose-300'
  const dirBg = isLong ? 'from-emerald-950/30 to-emerald-900/10 border-emerald-700/40' : 'from-rose-950/30 to-rose-900/10 border-rose-700/40'

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
        <div className="bg-neutral-900/60 border border-blue-800/40 rounded-lg px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-blue-400">Reward / Risk</div>
          <div className="text-lg font-bold text-blue-200 font-mono">
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

      <div className="mt-5 px-3 py-2 bg-amber-950/20 border border-amber-800/30 rounded text-xs text-amber-200">
        <span className="font-bold">No day trading.</span> Hold this position 3-5 days minimum. Best
        winners often run 4-8 weeks. Always honor the stop.
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true); setError(null); setAnalysis(null); setPick(null)
      try {
        const [aRes, sRes] = await Promise.all([
          fetch(`/api/analyze/${ticker}`),
          fetch(`/api/symbols-to-buy`),
        ])
        if (!aRes.ok) {
          let m = `HTTP ${aRes.status}`
          try { const e = await aRes.json(); m = e.detail || m } catch {}
          throw new Error(m)
        }
        const aJson = await aRes.json()
        if (cancelled) return
        setAnalysis(aJson)

        if (sRes.ok) {
          const sJson = await sRes.json()
          if (!cancelled && sJson.ok) {
            const list = side === 'short' ? (sJson.short_picks || []) : (sJson.long_picks || [])
            const found = list.find(p => p.ticker === ticker)
              || (sJson.long_picks || []).find(p => p.ticker === ticker)
              || (sJson.short_picks || []).find(p => p.ticker === ticker)
            if (found) setPick(found)
          }
        }
      } catch (e) {
        if (!cancelled) setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
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

      {!pick && analysis && !loading && (
        <div className="bg-amber-950/20 border border-amber-800/30 rounded-lg px-4 py-3 text-sm text-amber-200 mb-6">
          <span className="font-bold">{ticker}</span> is not on the current Symbols-to-Buy queue —
          showing the standard analysis only. The picks engine refreshes every ~30 min.
        </div>
      )}

      {analysis && !loading && (
        <div className="mt-4">
          <AnalysisDashboard data={analysis} />
        </div>
      )}
    </div>
  )
}
