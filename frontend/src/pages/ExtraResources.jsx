import { useState, useEffect, useRef, useMemo, Component } from 'react'
import { Link } from 'react-router-dom'
import CompoundInterestCalc from '../components/CompoundInterestCalc'
import SectorHeatmap from '../components/SectorHeatmap'

// EXTRA RESOURCES — event risk and tools.
//
// This page used to re-render the market-sentiment panel and headline list that
// the News page already owns. That duplication cost a second /api/market-news
// request on every visit and told you nothing new, so it is gone: News owns the
// wires, this page owns the calendar.
//
// What is left is the part that actually changes decisions. An earnings date is
// a binary event with a known timestamp — the one risk on the board you can see
// coming and simply choose not to stand in front of. The options engine already
// refuses to sell you a long call inside three days of a print because implied
// vol collapses after the report and a directionally-correct option still
// loses. This page surfaces that same window before you get there, using the
// same thresholds.
//
// One cached request (6-hour TTL server-side). Watchlist cross-reference is
// read from localStorage — no extra calls.

class PanelBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(e) { try { console.error('Resources panel error:', e) } catch {} }
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

// Thresholds mirror backend/predictions/options_recommendation.py exactly.
// If those constants change, change these — a calendar that disagrees with the
// engine gating the trades is worse than no calendar.
const EARN_BLACKOUT_DAYS = 3
const EARN_WARN_DAYS = 21

function daysUntil(iso) {
  if (typeof iso !== 'string') return null
  const parts = iso.slice(0, 10).split('-').map(Number)
  if (parts.length !== 3 || !parts.every(Number.isFinite)) return null
  const d = new Date(parts[0], parts[1] - 1, parts[2]); d.setHours(0, 0, 0, 0)
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const n = Math.round((d - today) / 86400000)
  return Number.isFinite(n) ? n : null
}

function fmtDate(iso) {
  const parts = typeof iso === 'string' ? iso.slice(0, 10).split('-').map(Number) : []
  if (parts.length !== 3 || !parts.every(Number.isFinite)) return iso || '—'
  const d = new Date(parts[0], parts[1] - 1, parts[2])
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
}

// The endpoint serves its last-known payload when the cache has expired and a
// background refresh is still running, so a response can legitimately be days
// old. Past-dated rows are dropped elsewhere; the danger is the leftover empty
// table reading as "the calendar is clear" when it only means "we do not know".
const STALE_HOURS = 24

function payloadAgeHours(iso) {
  if (typeof iso !== 'string') return null
  // The backend emits a naive datetime.now().isoformat(); the container runs
  // UTC, so a stamp with no offset must be read as UTC. Only inspect the time
  // portion for an offset — the date portion always contains hyphens.
  const timePart = iso.slice(11)
  const hasZone = /[Zz]$/.test(iso) || /[+\-]\d{2}:?\d{2}$/.test(timePart)
  const t = Date.parse(hasZone ? iso : iso + 'Z')
  if (!Number.isFinite(t)) return null
  const h = (Date.now() - t) / 3600000
  return h >= 0 ? h : 0
}

function getWatchlistSet() {
  try {
    const raw = localStorage.getItem('sentinel_quant_watchlist')
    if (!raw) return new Set()
    const list = JSON.parse(raw)
    if (!Array.isArray(list)) return new Set()
    return new Set(list.map(s => s && typeof s.ticker === 'string' ? s.ticker.toUpperCase().trim() : null).filter(Boolean))
  } catch { return new Set() }
}

