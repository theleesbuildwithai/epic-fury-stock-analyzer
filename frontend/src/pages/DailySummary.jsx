import { useState, useEffect, useRef, useMemo, Component } from 'react'
import { Link } from 'react-router-dom'

// DAILY SUMMARY — the session read, not a leaderboard.
//
// A top-10 gainers list is entertainment. What actually changes how you trade
// is breadth (did the whole tape go up, or did ten names carry it?) and
// dispersion (does single-name selection pay today, or is everything just
// beta?). Both are computed here from the ONE cached /api/daily-summary
// payload — no extra requests, no fan-out.
//
// Rate-limit discipline: single in-flight guard, visibility-gated refresh on
// the same 5-minute clock the backend caches on, and last-good payload kept on
// screen when a refresh fails rather than blanking the page.

class PanelBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(e) { try { console.error('Daily summary panel error:', e) } catch {} }
  render() {
    if (this.state.failed) {
      return (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl px-4 py-3 text-xs text-neutral-500">
          {this.props.label || 'Panel'} could not be rendered safely; excluded.
        </div>
      )
    }
    return this.props.children
  }
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)
// Price integrity: a quote of 0, a negative, or an absurd number is corrupt
// data, not a cheap stock. Render a dash rather than a confident wrong figure.
const MIN_PX = 0.01
const MAX_PX = 1_000_000
const okPrice = (v) => isNum(v) && v >= MIN_PX && v <= MAX_PX
const px = (v) => okPrice(v) ? '$' + v.toFixed(2) : '—'
const pct = (v, dec = 2) => isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(dec) + '%' : '—'
const chgText = (v) => !isNum(v) ? 'text-neutral-400' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-rose-400' : 'text-neutral-400'
const moodText = (m) => typeof m === 'string' && m.includes('Bullish') ? 'text-emerald-400'
  : typeof m === 'string' && m.includes('Bearish') ? 'text-rose-400' : 'text-neutral-300'

