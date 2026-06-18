import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'

// SYMBOLS TO BUY — Fundamental long-term picks (6-12 week holds).
// Powered by generate_fundamental_picks() — separate from the paper trading system.
// Scores on: revenue/earnings growth, ROE, PEG ratio, 12-month momentum.

function fmt(v, suffix = '', dec = 1) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(dec) + suffix
}
function fmtPos(v, suffix = '', dec = 1) {
  if (v == null || isNaN(v)) return '—'
  return Number(v).toFixed(dec) + suffix
}
function fmtPrice(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Number(v).toFixed(2)
}
function fmtMoney(v) {
  if (v == null || isNaN(v) || !isFinite(v)) return '—'
  return '$' + Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}
function confColor(c) {
  if (c == null) return 'text-neutral-500'
  if (c >= 75) return 'text-emerald-400'
  if (c >= 65) return 'text-amber-300'
  return 'text-neutral-400'
}
function growthColor(v) {
  if (v == null || isNaN(v)) return 'text-neutral-500'
  if (v >= 20) return 'text-emerald-400'
  if (v >= 5) return 'text-emerald-300/70'
  if (v < 0) return 'text-rose-400'
  return 'text-neutral-400'
}
function scoreColor(s) {
  if (s == null) return 'text-neutral-500'
  if (s >= 70) return 'text-emerald-400'
  if (s >= 55) return 'text-amber-300'
  return 'text-neutral-400'
}

// Allocation: 5% per pick, top 10 longs
const ALLOC_PCT = 0.05
const MAX_FUNDED = 10

function calcAlloc(cash, price, rank) {
  if (!cash || cash <= 0 || rank > MAX_FUNDED) return { dollars: 0, shares: 0, fund: false }
  const dollars = cash * ALLOC_PCT
  if (!price || price <= 0) return { dollars: Math.round(dollars), shares: null, fund: true }
  const shares = Math.floor(dollars / price)
  return { dollars: Math.round(shares * price), shares, fund: shares > 0 }
}

function PickRow({ p, rank, cash }) {
  const alloc = calcAlloc(cash, p.entry_price, rank)
  const rrColor = !p.reward_risk_ratio ? 'text-neutral-500'
    : p.reward_risk_ratio >= 3 ? 'text-emerald-400'
    : p.reward_risk_ratio >= 2 ? 'text-amber-300'
    : 'text-rose-400'

  return (
    <Link
      to={`/symbol/${p.ticker}?side=long`}
      className="grid grid-cols-12 gap-1 px-4 py-3 border-b border-neutral-800/40 hover:bg-neutral-800/30 transition-colors items-center text-sm group"
    >
      {/* #  Ticker  Sector */}
      <div className="col-span-2 flex items-center gap-2">
        <span className="text-neutral-600 text-xs font-mono w-5 text-right">#{rank}</span>
        <span className="font-bold text-white text-base group-hover:text-blue-300">{p.ticker}</span>
      </div>
      <div className="col-span-1 text-neutral-400 text-xs truncate">{p.sector || '—'}</div>

      {/* Entry */}
      <div className="col-span-1 text-right font-mono text-neutral-200 text-xs">{fmtPrice(p.entry_price)}</div>

      {/* Fund Score */}
      <div className={`col-span-1 text-right font-mono text-xs font-bold ${scoreColor(p.fundamental_score)}`}>
        {p.fundamental_score != null ? p.fundamental_score.toFixed(0) : '—'}
      </div>

      {/* Rev Growth */}
      <div className={`col-span-1 text-right font-mono text-xs ${growthColor(p.revenue_growth_pct)}`}>
        {fmt(p.revenue_growth_pct, '%', 0)}
      </div>

      {/* Confidence */}
      <div className={`col-span-1 text-right font-mono text-xs font-bold ${confColor(p.confidence)}`}>
        {p.confidence != null ? p.confidence + '%' : '—'}
      </div>

      {/* ROE */}
      <div className={`col-span-1 text-right font-mono text-xs ${growthColor(p.roe_pct)}`}>
        {fmtPos(p.roe_pct, '%', 0)}
      </div>

      {/* PEG */}
      <div className={`col-span-1 text-right font-mono text-xs ${
        p.peg_ratio == null ? 'text-neutral-500'
        : p.peg_ratio < 1 ? 'text-emerald-400'
        : p.peg_ratio < 2 ? 'text-amber-300'
        : 'text-rose-400'
      }`}>
        {fmtPos(p.peg_ratio, 'x', 1)}
      </div>

      {/* Stop / Target */}
      <div className="col-span-1 text-right font-mono text-xs">
        <div className="text-rose-300/80">{fmtPrice(p.stop_loss)}</div>
        <div className="text-emerald-300/80">{fmtPrice(p.target_price)}</div>
      </div>

      {/* R:R */}
      <div className={`col-span-1 text-right font-mono text-xs ${rrColor}`}>
        {p.reward_risk_ratio != null ? p.reward_risk_ratio.toFixed(1) + 'x' : '—'}
      </div>

      {/* Allocation */}
      <div className="col-span-1 text-right text-xs">
        {!cash || cash <= 0 ? (
          <span className="text-neutral-600">—</span>
        ) : !alloc.fund ? (
          <span className="text-neutral-600 text-[10px]">skip</span>
        ) : (
          <div>
            <div className="text-amber-200 font-bold">{alloc.shares} sh</div>
            <div className="text-[10px] text-neutral-500">{fmtMoney(alloc.dollars)}</div>
          </div>
        )}
      </div>
    </Link>
  )
}