// ─── Earnings calendar ────────────────────────────────────────────────────────
function EarningsRow({ e, owned }) {
  const d = daysUntil(e.date)
  const blackout = isNum(d) && d <= EARN_BLACKOUT_DAYS
  const warn = isNum(d) && d > EARN_BLACKOUT_DAYS && d <= EARN_WARN_DAYS
  return (
    <div className="grid grid-cols-12 gap-2 px-4 py-2.5 border-b border-neutral-800/40 last:border-b-0 items-center text-xs hover:bg-neutral-800/20">
      <div className="col-span-3 flex items-center gap-1.5 min-w-0">
        {owned && <span className="w-1 h-4 bg-white rounded-full shrink-0" title="On your watchlist" />}
        <Link to={`/symbol/${encodeURIComponent(e.symbol || '')}`} className="font-mono font-bold text-sm text-white hover:underline truncate">
          {e.symbol || '—'}
        </Link>
      </div>
      <div className="col-span-3 text-[11px] text-neutral-500 truncate">{e.name || ''}</div>
      <div className="col-span-2 text-neutral-200 font-mono text-[11px]">{fmtDate(e.date)}</div>
      <div className={`col-span-1 text-right font-mono font-bold ${blackout ? 'text-rose-400' : warn ? 'text-neutral-200' : 'text-neutral-400'}`}>
        {isNum(d) ? (d === 0 ? 'today' : `${d}d`) : '—'}
      </div>
      <div className="col-span-1 text-right font-mono text-neutral-300">
        {isNum(e.eps_estimate) ? '$' + e.eps_estimate.toFixed(2) : '—'}
      </div>
      <div className="col-span-2 text-right">
        {blackout ? (
          <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border text-rose-400 border-rose-600/50 bg-rose-600/10">
            No long premium
          </span>
        ) : warn ? (
          <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded border text-neutral-300 border-neutral-600/50 bg-neutral-800/50">
            Size down
          </span>
        ) : (
          <span className="text-[9px] text-neutral-600">clear</span>
        )}
      </div>
    </div>
  )
}