// ─── Session breadth ──────────────────────────────────────────────────────────
// Breadth is the read, not the mover list. Ten names up 5% inside a tape where
// 70% of issues fell is a narrow, fragile rally — the opposite of what the
// gainers table looks like on its own.
function SessionRead({ data }) {
  const mo = data?.market_overview || {}
  const gainers = (Array.isArray(data?.gainers) ? data.gainers : []).filter(g => g && okPrice(g.price))
  const losers = (Array.isArray(data?.losers) ? data.losers : []).filter(g => g && okPrice(g.price))
  const adv = isNum(mo.advancing) ? mo.advancing : 0
  const dec = isNum(mo.declining) ? mo.declining : 0
  const total = isNum(mo.total_stocks) ? mo.total_stocks : (adv + dec)
  const flat = Math.max(0, total - adv - dec)
  const advPct = total > 0 ? Math.round(adv / total * 100) : 0
  const decPct = total > 0 ? Math.round(dec / total * 100) : 0
  const flatPct = Math.max(0, 100 - advPct - decPct)
  const adRatio = dec > 0 ? adv / dec : (adv > 0 ? Infinity : null)

  // Dispersion: top gainer minus bottom loser. Wide spread = selection pays.
  // Narrow spread = everything is beta and stock picking adds nothing today.
  const best = gainers.length ? gainers[0].change_pct : null
  const worst = losers.length ? losers[0].change_pct : null
  const dispersion = (isNum(best) && isNum(worst)) ? best - worst : null
  const dispersionRead = !isNum(dispersion) ? null
    : dispersion > 10 ? 'Wide — single-name selection is what pays today'
    : dispersion > 5 ? 'Normal — selection matters at the margin'
    : 'Narrow — this is a beta day; names are moving together'

  const breadthRead = total === 0 ? null
    : advPct >= 65 ? 'Broad advance — the tape is carrying names, not the reverse'
    : decPct >= 65 ? 'Broad decline — do not read individual strength as a signal here'
    : Math.abs(advPct - decPct) <= 12 ? 'Split tape — no directional information in breadth'
    : advPct > decPct ? 'Mildly positive breadth — narrow leadership, treat rallies as fragile'
    : 'Mildly negative breadth — rallies are being sold'

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60">
        <h2 className="text-lg font-bold text-white">Session Read</h2>
        <p className="text-xs text-neutral-500 mt-0.5">
          Breadth and dispersion across {total || '—'} large-cap benchmark names. The mover lists below tell you what
          happened; these four numbers tell you whether it means anything.
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-neutral-800/60 border-b border-neutral-800/60">
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Tape Mood</div>
          <div className={`text-xl font-bold ${moodText(mo.mood)}`}>{mo.mood || '—'}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">avg move {pct(mo.avg_change_pct)}</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Advance / Decline</div>
          <div className="text-2xl font-bold font-mono">
            <span className="text-emerald-400">{adv}</span>
            <span className="text-neutral-600 text-sm"> / </span>
            <span className="text-rose-400">{dec}</span>
          </div>
          <div className="text-[10px] text-neutral-600 mt-0.5">
            {adRatio === null ? '—' : adRatio === Infinity ? 'no decliners' : `A/D ratio ${adRatio.toFixed(2)}`}
          </div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Dispersion</div>
          <div className={`text-2xl font-bold font-mono ${isNum(dispersion) ? (dispersion > 10 ? 'text-white' : 'text-neutral-300') : 'text-neutral-600'}`}>
            {isNum(dispersion) ? dispersion.toFixed(1) + 'pt' : '—'}
          </div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Best minus worst mover</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Session</div>
          <div className={`text-xl font-bold ${data?.market_open ? 'text-emerald-400' : 'text-neutral-300'}`}>
            {data?.market_open ? 'Open' : 'Closed'}
          </div>
          <div className="text-[10px] text-neutral-600 mt-0.5">
            {data?.market_open ? 'Figures move intraday' : 'Last completed session'}
          </div>
        </div>
      </div>

      <div className="px-4 py-3">
        <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-2">Breadth Distribution</div>
        <div className="flex h-2 rounded-full overflow-hidden bg-neutral-800 mb-2">
          {advPct > 0 && <div className="bg-emerald-500" style={{ width: `${advPct}%` }} />}
          {flatPct > 0 && <div className="bg-neutral-500" style={{ width: `${flatPct}%` }} />}
          {decPct > 0 && <div className="bg-rose-500" style={{ width: `${decPct}%` }} />}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 mb-2">
          {[['Advancing', adv, advPct, 'bg-emerald-500', 'text-emerald-400'],
            ['Unchanged', flat, flatPct, 'bg-neutral-500', 'text-neutral-300'],
            ['Declining', dec, decPct, 'bg-rose-500', 'text-rose-400']].map(([l, n, p, bar, txt]) => (
            <div key={l} className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-sm ${bar}`} />
              <span className="text-[10px] text-neutral-400">{l}</span>
              <span className={`text-[10px] font-mono font-bold ${txt}`}>{n} ({p}%)</span>
            </div>
          ))}
        </div>
        {breadthRead && <p className="text-[11px] text-neutral-400">{breadthRead}.</p>}
        {dispersionRead && <p className="text-[11px] text-neutral-500 mt-0.5">{dispersionRead}.</p>}
      </div>
    </div>
  )
}

// ─── Mover table ──────────────────────────────────────────────────────────────
function MoverTable({ title, note, rows, side }) {
  const clean = (Array.isArray(rows) ? rows : []).filter(r => r && okPrice(r.price) && isNum(r.change_pct))
  const max = clean.length ? Math.max(...clean.map(r => Math.abs(r.change_pct)), 0.1) : 1
  const up = side === 'up'

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white">{title}</h2>
          <p className="text-xs text-neutral-500 mt-0.5">{note}</p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-xl font-bold font-mono ${up ? 'text-emerald-400' : 'text-rose-400'}`}>
            {clean.length ? pct(clean[0].change_pct, 1) : '—'}
          </div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Extreme</div>
        </div>
      </div>
      {clean.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-neutral-500">
          No validated quotes returned for this side. Nothing is shown rather than a guessed price.
        </div>
      ) : clean.map((s, i) => (
        <Link
          key={s.symbol || i}
          to={`/symbol/${encodeURIComponent(s.symbol || '')}`}
          className="flex items-center gap-3 px-4 py-2.5 border-b border-neutral-800/40 last:border-b-0 hover:bg-neutral-800/20 transition-colors"
        >
          <span className="text-[10px] font-mono text-neutral-600 w-4 shrink-0">{i + 1}</span>
          <div className="w-28 shrink-0 min-w-0">
            <div className="font-mono font-bold text-sm text-white truncate">{s.symbol || '—'}</div>
            <div className="text-[10px] text-neutral-600 truncate">{s.name || ''}</div>
          </div>
          <span className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden hidden sm:block">
            <span
              className={`block h-full ${up ? 'bg-emerald-500' : 'bg-rose-500'}`}
              style={{ width: `${Math.min(100, Math.abs(s.change_pct) / max * 100)}%` }}
            />
          </span>
          <div className="text-right shrink-0 w-20">
            <div className="font-mono text-sm text-neutral-100">{px(s.price)}</div>
            <div className="text-[10px] font-mono text-neutral-600">
              {isNum(s.change) ? (s.change > 0 ? '+' : '') + s.change.toFixed(2) : '—'}
            </div>
          </div>
          <div className={`text-right shrink-0 w-16 font-mono font-bold text-sm ${chgText(s.change_pct)}`}>
            {pct(s.change_pct)}
          </div>
        </Link>
      ))}
    </div>
  )
}

