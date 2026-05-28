import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'

// SYMBOLS TO BUY — manual swing-trading reference page.
// Shows top 25 LONG + top 25 SHORT picks from /api/symbols-to-buy.
// Clicking a row navigates to /symbol/:ticker for the why-to-buy detail.

function formatPct(v) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}
function formatPrice(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Number(v).toFixed(2)
}
function confidenceColor(c) {
  if (c == null) return 'text-neutral-500'
  if (c >= 70) return 'text-emerald-400'
  if (c >= 55) return 'text-amber-300'
  return 'text-neutral-400'
}

function PickRow({ p, side }) {
  const isLong = side === 'long'
  const dirColor = isLong ? 'text-emerald-400' : 'text-rose-400'
  const dirBg = isLong ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-rose-500/10 border-rose-500/30'
  return (
    <Link
      to={`/symbol/${p.ticker}?side=${side}`}
      className="grid grid-cols-12 gap-2 px-4 py-3 border-b border-neutral-800/40 hover:bg-neutral-800/30 transition-colors items-center text-sm group"
    >
      <div className="col-span-2 flex items-center gap-2">
        <span className="font-bold text-white text-base group-hover:text-blue-300">{p.ticker}</span>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${dirBg} ${dirColor}`}>
          {isLong ? 'LONG' : 'SHORT'}
        </span>
      </div>
      <div className="col-span-2 text-neutral-400 text-xs truncate">{p.sector || 'Unknown'}</div>
      <div className="col-span-1 text-right font-mono text-neutral-200">{formatPrice(p.entry_price)}</div>
      <div className={`col-span-1 text-right font-mono ${confidenceColor(p.confidence)}`}>
        {p.confidence != null ? Math.round(p.confidence) + '%' : '—'}
      </div>
      <div className="col-span-1 text-right font-mono text-neutral-300">{p.composite_score?.toFixed(2) ?? '—'}</div>
      <div className="col-span-1 text-right font-mono text-rose-300/80 text-xs">
        {formatPrice(p.stop_loss)}
        <div className="text-[10px] text-neutral-500">{formatPct(p.stop_distance_pct && -p.stop_distance_pct)}</div>
      </div>
      <div className="col-span-1 text-right font-mono text-emerald-300/80 text-xs">
        {formatPrice(p.target_price)}
        <div className="text-[10px] text-neutral-500">{formatPct(p.target_distance_pct)}</div>
      </div>
      <div className="col-span-1 text-right font-mono text-blue-300 text-xs">
        {p.reward_risk_ratio != null ? p.reward_risk_ratio.toFixed(2) + 'x' : '—'}
      </div>
      <div className="col-span-2 text-right text-neutral-500 text-[11px] truncate">
        {(p.reasons && p.reasons[0]) || '—'}
      </div>
    </Link>
  )
}

function PickTable({ picks, side, title }) {
  if (!picks || !picks.length) {
    return (
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-8 text-center text-neutral-500">
        No {side} picks available right now. Picks regenerate every ~30 min.
      </div>
    )
  }
  const isLong = side === 'long'
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className={`px-4 py-3 border-b border-neutral-800/60 flex items-center justify-between ${isLong ? 'bg-emerald-950/20' : 'bg-rose-950/20'}`}>
        <div>
          <h2 className={`text-lg font-bold ${isLong ? 'text-emerald-300' : 'text-rose-300'}`}>{title}</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            {picks.length} pick{picks.length !== 1 ? 's' : ''} · Click any row for full analysis + why to buy
          </p>
        </div>
        <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-500">
          Hold 3-5 days min
        </span>
      </div>
      <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
        <div className="col-span-2">Ticker</div>
        <div className="col-span-2">Sector</div>
        <div className="col-span-1 text-right">Entry</div>
        <div className="col-span-1 text-right">Conf</div>
        <div className="col-span-1 text-right">Score</div>
        <div className="col-span-1 text-right">Stop</div>
        <div className="col-span-1 text-right">Target</div>
        <div className="col-span-1 text-right">R/R</div>
        <div className="col-span-2 text-right">Reason</div>
      </div>
      <div className="max-h-[700px] overflow-y-auto">
        {picks.map((p) => <PickRow key={p.ticker} p={p} side={side} />)}
      </div>
    </div>
  )
}

export default function SymbolsToBuy() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  async function load(force = false) {
    if (force) setRefreshing(true)
    else setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/symbols-to-buy${force ? '?force_refresh=true' : ''}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const j = await res.json()
      if (!j.ok && j.reason) throw new Error(j.message || j.reason)
      setData(j)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => { load(false) }, [])

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <div className="flex items-end justify-between mb-2">
          <div>
            <h1 className="text-3xl font-black text-white tracking-tight">Symbols to Buy</h1>
            <p className="text-sm text-neutral-400 mt-1">
              Manual swing-trading queue · 3-5 day minimum holds · No day trading
            </p>
          </div>
          <button
            onClick={() => load(true)}
            disabled={refreshing}
            className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 rounded-lg text-sm font-medium text-neutral-200 transition-colors"
          >
            {refreshing ? 'Refreshing…' : 'Force refresh'}
          </button>
        </div>
        {data && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-neutral-500 mt-3">
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Regime: <span className="text-white font-bold">{data.regime}</span>
              {data.regime_confidence != null ? ` (${Math.round(data.regime_confidence)}%)` : ''}
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Universe: <span className="text-white font-bold">{data.universe_size}</span> tickers
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Cache age: <span className="text-white font-bold">
                {data.cache_age_seconds != null ? Math.round(data.cache_age_seconds / 60) + ' min' : '—'}
              </span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Longs: <span className="text-emerald-300 font-bold">{data.long_count}</span> · Shorts: <span className="text-rose-300 font-bold">{data.short_count}</span>
            </span>
          </div>
        )}
      </div>

      {loading && (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Loading picks…
        </div>
      )}

      {error && !loading && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 mb-6">
          {error}
        </div>
      )}

      {data && !loading && (
        <div className="space-y-6">
          {data.guidance && (
            <div className="bg-blue-950/20 border border-blue-800/40 rounded-lg px-4 py-3 text-sm text-blue-200">
              <span className="font-bold mr-2">Guidance:</span>{data.guidance}
            </div>
          )}
          <PickTable picks={data.long_picks} side="long" title="LONG Queue" />
          <PickTable picks={data.short_picks} side="short" title="SHORT Queue" />
        </div>
      )}
    </div>
  )
}
