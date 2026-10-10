import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { addPickToWatchlist } from '../lib/watchlist'

// SYMBOLS TO BUY — Fundamental long-term picks (6-12 week holds).
// Powered by generate_fundamental_picks() — separate from the paper trading system.
// Scores on: revenue/earnings growth, ROE, PEG ratio, 12-month momentum.
// Hedge fund overlay: Bridgewater Pure Alpha regime, AQR quality, vol cap, correlation filter.

// ─── Format helpers ───────────────────────────────────────────────────────────
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
  if (c >= 65) return 'text-neutral-300'
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
  if (s >= 55) return 'text-neutral-300'
  return 'text-neutral-400'
}

// ─── Allocation ───────────────────────────────────────────────────────────────
const ALLOC_PCT = 0.05
const MAX_FUNDED = 10

// Allocation is scaled by the technical entry grade so a poor entry is sized
// down automatically rather than bought at full size. Missing timing data
// means full size — absent data must never silently shrink a position.
function calcAlloc(cash, price, rank, sizeFactor) {
  if (!cash || cash <= 0 || rank > MAX_FUNDED) return { dollars: 0, shares: 0, fund: false, sf: 1 }
  const sf = (typeof sizeFactor === 'number' && sizeFactor >= 0 && sizeFactor <= 1) ? sizeFactor : 1
  if (sf === 0) return { dollars: 0, shares: 0, fund: false, sf: 0 }
  const dollars = cash * ALLOC_PCT * sf
  if (!price || price <= 0) return { dollars: Math.round(dollars), shares: null, fund: true, sf }
  const shares = Math.floor(dollars / price)
  return { dollars: Math.round(shares * price), shares, fund: shares > 0, sf }
}

// ─── Regime constants (mirrors backend) ───────────────────────────────────────
const RISK_ON_SECTORS  = ['Technology', 'Industrials', 'Financial Services', 'Consumer Cyclical']
const RISK_OFF_SECTORS = ['Healthcare', 'Consumer Defensive', 'Utilities', 'Real Estate']