// ─── Watchlist ────────────────────────────────────────────────────────────────
// Your names, with the two things that actually require a decision surfaced
// first: an RSI extreme, and a one-day move large enough to invalidate a thesis.
const RSI_OVERSOLD = 30
const RSI_OVERBOUGHT = 70
const BIG_MOVE_PCT = 3

const WL_SORTS = {
  attention: { label: 'Needs attention' },
  day:       { label: 'Day move' },
  week:      { label: 'Week' },
  month:     { label: 'Month' },
  rsi:       { label: 'RSI' },
  symbol:    { label: 'Symbol' },
}

function attentionOf(s) {
  // Higher = more decision-worthy. RSI extremes and large single-day moves are
  // the only two things on this table that can force an action today.
  let n = 0
  if (isNum(s.rsi)) {
    if (s.rsi <= RSI_OVERSOLD) n += 40 + (RSI_OVERSOLD - s.rsi)
    else if (s.rsi >= RSI_OVERBOUGHT) n += 40 + (s.rsi - RSI_OVERBOUGHT)
  }
  if (isNum(s.day_change_pct) && Math.abs(s.day_change_pct) >= BIG_MOVE_PCT) {
    n += 20 + Math.abs(s.day_change_pct)
  }
  return n
}

function WatchlistPanel({ rows, marketOpen }) {
  const [sortBy, setSortBy] = useState('attention')
  const clean = useMemo(
    () => (Array.isArray(rows) ? rows : []).filter(r => r && okPrice(r.price)),
    [rows])

  const sorted = useMemo(() => {
    const f = {
      attention: (a, b) => attentionOf(b) - attentionOf(a),
      day: (a, b) => (b.day_change_pct ?? 0) - (a.day_change_pct ?? 0),
      week: (a, b) => (b.week_change_pct ?? 0) - (a.week_change_pct ?? 0),
      month: (a, b) => (b.month_change_pct ?? 0) - (a.month_change_pct ?? 0),
      rsi: (a, b) => (b.rsi ?? 0) - (a.rsi ?? 0),
      symbol: (a, b) => String(a.symbol || '').localeCompare(String(b.symbol || '')),
    }
    return [...clean].sort(f[sortBy] || f.attention)
  }, [clean, sortBy])

  if (!clean.length) {
    return (
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl px-4 py-6 text-center">
        <p className="text-neutral-300 text-sm">
          Add names to your <Link to="/watchlist" className="text-white underline hover:text-neutral-300">Watchlist</Link>{' '}
          to see your own book summarised here.
        </p>
      </div>
    )
  }

  const dayMoves = clean.map(s => s.day_change_pct).filter(isNum)
  const avgDay = dayMoves.length ? dayMoves.reduce((a, b) => a + b, 0) / dayMoves.length : null
  const upCount = dayMoves.filter(v => v > 0).length
  const flagged = clean.filter(s => attentionOf(s) > 0)

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-bold text-white">Your Watchlist</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            {clean.length} names · {upCount} up on the session · average move {pct(avgDay)}.
            Sorted by what needs a decision, not alphabetically.
          </p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-2xl font-bold font-mono ${flagged.length ? 'text-white' : 'text-neutral-600'}`}>{flagged.length}</div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Flagged</div>
        </div>
      </div>

      <div className="px-4 py-2 border-b border-neutral-800/60 flex items-center gap-1 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold mr-1">Sort</span>
        {Object.entries(WL_SORTS).map(([k, v]) => (
          <button
            key={k}
            onClick={() => setSortBy(k)}
            className={`px-2 py-1 rounded border text-[10px] font-bold transition-colors ${sortBy === k ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
          >
            {v.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
        <div className="col-span-2">Symbol</div>
        <div className="col-span-2 text-right">Price</div>
        <div className="col-span-1 text-right">{marketOpen ? 'Today' : 'Last'}</div>
        <div className="col-span-1 text-right">Week</div>
        <div className="col-span-1 text-right">Month</div>
        <div className="col-span-1 text-right">RSI</div>
        <div className="col-span-4">Read</div>
      </div>

      {sorted.map((s, i) => {
        const flag = attentionOf(s) > 0
        const rsiCol = !isNum(s.rsi) ? 'text-neutral-500'
          : s.rsi <= RSI_OVERSOLD ? 'text-emerald-400'
          : s.rsi >= RSI_OVERBOUGHT ? 'text-rose-400' : 'text-neutral-300'
        return (
          <div key={s.symbol || i} className="grid grid-cols-12 gap-2 px-4 py-2.5 border-b border-neutral-800/40 last:border-b-0 items-center text-xs hover:bg-neutral-800/20">
            <div className="col-span-2 flex items-center gap-1.5 min-w-0">
              {flag && <span className="w-1 h-4 bg-white rounded-full shrink-0" />}
              <Link to={`/symbol/${encodeURIComponent(s.symbol || '')}`} className="font-mono font-bold text-sm text-white hover:underline truncate">
                {s.symbol || '—'}
              </Link>
            </div>
            <div className="col-span-2 text-right font-mono text-neutral-100">{px(s.price)}</div>
            <div className={`col-span-1 text-right font-mono font-bold ${chgText(s.day_change_pct)}`}>{pct(s.day_change_pct, 1)}</div>
            <div className={`col-span-1 text-right font-mono ${chgText(s.week_change_pct)}`}>{pct(s.week_change_pct, 1)}</div>
            <div className={`col-span-1 text-right font-mono ${chgText(s.month_change_pct)}`}>{pct(s.month_change_pct, 1)}</div>
            <div className={`col-span-1 text-right font-mono ${rsiCol}`}>{isNum(s.rsi) ? s.rsi.toFixed(0) : '—'}</div>
            <div className="col-span-4 text-[10px] text-neutral-500 leading-tight truncate">{s.signal || 'No read'}</div>
          </div>
        )
      })}

      <div className="px-4 py-2.5 text-[10px] text-neutral-600 leading-snug">
        Flagged = 14-day RSI at or beyond {RSI_OVERSOLD}/{RSI_OVERBOUGHT}, or a single-session move of {BIG_MOVE_PCT}%+.
        An RSI extreme is a condition, not a signal — it tells you to look, not to act. Momentum can stay
        overbought for months.
      </div>
    </div>
  )
}

// ─── Data layer ───────────────────────────────────────────────────────────────
const CACHE_KEY = 'dailySummary:v1'
const REFRESH_MS = 5 * 60 * 1000   // matches the backend's 5-minute cache on both legs
const REQ_TIMEOUT = 20000

function getWatchlistTickers() {
  try {
    const raw = localStorage.getItem('sentinel_quant_watchlist')
    if (!raw) return ''
    const list = JSON.parse(raw)
    if (!Array.isArray(list)) return ''
    return [...new Set(
      list.map(s => s && typeof s.ticker === 'string' ? s.ticker.toUpperCase().trim() : null)
        .filter(t => t && /^[A-Z][A-Z.\-]{0,6}$/.test(t))
    )].slice(0, 60).join(',')
  } catch { return '' }
}

async function fetchJSON(url, timeout = REQ_TIMEOUT) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const r = await fetch(url, { signal: ctrl.signal })
    if (!r.ok) return null
    const j = await r.json()
    return (j && typeof j === 'object') ? j : null
  } catch { return null } finally { clearTimeout(timer) }
}