function EarningsCalendar({ data, loading, onRetry }) {
  const watchlist = useMemo(() => getWatchlistSet(), [])
  const rows = useMemo(() => {
    const list = Array.isArray(data?.earnings) ? data.earnings : []
    return list
      .filter(e => e && typeof e.symbol === 'string')
      .map(e => ({ ...e, _d: daysUntil(e.date), _owned: watchlist.has(String(e.symbol).toUpperCase()) }))
      .filter(e => isNum(e._d) && e._d >= 0)
      .sort((a, b) => a._d - b._d)
  }, [data, watchlist])

  const blackout = rows.filter(e => e._d <= EARN_BLACKOUT_DAYS)
  const owned = rows.filter(e => e._owned)
  const thisWeek = rows.filter(e => e._d <= 7)

  const ageH = payloadAgeHours(data?.generated_at)
  const warming = data?.cache_status === 'warming'
  const stale = warming || (isNum(ageH) && ageH > STALE_HOURS)

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-bold text-white">Event Risk — Earnings</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            The only major risk on the board with a known timestamp. A good chart does not survive a bad print,
            and implied vol collapses the morning after — which is why the options engine withholds long premium
            inside {EARN_BLACKOUT_DAYS} days of a report.
          </p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-2xl font-bold font-mono ${blackout.length ? 'text-rose-400' : 'text-emerald-400'}`}>{blackout.length}</div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">In blackout</div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-neutral-800/60 border-b border-neutral-800/60">
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Reporting ≤ 14d</div>
          <div className="text-2xl font-bold font-mono text-neutral-100">{rows.length}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">of {data?.stocks_checked ?? '—'} tracked bellwethers</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Next 7 Days</div>
          <div className={`text-2xl font-bold font-mono ${thisWeek.length ? 'text-white' : 'text-neutral-600'}`}>{thisWeek.length}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Inside most option expiries</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">On Your Watchlist</div>
          <div className={`text-2xl font-bold font-mono ${owned.length ? 'text-white' : 'text-neutral-600'}`}>{owned.length}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">
            {owned.length ? owned.slice(0, 3).map(e => e.symbol).join(', ') : 'None of your names report'}
          </div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Soonest</div>
          <div className="text-2xl font-bold font-mono text-neutral-100">
            {rows.length ? (rows[0]._d === 0 ? 'Today' : rows[0]._d + 'd') : '—'}
          </div>
          <div className="text-[10px] text-neutral-600 mt-0.5">{rows.length ? rows[0].symbol : 'Calendar clear'}</div>
        </div>
      </div>

      {stale && (
        <div className="px-4 py-2 border-b border-neutral-800/60 bg-neutral-900/60 flex items-center justify-between gap-3 flex-wrap">
          <p className="text-[11px] text-neutral-300">
            {warming
              ? 'Calendar is rebuilding server-side. This panel will fill in on its own within a minute.'
              : `Calendar data is ${isNum(ageH) ? (ageH < 48 ? Math.round(ageH) + 'h' : Math.round(ageH / 24) + 'd') : 'significantly'} old and a refresh is running. Treat an empty table as unknown, not as clear.`}
          </p>
          <button onClick={onRetry} className="text-[11px] underline text-neutral-400 hover:text-white shrink-0">
            Refresh now
          </button>
        </div>
      )}

      {loading && !rows.length ? (
        <div className="px-4 py-8 text-center text-sm text-neutral-500">Loading the calendar…</div>
      ) : !rows.length ? (
        <div className="px-4 py-6 text-center">
          <p className="text-neutral-300 text-sm font-bold">
            {stale ? 'Calendar unavailable — no confirmed dates to show.' : 'No bellwether earnings in the next 14 days.'}
          </p>
          <p className="text-neutral-500 text-xs mt-1">
            {stale
              ? 'This is missing data, not an all-clear. Do not read it as a clear window.'
              : 'Either the window is genuinely clear or the upstream calendar returned nothing — no date is inferred either way.'}{' '}
            Per-ticker earnings dates still gate every options recommendation independently.
          </p>
          <button onClick={onRetry} className="mt-2 text-xs underline text-neutral-400 hover:text-white">Retry</button>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
            <div className="col-span-3">Symbol</div>
            <div className="col-span-3">Company</div>
            <div className="col-span-2">Reports</div>
            <div className="col-span-1 text-right">In</div>
            <div className="col-span-1 text-right">EPS Est</div>
            <div className="col-span-2 text-right">Options Stance</div>
          </div>
          {rows.map((e, i) => <EarningsRow key={`${e.symbol}-${i}`} e={e} owned={e._owned} />)}
          <div className="px-4 py-2.5 text-[10px] text-neutral-600 leading-snug">
            "No long premium" = reports within {EARN_BLACKOUT_DAYS} days; the engine will not open a long call or put
            on these because the post-report vol crush can lose money on a correct directional call.
            "Size down" = reports within {EARN_WARN_DAYS} days; tradeable, but implied vol is already bid and the
            position carries a binary event. Selling premium is the structure that benefits from this, not buying it.
            A white bar marks a name on your watchlist. Dates are estimates from the exchange calendar and do move —
            confirm with the company before putting risk on.
          </div>
        </>
      )}
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────
const REQ_TIMEOUT = 20000

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

// On a cold or expired cache the endpoint answers immediately — either with
// cache_status "warming" and an empty list, or with its last-known payload,
// which can be days old — and kicks off a background rebuild that takes a
// couple of minutes. Both cases need a bounded retry, otherwise the calendar
// shows nothing (or stale rows that get filtered to nothing) until a reload.
const WARM_RETRIES = 6
const WARM_RETRY_MS = 20000

export default function ExtraResources() {
  const [earnings, setEarnings] = useState(null)
  const [loading, setLoading] = useState(true)
  const inFlight = useRef(false)
  const warmTimer = useRef(null)
  const warmTries = useRef(0)

  async function load() {
    if (inFlight.current) return
    inFlight.current = true
    const j = await fetchJSON('/api/earnings-calendar')
    if (j) setEarnings(j)
    setLoading(false)
    inFlight.current = false
    const age = payloadAgeHours(j?.generated_at)
    const needsRefresh = j?.cache_status === 'warming' || (isNum(age) && age > STALE_HOURS)
    if (needsRefresh && warmTries.current < WARM_RETRIES) {
      warmTries.current += 1
      warmTimer.current = setTimeout(load, WARM_RETRY_MS)
    }
  }

  // Single fetch on mount (plus the cold-start retries above). The backend
  // caches this for six hours, so steady-state polling has cost and no benefit.
  useEffect(() => {
    load()
    return () => { if (warmTimer.current) clearTimeout(warmTimer.current) }
  }, [])

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <h1 className="text-3xl font-black text-white tracking-tight">Resources</h1>
        <p className="text-sm text-neutral-400 mt-1">
          Sector rotation, the earnings event calendar, and a compounding model. For headline sentiment and the
          macro wire read, see <Link to="/news" className="text-white underline hover:text-neutral-300">News</Link> —
          it is not duplicated here.
        </p>
      </div>

      <div className="space-y-6">
        <PanelBoundary label="Sector heatmap"><SectorHeatmap /></PanelBoundary>

        <PanelBoundary label="Earnings calendar">
          <EarningsCalendar
            data={earnings}
            loading={loading}
            onRetry={() => { warmTries.current = 0; load() }}
          />
        </PanelBoundary>

        <PanelBoundary label="Compounding model"><CompoundInterestCalc /></PanelBoundary>

        <p className="text-[10px] text-neutral-600 leading-snug">
          Calendar data from the Yahoo Finance exchange calendar, cached six hours server-side and fetched once per
          page load. Earnings dates are company estimates and are frequently revised — this page is a planning tool,
          not a confirmation. Not financial advice.
        </p>
      </div>
    </div>
  )
}