function PickTable({ picks, cash }) {
  if (!picks || !picks.length) {
    return (
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-8 text-center text-neutral-500">
        No picks available yet — fundamental scan warming up. Check back in 2 minutes.
      </div>
    )
  }
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 flex items-center justify-between bg-blue-950/20">
        <div>
          <h2 className="text-lg font-bold text-blue-300">Long-Term Fundamental Picks</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            {picks.length} picks · Ranked by fundamental score · Click row for details
          </p>
        </div>
        <span className="text-[10px] font-bold uppercase tracking-wider text-blue-400 border border-blue-800/40 px-2 py-1 rounded">
          6-12 Week Hold
        </span>
      </div>

      {/* Header */}
      <div className="grid grid-cols-12 gap-1 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
        <div className="col-span-2">Ticker</div>
        <div className="col-span-1">Sector</div>
        <div className="col-span-1 text-right">Entry</div>
        <div className="col-span-1 text-right">Score</div>
        <div className="col-span-1 text-right">Rev Gr</div>
        <div className="col-span-1 text-right">Conf</div>
        <div className="col-span-1 text-right">ROE</div>
        <div className="col-span-1 text-right">PEG</div>
        <div className="col-span-1 text-right">Stop/Tgt</div>
        <div className="col-span-1 text-right">R:R</div>
        <div className="col-span-1 text-right">Allocate</div>
      </div>

      <div className="max-h-[800px] overflow-y-auto">
        {picks.map((p, i) => <PickRow key={p.ticker} p={p} rank={i + 1} cash={cash} />)}
      </div>
    </div>
  )
}

function CashInput({ value, onChange }) {
  return (
    <div className="flex items-center gap-3">
      <label className="text-xs uppercase tracking-wider font-bold text-neutral-400">Your Balance</label>
      <div className="relative">
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500 font-mono">$</span>
        <input
          type="number" inputMode="numeric" min="0" step="100"
          value={value || ''}
          placeholder="0"
          onChange={e => {
            const v = e.target.value
            if (v === '') return onChange(null)
            const n = Number(v)
            onChange(isNaN(n) || n < 0 ? null : n)
          }}
          className="pl-7 pr-3 py-2 w-44 bg-neutral-900 border border-neutral-700 rounded-lg text-sm text-white font-mono placeholder-neutral-600 focus:border-blue-500 focus:outline-none"
        />
      </div>
      {value > 0 && (
        <div className="text-[11px] text-neutral-500">
          {(ALLOC_PCT * 100).toFixed(0)}% per pick · top {MAX_FUNDED} positions
          · total deploy ≈ {fmtMoney(value * ALLOC_PCT * Math.min(MAX_FUNDED, 10))}
        </div>
      )}
    </div>
  )
}

