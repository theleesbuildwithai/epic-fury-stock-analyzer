import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'

// SYMBOLS TO BUY — manual swing-trading reference page.
// Shows top 25 LONG + top 25 SHORT picks from /api/symbols-to-buy.
// Clicking a row navigates to /symbol/:ticker for the why-to-buy detail.
//
// 2026-05-29 update:
//   - Picks now ranked best→worst by CONFIDENCE (calibrated probability)
//   - Added Cash Allocator: enter your account balance, see exactly how
//     many shares + $ to deploy per pick under a fixed allocation policy
//     (default 1.5% per long, 1.0% per short; capped at 10 longs / 5 shorts).
//   - Input placeholder shows '0' and clears the moment you focus it.

function formatPct(v) {
  if (v == null || isNaN(v)) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}
function formatPrice(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Number(v).toFixed(2)
}
function formatMoney(v) {
  if (v == null || isNaN(v) || !isFinite(v)) return '—'
  return '$' + Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}
function confidenceColor(c) {
  if (c == null) return 'text-neutral-500'
  if (c >= 70) return 'text-emerald-400'
  if (c >= 55) return 'text-amber-300'
  return 'text-neutral-400'
}

// Allocation policy (per-pick % of total balance, capped to top N per side)
const LONG_PCT_PER_PICK = 0.015   // 1.5% per long
const SHORT_PCT_PER_PICK = 0.010  // 1.0% per short (smaller — shorts asymmetric)
const MAX_LONGS_TO_FUND = 10
const MAX_SHORTS_TO_FUND = 5

function calcAlloc(cash, pick, side, rank) {
  const isLong = side === 'long'
  const maxRank = isLong ? MAX_LONGS_TO_FUND : MAX_SHORTS_TO_FUND
  if (!cash || cash <= 0) return { dollars: null, shares: null, fund: false }
  if ((pick.rank || rank) > maxRank) return { dollars: 0, shares: 0, fund: false }
  const pct = isLong ? LONG_PCT_PER_PICK : SHORT_PCT_PER_PICK
  const dollars = cash * pct
  const price = pick.entry_price
  if (!price || price <= 0) return { dollars: Math.round(dollars), shares: null, fund: true }
  const shares = Math.floor(dollars / price)
  return { dollars: Math.round(shares * price), shares, fund: shares > 0 }
}