export default function DailySummary() {
  const cached = (() => {
    try { const r = localStorage.getItem(CACHE_KEY); return r ? JSON.parse(r) : null } catch { return null }
  })()
  const [data, setData] = useState(cached?.data || null)
  const [loading, setLoading] = useState(!cached?.data)
  const [refreshing, setRefreshing] = useState(false)
  const [stale, setStale] = useState(false)
  const inFlight = useRef(false)

  async function load() {
    if (inFlight.current) return        // never stack requests
    inFlight.current = true
    setRefreshing(true)
    const wl = getWatchlistTickers()
    const url = wl ? `/api/daily-summary?watchlist=${encodeURIComponent(wl)}` : '/api/daily-summary'
    const j = await fetchJSON(url)
    if (j && (Array.isArray(j.gainers) || Array.isArray(j.losers))) {
      setData(j)
      setStale(false)
      try { localStorage.setItem(CACHE_KEY, JSON.stringify({ data: j, ts: Date.now() })) } catch {}
    } else {
      setStale(true)    // keep last-good on screen
    }
    setLoading(false)
    setRefreshing(false)
    inFlight.current = false
  }

  useEffect(() => {
    load()
    // Visibility-gated: a backgrounded tab must not keep polling Yahoo.
    const id = setInterval(() => { if (document.visibilityState === 'visible') load() }, REFRESH_MS)
    const onVis = () => {
      if (document.visibilityState !== 'visible') return
      let ts = 0
      try { ts = (JSON.parse(localStorage.getItem(CACHE_KEY) || '{}').ts) || 0 } catch {}
      if (Date.now() - ts > REFRESH_MS) load()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearInterval(id); document.removeEventListener('visibilitychange', onVis) }
  }, [])

  const sessionLabel = useMemo(() => {
    if (!data?.trading_date || typeof data.trading_date !== 'string') return null
    try {
      const [y, m, d] = data.trading_date.split('-').map(Number)
      if (![y, m, d].every(Number.isFinite)) return null
      const td = new Date(y, m - 1, d); td.setHours(0, 0, 0, 0)
      const today = new Date(); today.setHours(0, 0, 0, 0)
      const diff = Math.round((today - td) / 86400000)
      if (diff === 0) return "today's session"
      if (diff === 1) return "yesterday's session"
      return `the session of ${td.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' })}`
    } catch { return null }
  }, [data])

  const hasMovers = (Array.isArray(data?.gainers) && data.gainers.length) ||
                    (Array.isArray(data?.losers) && data.losers.length)

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6 flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-black text-white tracking-tight">Daily Summary</h1>
          <p className="text-sm text-neutral-400 mt-1">
            Breadth, dispersion and the extremes of {sessionLabel || 'the last completed session'}, plus your own book.
            One cached request serves the whole page — quotes are validated before they are shown, never guessed.
          </p>
        </div>
        <div className="text-right">
          <button
            onClick={load}
            disabled={refreshing}
            className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 rounded-lg text-sm font-medium text-neutral-200 transition-colors"
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
          {data?.generated_at && (
            <div className="text-[10px] text-neutral-600 mt-1">
              as of {new Date(data.generated_at).toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>

      {stale && data && (
        <div className="mb-4 bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-4 py-2.5 text-xs text-neutral-300">
          Last refresh failed — showing the previous good pull rather than an empty page. Figures may be a few
          minutes behind.
        </div>
      )}

      {loading && !data ? (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Pulling the session…
        </div>
      ) : !hasMovers ? (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center">
          <p className="text-neutral-300 font-bold">No validated session data.</p>
          <p className="text-neutral-500 text-sm mt-1">
            {typeof data?.error === 'string' ? data.error : 'Both the primary and fallback quote sources returned nothing usable.'}{' '}
            Nothing is inferred from missing data — no breadth or mood is reported.
          </p>
          <button onClick={load} className="mt-3 text-xs underline text-neutral-400 hover:text-white">Retry</button>
        </div>
      ) : (
        <div className="space-y-6">
          <PanelBoundary label="Session Read"><SessionRead data={data} /></PanelBoundary>

          <PanelBoundary label="Watchlist">
            <WatchlistPanel rows={data?.watchlist_summary} marketOpen={!!data?.market_open} />
          </PanelBoundary>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <PanelBoundary label="Leaders">
              <MoverTable
                title="Leaders"
                note={data?.market_open ? 'Strongest large caps intraday' : 'Strongest large caps last session'}
                rows={data?.gainers}
                side="up"
              />
            </PanelBoundary>
            <PanelBoundary label="Laggards">
              <MoverTable
                title="Laggards"
                note={data?.market_open ? 'Weakest large caps intraday' : 'Weakest large caps last session'}
                rows={data?.losers}
                side="down"
              />
            </PanelBoundary>
          </div>

          <p className="text-[10px] text-neutral-600 leading-snug">
            Quotes from Yahoo Finance with a CNBC fallback, cached for five minutes on both the mover and watchlist
            legs. Refresh is gated on tab visibility and a single in-flight request, so a page left open does not
            accumulate load. A top-mover list is a description of the past; the breadth and dispersion numbers above
            are the part that should change how you size and select. Not financial advice.
          </p>
        </div>
      )}
    </div>
  )
}