export default function SymbolsToBuy() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [cash, setCash] = useState(() => {
    try {
      const stored = localStorage.getItem('stb_cash')
      const n = stored == null ? null : Number(stored)
      return Number.isFinite(n) && n > 0 ? n : null
    } catch { return null }
  })

  useEffect(() => {
    try {
      if (cash == null) localStorage.removeItem('stb_cash')
      else localStorage.setItem('stb_cash', String(cash))
    } catch {}
  }, [cash])

  async function load(force = false) {
    if (force) setRefreshing(true)
    else setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/symbols-to-buy${force ? '?force_refresh=true' : ''}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const j = await res.json()
      if (!j.ok && j.reason && j.reason !== 'warming_up') throw new Error(j.message || j.reason)
      setData(j)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => { load(false) }, [])

  const cacheMinutes = data?.cache_age_seconds != null
    ? Math.round(data.cache_age_seconds / 60)
    : null

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <div className="flex items-end justify-between mb-2">
          <div>
            <h1 className="text-3xl font-black text-white tracking-tight">Stocks to Buy</h1>
            <p className="text-sm text-neutral-400 mt-1">
              Fundamentals-driven · 6-12 week holds · Revenue growth + ROE + valuation
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

        {/* Cash input */}
        <div className="mt-4 bg-neutral-900/40 border border-neutral-800/60 rounded-xl px-4 py-3">
          <CashInput value={cash} onChange={setCash} />
        </div>

        {/* Meta bar */}
        {data && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-neutral-500 mt-3">
            <span className="px-2 py-1 bg-blue-950/30 rounded border border-blue-900/40 text-blue-300 font-bold">
              FUNDAMENTAL ENGINE
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Picks: <span className="text-white font-bold">{data.long_count ?? 0}</span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Universe: <span className="text-white font-bold">{data.universe_total ?? '—'}</span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Priced: <span className="text-white font-bold">{data.tickers_attempted ?? data.universe_scanned ?? '—'}</span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Scored: <span className="text-white font-bold">{data.candidates_scored ?? '—'}</span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800">
              Cache age: <span className={data.cache_is_restoring ? 'text-yellow-400 font-bold' : 'text-white font-bold'}>
                {data.cache_is_restoring ? 'Warming up…' : cacheMinutes != null ? `${cacheMinutes} min` : '—'}
              </span>
            </span>
            <span className="px-2 py-1 bg-neutral-900 rounded border border-neutral-800 text-neutral-500">
              Refreshes every 6h
            </span>
          </div>
        )}
      </div>

      {loading && (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Loading fundamental picks…
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
              <span className="font-bold mr-2">Strategy:</span>{data.guidance}
            </div>
          )}

          {/* Score legend */}
          <div className="flex flex-wrap gap-4 text-xs text-neutral-500">
            <span><span className="text-emerald-400 font-bold">Score 70+</span> = Strong buy</span>
            <span><span className="text-amber-300 font-bold">Score 55-70</span> = Good quality</span>
            <span><span className="text-emerald-400 font-bold">Conf 75%+</span> = High conviction · min 70% required</span>
            <span><span className="font-bold text-neutral-400">Rev Gr</span> = YoY revenue growth</span>
            <span><span className="font-bold text-neutral-400">ROE</span> = Return on equity</span>
            <span><span className="font-bold text-neutral-400">PEG</span> = Price/earnings-to-growth ({"<"}1.5 = good)</span>
            <span><span className="font-bold text-neutral-400">R:R</span> = Reward:risk ratio (3x+ = ideal)</span>
          </div>

          <PickTable picks={data.long_picks} cash={cash} />
        </div>
      )}
    </div>
  )
}