// ─── Regime Banner ────────────────────────────────────────────────────────────
function RegimeBanner({ regime }) {
  // Always render something so users know the engine is running
  if (!regime || regime === 'NEUTRAL') {
    return (
      <div className="flex flex-wrap items-center gap-3 px-4 py-2.5 bg-neutral-900/40 border border-neutral-800/60 rounded-lg text-xs">
        <span className="w-2 h-2 rounded-full bg-neutral-500 shrink-0" />
        <span className="font-bold text-neutral-300 uppercase tracking-wider">Bridgewater: NEUTRAL</span>
        <span className="text-neutral-600 hidden sm:inline">·</span>
        <span className="text-neutral-500">No strong sector bias — all sectors scored equally</span>
      </div>
    )
  }

  const isRiskOn = regime === 'RISK_ON'
  const favored  = isRiskOn ? RISK_ON_SECTORS  : RISK_OFF_SECTORS
  const avoided  = isRiskOn ? RISK_OFF_SECTORS : RISK_ON_SECTORS

  return (
    <div className={`flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2.5 border rounded-lg text-xs ${
      isRiskOn
        ? 'bg-emerald-950/30 border-emerald-800/40'
        : 'bg-neutral-900/50 border-neutral-700/50'
    }`}>
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full shrink-0 animate-pulse ${isRiskOn ? 'bg-emerald-400' : 'bg-neutral-300'}`} />
        <span className={`font-bold uppercase tracking-wider ${isRiskOn ? 'text-emerald-300' : 'text-neutral-200'}`}>
          Bridgewater Pure Alpha: {isRiskOn ? 'RISK-ON' : 'RISK-OFF'}
        </span>
      </div>
      <div className="text-neutral-400">
        <span className={`font-semibold mr-1 ${isRiskOn ? 'text-emerald-300' : 'text-neutral-200'}`}>▲ Favored:</span>
        {favored.join(' · ')}
      </div>
      <div className="text-neutral-400">
        <span className="font-semibold text-rose-400 mr-1">▼ Discounted:</span>
        {avoided.join(' · ')}
      </div>
      <div className="text-neutral-600 hidden md:block">+12 pts aligned · −5 pts misaligned</div>
    </div>
  )
}

// ─── Sort helpers ─────────────────────────────────────────────────────────────
// Columns where ascending = better (lower is better)
const COL_ASC_BETTER = new Set(['peg_ratio'])

function handleSortClick(col, sortCol, sortDir, setSortCol, setSortDir) {
  if (sortCol === col) {
    // same column → toggle direction
    setSortDir(d => d === 'desc' ? 'asc' : 'desc')
  } else {
    setSortCol(col)
    // default direction: asc for PEG (lower = better), desc for everything else
    setSortDir(COL_ASC_BETTER.has(col) ? 'asc' : 'desc')
  }
}

// Resolve a possibly-dotted sort key (e.g. "timing.timing_score") safely.
function sortVal(obj, col) {
  if (!col.includes('.')) return obj[col] ?? null
  let cur = obj
  for (const part of col.split('.')) {
    if (cur == null || typeof cur !== 'object') return null
    cur = cur[part]
  }
  return cur ?? null
}

function sortedPicks(picks, col, dir) {
  if (!col) return picks
  return [...picks].sort((a, b) => {
    const va = sortVal(a, col)
    const vb = sortVal(b, col)
    // Nulls always last, regardless of direction
    if (va === null && vb === null) return 0
    if (va === null) return 1
    if (vb === null) return -1
    const cmp = va < vb ? -1 : va > vb ? 1 : 0
    return dir === 'desc' ? -cmp : cmp
  })
}

// Sortable header button
function SortTh({ label, col, sortCol, sortDir, onSort, right = true }) {
  const active = sortCol === col
  return (
    <button
      onClick={() => onSort(col)}
      className={`w-full font-bold uppercase tracking-wider text-[10px] hover:text-white transition-colors select-none ${right ? 'text-right' : 'text-left'} ${active ? 'text-white' : 'text-neutral-500'}`}
    >
      {label}
      {active && <span className="ml-0.5 text-[8px]">{sortDir === 'desc' ? '▼' : '▲'}</span>}
    </button>
  )
}

// ─── Technical analysis (entry timing) ────────────────────────────────────────
// Fundamentals say WHAT to own. These say WHETHER today is a sane price to pay.
// Palette discipline: emerald = bullish, rose = bearish, everything else neutral.
const TIMING_GRADES = ['BUY_NOW', 'ACCUMULATE', 'WAIT', 'AVOID']

const TIMING_META = {
  BUY_NOW:    { label: 'Buy Now',    text: 'text-emerald-400', border: 'border-emerald-600/50', bg: 'bg-emerald-600/10', bar: 'bg-emerald-500' },
  ACCUMULATE: { label: 'Accumulate', text: 'text-white',       border: 'border-neutral-500/60', bg: 'bg-neutral-700/30', bar: 'bg-neutral-300' },
  WAIT:       { label: 'Wait',       text: 'text-neutral-400', border: 'border-neutral-700',    bg: 'bg-neutral-800/40', bar: 'bg-neutral-500' },
  AVOID:      { label: 'Avoid',      text: 'text-rose-400',    border: 'border-rose-600/50',    bg: 'bg-rose-600/10',    bar: 'bg-rose-500' },
  UNRATED:    { label: 'Unrated',    text: 'text-neutral-600', border: 'border-neutral-800',    bg: 'bg-neutral-900/40', bar: 'bg-neutral-700' },
}

const timingMeta = g => TIMING_META[g] || TIMING_META.UNRATED

const EVENT_RISK_TEXT = {
  HIGH: 'text-rose-400', ELEVATED: 'text-rose-300/80',
  MODERATE: 'text-neutral-300', LOW: 'text-neutral-500',
}

const stateText = s => s === 'BULL' ? 'text-emerald-400' : s === 'BEAR' ? 'text-rose-400' : 'text-neutral-400'
const stateBar  = s => s === 'BULL' ? 'bg-emerald-500'   : s === 'BEAR' ? 'bg-rose-500'   : 'bg-neutral-500'

// Compact grade chip for the main row.
function TimingChip({ t }) {
  if (!t) return <span className="text-[9px] text-neutral-700">—</span>
  const m = timingMeta(t.grade)
  return (
    <span className={`inline-block text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border ${m.text} ${m.border} ${m.bg}`}>
      {m.label}{t.timing_score != null ? ` ${Math.round(t.timing_score)}` : ''}
    </span>
  )
}

// Portfolio-level read: how many picks are actually buyable at today's prices.
function TimingSummary({ picks }) {
  const rated = (picks || []).filter(p => p.timing && p.timing.ok)
  if (!rated.length) return null

  const counts = TIMING_GRADES.reduce((acc, g) => {
    acc[g] = rated.filter(p => p.timing.grade === g).length
    return acc
  }, {})
  const avg = rated.reduce((s, p) => s + (p.timing.timing_score || 0), 0) / rated.length
  const risky = rated.filter(p => ['ELEVATED', 'HIGH'].includes(p.timing.event_risk?.level || 'LOW'))
  const extended = rated.filter(p => (p.timing.extension_atr ?? 0) > 2.5)
  const avgRisk = rated
    .map(p => p.timing.near_term_risk_pct)
    .filter(v => v != null)
  const avgRiskPct = avgRisk.length ? avgRisk.reduce((s, v) => s + v, 0) / avgRisk.length : null
  const ready = counts.BUY_NOW + counts.ACCUMULATE
  const readyCol = ready === 0 ? 'text-rose-400'
    : ready >= rated.length / 2 ? 'text-emerald-400' : 'text-white'

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60">
        <h2 className="text-lg font-bold text-white">Technical Analysis — Entry Timing</h2>
        <p className="text-xs text-neutral-500 mt-0.5">
          Fundamentals decide what to own. These 14 signals decide whether today is a defensible price to pay.
          Scored on validated price history only — no additional data requests.
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-neutral-800/60 border-b border-neutral-800/60">
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Buyable Today</div>
          <div className={`text-2xl font-bold font-mono ${readyCol}`}>{ready}<span className="text-neutral-600 text-sm">/{rated.length}</span></div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Buy Now or Accumulate grade</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Avg Timing Score</div>
          <div className="text-2xl font-bold font-mono text-neutral-100">{avg.toFixed(0)}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">0–100 · 70+ is a clean entry</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Extended &gt; 2.5 ATR</div>
          <div className={`text-2xl font-bold font-mono ${extended.length ? 'text-rose-400' : 'text-emerald-400'}`}>{extended.length}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Stretched above the 20-day mean</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Event Risk Flagged</div>
          <div className={`text-2xl font-bold font-mono ${risky.length ? 'text-rose-400' : 'text-emerald-400'}`}>{risky.length}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">
            {avgRiskPct != null ? `Avg 1-ATR risk ${avgRiskPct.toFixed(1)}%` : 'Earnings, macro, seasonal'}
          </div>
        </div>
      </div>

      <div className="px-4 py-3">
        <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-2">Grade Distribution</div>
        <div className="flex h-2 rounded-full overflow-hidden bg-neutral-800 mb-2">
          {TIMING_GRADES.map(g => counts[g] > 0 && (
            <div key={g} className={timingMeta(g).bar} style={{ width: `${counts[g] / rated.length * 100}%` }} />
          ))}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          {TIMING_GRADES.map(g => (
            <div key={g} className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-sm ${timingMeta(g).bar}`} />
              <span className="text-[10px] text-neutral-400">{timingMeta(g).label}</span>
              <span className={`text-[10px] font-mono font-bold ${timingMeta(g).text}`}>{counts[g]}</span>
            </div>
          ))}
        </div>
        {ready === 0 && (
          <div className="mt-2 text-[11px] text-rose-300/90">
            Nothing on this list is a clean entry right now. Holding cash is a position.
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Buy Today ────────────────────────────────────────────────────────────────
// The decisive answer to "what do I buy?". A name appears here only when the
// fundamental case, the technical entry and the event calendar all agree.
// Derived entirely from the payload already on screen — no extra requests.
//
// Two tiers, deliberately: BUY is the unambiguous set; SCALE IN is good enough
// to start a position but not to take full size. Everything else is excluded on
// purpose. An empty list is a real answer, not a failure.
const CONVICTION_MIN_CONF = 75
const SCALE_MIN_CONF = 72
const SCALE_MIN_TIMING = 62

function buyTodayTiers(picks) {
  const rated = (picks || []).filter(p =>
    p && p.timing && p.timing.ok === true && !((p.timing.grade_caps || []).length))

  // Event risk is a veto, not a discount: ELEVATED/HIGH means a binary event
  // lands inside the entry window, and a good chart does not survive a bad print.
  const calmEvent = p => !['ELEVATED', 'HIGH'].includes(p.timing.event_risk?.level || 'LOW')
  const rank = (a, b) =>
    ((b.fundamental_score || 0) + (b.timing.timing_score || 0)) -
    ((a.fundamental_score || 0) + (a.timing.timing_score || 0))

  const buy = rated.filter(p =>
    p.timing.grade === 'BUY_NOW' &&
    calmEvent(p) &&
    (p.confidence || 0) >= CONVICTION_MIN_CONF
  ).sort(rank)

  const buySet = new Set(buy.map(p => p.ticker))
  const scale = rated.filter(p =>
    !buySet.has(p.ticker) &&
    ['BUY_NOW', 'ACCUMULATE'].includes(p.timing.grade) &&
    (p.timing.timing_score || 0) >= SCALE_MIN_TIMING &&
    (p.confidence || 0) >= SCALE_MIN_CONF
  ).sort(rank)

  return { buy, scale, ratedCount: rated.length }
}

function ConvictionRow({ p, tier }) {
  const t = p.timing
  const plan = t.entry_plan || {}
  const accent = tier === 'BUY' ? 'text-emerald-400' : 'text-white'
  return (
    <div className="grid grid-cols-12 gap-2 px-4 py-2.5 border-b border-neutral-800/40 items-center text-xs hover:bg-neutral-800/20">
      <div className="col-span-2 flex items-center gap-2 min-w-0">
        <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border shrink-0 ${
          tier === 'BUY'
            ? 'text-emerald-400 border-emerald-600/50 bg-emerald-600/10'
            : 'text-neutral-300 border-neutral-600/50 bg-neutral-700/30'
        }`}>{tier === 'BUY' ? 'Buy' : 'Scale'}</span>
        <Link
          to={`/symbol/${p.ticker}?side=long`}
          className={`font-bold text-base hover:underline truncate ${accent}`}
        >{p.ticker}</Link>
      </div>
      <div className="col-span-2 text-neutral-500 text-[11px] truncate">{p.sector || '—'}</div>
      <div className="col-span-2 text-right font-mono text-neutral-200">
        {plan.entry_zone_low != null
          ? `${fmtPrice(plan.entry_zone_low)}–${fmtPrice(plan.entry_zone_high)}`
          : fmtPrice(p.entry_price)}
        <div className="text-[9px] text-neutral-600">entry zone</div>
      </div>
      <div className="col-span-2 text-right font-mono">
        <div className="text-rose-300/80">{fmtPrice(p.stop_loss)}</div>
        <div className="text-emerald-300/80">{fmtPrice(p.target_price)}</div>
      </div>
      <div className="col-span-1 text-right font-mono text-neutral-200">
        {t.timing_score != null ? Math.round(t.timing_score) : '—'}
        <div className="text-[9px] text-neutral-600">timing</div>
      </div>
      <div className="col-span-1 text-right font-mono text-neutral-200">
        {p.confidence != null ? p.confidence + '%' : '—'}
        <div className="text-[9px] text-neutral-600">conf</div>
      </div>
      <div className="col-span-2 text-[10px] text-neutral-500 leading-tight">
        {tier === 'BUY' ? 'Full size' : 'Partial size, add on weakness'}
        {t.near_term_risk_pct != null && ` · 1-ATR risk ${t.near_term_risk_pct.toFixed(1)}%`}
      </div>
    </div>
  )
}

function BuyToday({ picks }) {
  const { buy, scale, ratedCount } = buyTodayTiers(picks)
  if (!ratedCount) return null
  const shown = [
    ...buy.slice(0, 8).map(p => ({ p, tier: 'BUY' })),
    ...scale.slice(0, 8).map(p => ({ p, tier: 'SCALE' })),
  ]

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white">Buy Today</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            Names where the fundamental case, the technical entry and the event calendar all agree.
            Everything else on this page is a watch item, not a buy.
          </p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-2xl font-bold font-mono ${buy.length ? 'text-emerald-400' : 'text-rose-400'}`}>{buy.length}</div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Full size</div>
        </div>
      </div>

      {shown.length === 0 ? (
        <div className="px-4 py-5">
          <p className="text-sm text-rose-300/90 font-bold">Nothing qualifies today.</p>
          <p className="text-[11px] text-neutral-500 mt-1 leading-snug">
            {ratedCount} picks were graded and none cleared the entry, confidence and event-risk bars
            simultaneously. Forcing a trade into that is how a good watchlist becomes a bad month.
            Cash is the position until a setup repairs.
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
            <div className="col-span-2">Action / Ticker</div>
            <div className="col-span-2">Sector</div>
            <div className="col-span-2 text-right">Buy Between</div>
            <div className="col-span-2 text-right">Stop / Target</div>
            <div className="col-span-1 text-right">Timing</div>
            <div className="col-span-1 text-right">Conf</div>
            <div className="col-span-2">Sizing</div>
          </div>
          {shown.map(({ p, tier }) => <ConvictionRow key={p.ticker} p={p} tier={tier} />)}
          <div className="px-4 py-2.5 text-[10px] text-neutral-600 leading-snug">
            Buy = timing grade Buy Now, confidence ≥ {CONVICTION_MIN_CONF}%, no uncapped technical flaw, event risk low or moderate.
            Scale = acceptable entry (timing ≥ {SCALE_MIN_TIMING}, confidence ≥ {SCALE_MIN_CONF}%) that does not justify full size.
            Work limit orders inside the zone — do not pay the market.
          </div>
        </>
      )}
    </div>
  )
}

// Per-pick detail: the full signal stack, the entry plan, and event risk.
function TimingPanel({ t, stopLoss }) {
  if (!t) return null
  const m = timingMeta(t.grade)

  if (!t.ok) {
    return (
      <div className="mt-3 pt-3 border-t border-neutral-800/60">
        <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-1">Entry Timing</div>
        <div className="text-[11px] text-neutral-500">
          {t.summary || 'No short-term timing read available.'}
          {t.reason ? <span className="text-neutral-700"> ({t.reason})</span> : null}
        </div>
      </div>
    )
  }

  const signals = Array.isArray(t.signals) ? t.signals : []
  const plan = t.entry_plan
  const ev = t.event_risk || {}
  const caps = Array.isArray(t.grade_caps) ? t.grade_caps : []

  return (
    <div className="mt-3 pt-3 border-t border-neutral-800/60">
      <div className="flex items-center flex-wrap gap-2 mb-2">
        <span className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Entry Timing</span>
        <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded border ${m.text} ${m.border} ${m.bg}`}>
          {m.label} · {Math.round(t.timing_score)}/100
        </span>
        <span className="text-[10px] text-emerald-400/80">{t.bull_count} bullish</span>
        <span className="text-[10px] text-rose-400/80">{t.bear_count} bearish</span>
        {t.near_term_risk_pct != null && (
          <span className="text-[10px] text-neutral-500">1-ATR risk {t.near_term_risk_pct}%</span>
        )}
        {ev.level && ev.level !== 'LOW' && (
          <span className={`text-[10px] font-bold ${EVENT_RISK_TEXT[ev.level] || 'text-neutral-400'}`}>
            Event risk {ev.level.toLowerCase()}
          </span>
        )}
      </div>

      <p className="text-[11px] text-neutral-300 mb-2">{t.summary}</p>

      {caps.length > 0 && (
        <div className="mb-2 space-y-0.5">
          {caps.map((c, i) => (
            <div key={i} className="text-[10px] text-rose-300/90">Grade capped: {c}</div>
          ))}
        </div>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        {/* Signal stack — worst first, because the weakest link sets the risk. */}
        <div className="space-y-1">
          {signals.map(s => (
            <div key={s.name} className="flex items-center gap-2">
              <span className="text-[9px] text-neutral-500 w-24 shrink-0 truncate capitalize">{s.label}</span>
              <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden min-w-[40px]">
                <div className={`h-full ${stateBar(s.state)}`} style={{ width: `${Math.max(2, Math.min(100, s.score))}%` }} />
              </div>
              <span className={`text-[9px] font-mono w-7 text-right shrink-0 ${stateText(s.state)}`}>{s.score}</span>
              <span className="text-[9px] text-neutral-500 flex-1 truncate hidden lg:block" title={s.detail}>{s.detail}</span>
            </div>
          ))}
        </div>

        {/* Entry plan + event risk */}
        <div>
          {plan && (
            <div className="bg-neutral-900/60 border border-neutral-800/60 rounded-lg px-3 py-2">
              <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-1.5">Entry Plan</div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
                <span className="text-neutral-500">Ideal entry</span>
                <span className="font-mono text-neutral-100 text-right">{fmtPrice(plan.ideal_entry)}</span>
                <span className="text-neutral-500">Entry zone</span>
                <span className="font-mono text-neutral-300 text-right">{fmtPrice(plan.entry_zone_low)} – {fmtPrice(plan.entry_zone_high)}</span>
                <span className="text-neutral-500">Technical invalidation</span>
                <span className="font-mono text-rose-300/90 text-right">{fmtPrice(plan.structural_stop)} ({plan.stop_pct}%)</span>
                <span className="text-neutral-500">Size factor</span>
                <span className={`font-mono text-right font-bold ${plan.size_factor >= 1 ? 'text-emerald-400' : plan.size_factor <= 0 ? 'text-rose-400' : 'text-neutral-200'}`}>
                  {(plan.size_factor * 100).toFixed(0)}%
                </span>
              </div>
              <div className="text-[10px] text-neutral-400 mt-1.5 pt-1.5 border-t border-neutral-800/60">
                {plan.note}
                <span className="text-neutral-600"> The {fmtPrice(stopLoss)} position stop governs the trade; the level above is where the technical thesis breaks.</span>
              </div>
            </div>
          )}
          {Array.isArray(ev.items) && ev.items.length > 0 && (
            <div className="mt-2">
              <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-1">Event Watch</div>
              <ul className="space-y-0.5">
                {ev.items.map((it, i) => (
                  <li key={i} className="text-[10px] text-neutral-400">· {it}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Pick Row ─────────────────────────────────────────────────────────────────
function PickRow({ p, rank, cash, expanded, onToggle, qhfSig }) {
  const alloc = calcAlloc(cash, p.entry_price, rank, p.timing?.entry_plan?.size_factor)
  const rrColor = !p.reward_risk_ratio ? 'text-neutral-500'
    : p.reward_risk_ratio >= 3 ? 'text-emerald-400'
    : p.reward_risk_ratio >= 2 ? 'text-neutral-300'
    : 'text-rose-400'

  const reasons = Array.isArray(p.reasons) ? p.reasons.filter(Boolean) : []
  const [wlState, setWlState] = useState(null) // null | 'added' | 'exists' | 'error'

  return (
    <div>
      {/* Main row — click to expand, ticker navigates */}
      <div
        onClick={onToggle}
        className={`grid grid-cols-12 gap-1 px-4 py-3 border-b border-neutral-800/40 hover:bg-neutral-800/30 transition-colors items-center text-sm cursor-pointer group ${expanded ? 'bg-neutral-800/20' : ''}`}
      >
        {/* Rank + chevron + ticker */}
        <div className="col-span-2 flex items-center gap-1.5 min-w-0">
          <span
            className={`text-[8px] text-neutral-600 shrink-0 transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`}
            style={{ display: 'inline-block' }}
          >▶</span>
          <span className="text-neutral-600 text-xs font-mono w-4 text-right shrink-0">{rank}</span>
          <Link
            to={`/symbol/${p.ticker}?side=long`}
            onClick={e => e.stopPropagation()}
            className="font-bold text-white text-base group-hover:text-neutral-300 hover:underline truncate"
          >
            {p.ticker}
          </Link>
        </div>

        {/* Sector */}
        <div className="col-span-1 text-neutral-400 text-xs truncate">{p.sector || '—'}</div>

        {/* Entry + technical entry-timing grade */}
        <div className="col-span-1 text-right">
          <div className="font-mono text-neutral-200 text-xs">{fmtPrice(p.entry_price)}</div>
          <div className="mt-0.5"><TimingChip t={p.timing} /></div>
        </div>

        {/* Fund Score */}
        <div className={`col-span-1 text-right font-mono text-xs font-bold ${scoreColor(p.fundamental_score)}`}>
          {p.fundamental_score != null ? p.fundamental_score.toFixed(0) : '—'}
        </div>

        {/* Rev Growth */}
        <div className={`col-span-1 text-right font-mono text-xs ${growthColor(p.revenue_growth_pct)}`}>
          {fmt(p.revenue_growth_pct, '%', 0)}
        </div>

        {/* Confidence + QHF alignment */}
        <div className="col-span-1 text-right">
          <div className={`font-mono text-xs font-bold ${confColor(p.confidence)}`}>
            {p.confidence != null ? p.confidence + '%' : '—'}
          </div>
          <QHFBadge sig={qhfSig} />
        </div>

        {/* ROE */}
        <div className={`col-span-1 text-right font-mono text-xs ${growthColor(p.roe_pct)}`}>
          {fmtPos(p.roe_pct, '%', 0)}
        </div>

        {/* PEG */}
        <div className={`col-span-1 text-right font-mono text-xs ${
          p.peg_ratio == null ? 'text-neutral-500'
          : p.peg_ratio < 1 ? 'text-emerald-400'
          : p.peg_ratio < 2 ? 'text-neutral-300'
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
            <span className={`text-[10px] ${alloc.sf === 0 ? 'text-rose-400 font-bold' : 'text-neutral-600'}`}>
              {alloc.sf === 0 ? 'no entry' : 'skip'}
            </span>
          ) : (
            <div>
              <div className="text-neutral-100 font-bold">{alloc.shares} sh</div>
              <div className="text-[10px] text-neutral-500">{fmtMoney(alloc.dollars)}</div>
              {alloc.sf < 1 && (
                <div className="text-[9px] text-neutral-500">{(alloc.sf * 100).toFixed(0)}% size · timing</div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Expanded reasons + QHF factors panel */}
      {expanded && (
        <div className="px-6 py-3 bg-neutral-900/70 border-b border-neutral-800/60">
          {reasons.length > 0 ? (
            <div className="flex flex-wrap gap-2 mb-1">
              {reasons.map((r, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1 bg-neutral-800/60 border border-neutral-600/40 rounded-full text-neutral-200"
                >
                  <span className="text-neutral-500 font-bold">·</span>
                  {r}
                </span>
              ))}
            </div>
          ) : (
            <span className="text-xs text-neutral-600">No signal details available for this pick.</span>
          )}
          <TimingPanel t={p.timing} stopLoss={p.stop_loss} />
          <QHFFactors sig={qhfSig} />

          {/* Buy this pick → carry entry/stop/target into the Watchlist sell engine */}
          <div className="mt-3 flex items-center gap-3">
            <button
              onClick={e => { e.stopPropagation(); setWlState(addPickToWatchlist(p)) }}
              disabled={wlState === 'added' || wlState === 'exists'}
              className={`px-4 py-2 rounded-lg font-bold text-xs transition-colors ${
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
                ? '✓ Added to watchlist'
                : wlState === 'exists'
                ? 'Already in watchlist'
                : wlState === 'error'
                ? 'Could not add — retry'
                : 'Add to watchlist (buy this pick)'}
            </button>
            <span className="text-[11px] text-neutral-500">
              Carries stop {fmtPrice(p.stop_loss)} &amp; target {fmtPrice(p.target_price)} for automatic SELL alerts.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Pick Table ───────────────────────────────────────────────────────────────
function PickTable({ picks, cash, quantSignals }) {
  const [sortCol, setSortCol] = useState(null)
  const [sortDir, setSortDir] = useState('desc')
  const [expandedTickers, setExpandedTickers] = useState(new Set())

  function onSort(col) {
    handleSortClick(col, sortCol, sortDir, setSortCol, setSortDir)
  }

  function toggleExpand(ticker) {
    setExpandedTickers(prev => {
      const next = new Set(prev)
      if (next.has(ticker)) next.delete(ticker)
      else next.add(ticker)
      return next
    })
  }

  if (!picks || !picks.length) {
    return (
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-8 text-center text-neutral-500">
        No picks available — the fundamental scan is initializing. Results populate within approximately 2 minutes.
      </div>
    )
  }

  const displayed = sortedPicks(picks, sortCol, sortDir)

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 flex items-center justify-between bg-neutral-900/60">
        <div>
          <h2 className="text-lg font-bold text-white">Long-Term Fundamental Picks</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            {picks.length} picks · {sortCol ? `Sorted by ${sortCol.replace(/[._]/g, ' ')} ${sortDir}` : 'Ranked by fundamental score'} · Click row to expand signals, entry plan and timing · Click ticker to view detail
          </p>
        </div>
        <span className="text-[10px] font-bold uppercase tracking-wider text-neutral-300 border border-neutral-600/50 px-2 py-1 rounded">
          6-12 Week Hold
        </span>
      </div>

      {/* Header row */}
      <div className="grid grid-cols-12 gap-1 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60">
        <div className="col-span-2 text-[10px] font-bold uppercase tracking-wider text-neutral-500">Ticker</div>
        <div className="col-span-1 text-[10px] font-bold uppercase tracking-wider text-neutral-500">Sector</div>
        <SortTh label="Entry / Timing" col="timing.timing_score" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <SortTh label="Score"   col="fundamental_score"  sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <SortTh label="Rev Gr"  col="revenue_growth_pct" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <SortTh label="Conf"    col="confidence"         sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <SortTh label="ROE"     col="roe_pct"            sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <SortTh label="PEG"     col="peg_ratio"          sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <div className="col-span-1 text-[10px] font-bold uppercase tracking-wider text-neutral-500 text-right">Stop/Tgt</div>
        <SortTh label="R:R"     col="reward_risk_ratio"  sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
        <div className="col-span-1 text-[10px] font-bold uppercase tracking-wider text-neutral-500 text-right">Allocate</div>
      </div>

      <div className="max-h-[800px] overflow-y-auto">
        {displayed.map((p, i) => (
          <PickRow
            key={p.ticker}
            p={p}
            rank={i + 1}
            cash={cash}
            expanded={expandedTickers.has(p.ticker)}
            onToggle={() => toggleExpand(p.ticker)}
            qhfSig={(quantSignals || {})[p.ticker] || null}
          />
        ))}
      </div>
    </div>
  )
}

// ─── Cash Input ───────────────────────────────────────────────────────────────
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
          className="pl-7 pr-3 py-2 w-44 bg-neutral-900 border border-neutral-700 rounded-lg text-sm text-white font-mono placeholder-neutral-600 focus:border-neutral-500 focus:outline-none"
        />
      </div>
      {value > 0 && (
        <div className="text-[11px] text-neutral-500">
          {(ALLOC_PCT * 100).toFixed(0)}% per pick · top {MAX_FUNDED} positions
          · max deploy ≈ {fmtMoney(value * ALLOC_PCT * Math.min(MAX_FUNDED, 10))}
          · scaled down automatically on poor entry timing
        </div>
      )}
    </div>
  )
}

// ─── QHF Signal Badge ─────────────────────────────────────────────────────────
function QHFBadge({ sig }) {
  if (!sig) return <span className="text-neutral-700 text-[9px]">—</span>
  const isLong  = sig.direction === 'LONG'
  const isShort = sig.direction === 'SHORT'
  const col = isLong ? 'text-emerald-400' : isShort ? 'text-rose-400' : 'text-neutral-500'
  const arrow = isLong ? '▲' : isShort ? '▼' : '·'
  return (
    <div className={`text-[9px] font-mono font-bold ${col} leading-tight`}>
      <span className="text-neutral-600 font-normal">QHF </span>
      {arrow} {sig.confidence ?? '?'}%
    </div>
  )
}

// ─── QHF Factor Bar ───────────────────────────────────────────────────────────
function QHFFactors({ sig }) {
  if (!sig || !sig.factors) return null
  const entries = Object.entries(sig.factors)
    .map(([k, v]) => ({ name: k, contrib: v?.contribution ?? 0 }))
    .filter(e => Math.abs(e.contrib) > 0.03)
    .sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib))
    .slice(0, 5)
  if (!entries.length) return null
  const maxAbs = Math.max(...entries.map(e => Math.abs(e.contrib)), 0.01)
  const isLong  = sig.direction === 'LONG'
  const isShort = sig.direction === 'SHORT'
  const dirCol  = isLong ? 'text-emerald-300' : isShort ? 'text-rose-300' : 'text-neutral-400'
  const arrow   = isLong ? '▲' : isShort ? '▼' : '·'
  return (
    <div className="mt-3 pt-3 border-t border-neutral-800/60">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Quant HF Signal</span>
        <span className={`text-[10px] font-bold ${dirCol}`}>{arrow} {sig.direction} {sig.confidence}%</span>
        <span className="text-[9px] text-neutral-600">composite {sig.composite_score?.toFixed(2)}</span>
      </div>
      <div className="space-y-1">
        {entries.map(e => {
          const pct = Math.abs(e.contrib) / maxAbs * 100
          const bull = e.contrib > 0
          return (
            <div key={e.name} className="flex items-center gap-2">
              <span className="text-[9px] text-neutral-500 w-24 truncate capitalize">{e.name.replace(/_/g, ' ')}</span>
              <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full ${bull ? 'bg-emerald-500/60' : 'bg-rose-500/60'}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className={`text-[9px] font-mono ${bull ? 'text-emerald-400' : 'text-rose-400'}`}>
                {e.contrib > 0 ? '+' : ''}{e.contrib.toFixed(2)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────
export default function SymbolsToBuy() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [retryCountdown, setRetryCountdown] = useState(null)
  const [retryTick, setRetryTick] = useState(0)
  const retryTimerRef = useRef(null)
  const countdownRef = useRef(null)
  const [quantSignals, setQuantSignals] = useState({})
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
      if (!j.ok && j.reason && j.reason !== 'warming_up' && j.reason !== 'no_picks_yet') throw new Error(j.message || j.reason)
      setData(j)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => { load(false) }, [])

  // Fetch quant-picks (cache-only, no external calls) to overlay QHF signals on STB picks.
  // Runs once on mount and does NOT interfere with the STB scoring logic.
  useEffect(() => {
    fetch('/api/quant-picks')
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (!j) return
        const map = {}
        for (const p of [...(j.long_picks || []), ...(j.short_picks || [])]) {
          if (p.ticker) map[p.ticker] = {
            direction: p.direction,
            confidence: p.confidence,
            composite_score: p.composite_score,
            factors: p.factors,
          }
        }
        setQuantSignals(map)
      })
      .catch(() => {})
  }, [])

  // Auto-retry every 45s when picks haven't loaded yet (warming_up / no_picks_yet).
  // Without this, users who land on the page during cold-start see the warming_up
  // message forever and must manually click Force Refresh.
  useEffect(() => {
    const notReady = data && data.ok === false
    if (!notReady) {
      // Picks loaded — clear any pending retry timers
      if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null }
      if (countdownRef.current)  { clearInterval(countdownRef.current);  countdownRef.current  = null }
      setRetryCountdown(null)
      return
    }
    // Start a 45-second countdown then retry silently
    const RETRY_SECS = 45
    setRetryCountdown(RETRY_SECS)
    countdownRef.current = setInterval(() => {
      setRetryCountdown(prev => (prev != null && prev > 1 ? prev - 1 : null))
    }, 1000)
    retryTimerRef.current = setTimeout(() => {
      if (countdownRef.current) { clearInterval(countdownRef.current); countdownRef.current = null }
      setRetryCountdown(null)
      // Silent refresh: show "Refreshing…" on the button but keep existing content visible
      setRefreshing(true)
      fetch('/api/symbols-to-buy')
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
        .then(j => {
          if (!j.ok && j.reason && j.reason !== 'warming_up' && j.reason !== 'no_picks_yet') {
            throw new Error(j.message || j.reason)
          }
          setData(j)
          // If still not ready, bump tick so the effect re-fires for another cycle
          if (!j.ok) setRetryTick(t => t + 1)
        })
        .catch(e => setError(e.message))
        .finally(() => setRefreshing(false))
    }, RETRY_SECS * 1000)
    return () => {
      if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null }
      if (countdownRef.current)  { clearInterval(countdownRef.current);  countdownRef.current  = null }
    }
  }, [data?.ok, data?.reason, retryTick])

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
              Fundamentals-driven · 6-12 week holds · Revenue growth + ROE + valuation · Bridgewater macro overlay
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

        {/* Regime banner — always show when data is loaded */}
        {data && (
          <div className="mt-3">
            <RegimeBanner regime={data.hf_regime} />
          </div>
        )}

        {/* Meta bar */}
        {data && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-neutral-500 mt-3">
            <span className="px-2 py-1 bg-white/10 rounded border border-white/20 text-white font-bold">
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
              Cache age: <span className={data.cache_is_restoring ? 'text-neutral-300 font-bold' : 'text-white font-bold'}>
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

      {/* Warming-up banner — shown while backend scan is still running */}
      {data && data.ok === false && !loading && (
        <div className="bg-neutral-900/50 border border-neutral-700/50 rounded-xl p-8 text-center mb-4">
          <div className="text-white font-bold text-lg mb-2">Fundamental scan in progress…</div>
          <div className="text-neutral-400 text-sm mb-3">
            {data.message || 'Picks will populate automatically once the scan completes.'}
          </div>
          {retryCountdown != null && (
            <div className="text-neutral-500 text-xs">
              Auto-refreshing in <span className="text-white font-mono font-bold">{retryCountdown}s</span>
            </div>
          )}
        </div>
      )}

      {error && !loading && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 mb-6">
          {error}
          <button
            onClick={() => { setError(null); load(false) }}
            className="ml-4 text-xs underline text-red-400 hover:text-red-200"
          >Retry</button>
        </div>
      )}

      {data && data.ok !== false && !loading && (
        <div className="space-y-6">
          <BuyToday picks={data.long_picks} />

          {data.guidance && (
            <div className="bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-4 py-3 text-sm text-neutral-300">
              <span className="font-bold mr-2 text-white">Strategy:</span>{data.guidance}
            </div>
          )}

          {/* Score legend */}
          <div className="flex flex-wrap gap-4 text-xs text-neutral-500">
            <span><span className="text-emerald-400 font-bold">Score 70+</span> = Strong buy</span>
            <span><span className="text-neutral-300 font-bold">Score 55–70</span> = Good quality</span>
            <span><span className="text-emerald-400 font-bold">Conf 75%+</span> = High conviction · min 70% required</span>
            <span><span className="font-bold text-neutral-400">Rev Gr</span> = YoY revenue growth</span>
            <span><span className="font-bold text-neutral-400">ROE</span> = Return on equity</span>
            <span><span className="font-bold text-neutral-400">PEG</span> = Price/earnings-to-growth ({"<"}1.5 = good)</span>
            <span><span className="font-bold text-neutral-400">R:R</span> = Reward:risk ratio (3x+ = ideal)</span>
            <span><span className="font-bold text-neutral-400">Timing</span> = short-term entry grade (Buy Now / Accumulate / Wait / Avoid)</span>
            <span><span className="font-bold text-neutral-400">Click any column header</span> to sort · <span className="font-bold text-neutral-400">Click row</span> to expand signals</span>
          </div>

          <TimingSummary picks={data.long_picks} />

          <PickTable picks={data.long_picks} cash={cash} quantSignals={quantSignals} />
        </div>
      )}
    </div>
  )
}