function PickRow({ p, side, rank, cash }) {
  const isLong = side === 'long'
  const dirColor = isLong ? 'text-emerald-400' : 'text-rose-400'
  const dirBg = isLong ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-rose-500/10 border-rose-500/30'
  const alloc = calcAlloc(cash, p, side, rank)
  return (
    <Link
      to={`/symbol/${p.ticker}?side=${side}`}
      className="grid grid-cols-12 gap-2 px-4 py-3 border-b border-neutral-800/40 hover:bg-neutral-800/30 transition-colors items-center text-sm group"
    >
      <div className="col-span-2 flex items-center gap-2">
        <span className="text-neutral-600 text-xs font-mono w-5 text-right">#{p.rank || rank}</span>
        <span className="font-bold text-white text-base group-hover:text-blue-300">{p.ticker}</span>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${dirBg} ${dirColor}`}>
          {isLong ? 'LONG' : 'SHORT'}
        </span>
      </div>
      <div className="col-span-1 text-neutral-400 text-xs truncate">{p.sector || 'Unknown'}</div>
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
      <div className="col-span-1 text-right font-mono text-xs">
        {!cash || cash <= 0 ? (
          <span className="text-neutral-600">—</span>
        ) : !alloc.fund ? (
          <span className="text-neutral-600 text-[11px]">skip</span>
        ) : (
          <div>
            <div className="text-amber-200 font-bold">{alloc.shares} sh</div>
            <div className="text-[10px] text-neutral-500">{formatMoney(alloc.dollars)}</div>
          </div>
        )}
      </div>
      <div className="col-span-1 text-right text-neutral-500 text-[11px] truncate">
        {(p.reasons && p.reasons[0]) || '—'}
      </div>
      <div className={`col-span-1 text-right text-[10px] font-bold ${p.hold_class === 'position' ? 'text-blue-400' : 'text-neutral-500'}`}>
        {p.hold_class === 'position' ? 'POSITION' : p.hold_class === 'intraday' ? 'INTRADAY' : 'SWING'}
      </div>
    </Link>
  )
}

function PickTable({ picks, side, title, cash }) {
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
            {picks.length} pick{picks.length !== 1 ? 's' : ''} · Sorted by confidence high→low · Click any row for full why-to-buy
          </p>
        </div>
        <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-500">
          {picks[0]?.hold_class === 'position' ? 'Position trade (30-60d)' : 'Swing trade (5-14d)'}
        </span>
      </div>
      <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
        <div className="col-span-2">Ticker</div>
        <div className="col-span-1">Sector</div>
        <div className="col-span-1 text-right">Entry</div>
        <div className="col-span-1 text-right">Conf</div>
        <div className="col-span-1 text-right">Score</div>
        <div className="col-span-1 text-right">Stop</div>
        <div className="col-span-1 text-right">Target</div>
        <div className="col-span-1 text-right">R/R</div>
        <div className="col-span-1 text-right">Allocate</div>
        <div className="col-span-1 text-right">Reason</div>
        <div className="col-span-1 text-right">Hold</div>
      </div>
      <div className="max-h-[700px] overflow-y-auto">
        {picks.map((p, i) => <PickRow key={p.ticker} p={p} side={side} rank={i + 1} cash={cash} />)}
      </div>
    </div>
  )
}

// Cash balance input — placeholder shows '0', clears on focus.
function CashInput({ value, onChange }) {
  const [focused, setFocused] = useState(false)
  const display = value === 0 || value === null || value === undefined || value === ''
    ? (focused ? '' : '')
    : String(value)
  return (
    <div className="flex items-center gap-3">
      <label className="text-xs uppercase tracking-wider font-bold text-neutral-400">
        Your Cash Balance
      </label>
      <div className="relative">
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500 font-mono">$</span>
        <input
          type="number"
          inputMode="numeric"
          min="0"
          step="100"
          value={display}
          placeholder="0"
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onChange={(e) => {
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
          Auto-allocates {(LONG_PCT_PER_PICK * 100).toFixed(1)}% per long · {(SHORT_PCT_PER_PICK * 100).toFixed(1)}% per short
          · top {MAX_LONGS_TO_FUND}L / {MAX_SHORTS_TO_FUND}S
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
  // Cash balance persists in localStorage so user doesn't retype
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

  // Totals across all funded picks
  const totals = (() => {
    if (!data || !cash || cash <= 0) return null
    const fundedLongs = (data.long_picks || []).slice(0, MAX_LONGS_TO_FUND)
    const fundedShorts = (data.short_picks || []).slice(0, MAX_SHORTS_TO_FUND)
    const longDollars = fundedLongs.reduce((s, p, i) => {
      const a = calcAlloc(cash, p, 'long', i + 1)
      return s + (a.dollars || 0)
    }, 0)
    const shortDollars = fundedShorts.reduce((s, p, i) => {
      const a = calcAlloc(cash, p, 'short', i + 1)
      return s + (a.dollars || 0)
    }, 0)
    const gross = longDollars + shortDollars
    return { longDollars, shortDollars, gross, grossPct: (gross / cash) * 100 }
  })()

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

        {/* Cash balance input (always visible) */}
        <div className="mt-4 bg-neutral-900/40 border border-neutral-800/60 rounded-xl px-4 py-3 flex flex-wrap items-center gap-4">
          <CashInput value={cash} onChange={setCash} />
          {totals && (
            <div className="flex flex-wrap items-center gap-3 text-xs ml-auto">
              <span className="px-2 py-1 bg-emerald-950/30 border border-emerald-800/40 rounded text-emerald-300">
                Long deploy: <span className="font-bold">{formatMoney(totals.longDollars)}</span>
              </span>
              <span className="px-2 py-1 bg-rose-950/30 border border-rose-800/40 rounded text-rose-300">
                Short deploy: <span className="font-bold">{formatMoney(totals.shortDollars)}</span>
              </span>
              <span className="px-2 py-1 bg-blue-950/30 border border-blue-800/40 rounded text-blue-300">
                Gross: <span className="font-bold">{formatMoney(totals.gross)}</span>
                <span className="ml-1 text-neutral-500">({totals.grossPct.toFixed(1)}%)</span>
              </span>
            </div>
          )}
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
          <PickTable picks={data.long_picks} side="long" title="LONG Queue" cash={cash} />
          <PickTable picks={data.short_picks} side="short" title="SHORT Queue" cash={cash} />
        </div>
      )}
    </div>
  )
}
