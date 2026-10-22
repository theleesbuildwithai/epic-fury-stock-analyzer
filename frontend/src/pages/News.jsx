import { useState, useEffect, useRef, useMemo, Component } from 'react'

// MARKET NEWS — the tape read, not a headline dump.
//
// Everything on this page is derived from ONE request to /api/market-news,
// which the backend already caches for 5 minutes. No per-headline calls, no
// per-sector calls, no fan-out. The payload already carries recency-weighted
// sentiment, multi-source confirmation and per-sector scores; the old page
// rendered almost none of it. This one uses all of it and adds nothing to the
// request budget.
//
// Refresh is gated on tab visibility and on a single in-flight guard, so a
// backgrounded tab left open overnight cannot quietly hammer the endpoint.

// ─── Per-panel error boundary: one bad field can never blank the page ─────────
class PanelBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(e) { try { console.error('News panel error:', e) } catch {} }
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
const sentText = (s) => !isNum(s) ? 'text-neutral-500'
  : s > 0.1 ? 'text-emerald-400' : s > 0 ? 'text-emerald-500/70'
  : s < -0.1 ? 'text-rose-400' : s < 0 ? 'text-rose-500/70' : 'text-neutral-500'
const sentBar = (s) => !isNum(s) ? 'bg-neutral-700'
  : s > 0.03 ? 'bg-emerald-500' : s < -0.03 ? 'bg-rose-500' : 'bg-neutral-500'
const sentLabel = (s) => {
  if (!isNum(s)) return 'Unscored'
  if (s > 0.3) return 'Very Bullish'
  if (s > 0.1) return 'Bullish'
  if (s > 0) return 'Slightly Bullish'
  if (s < -0.3) return 'Very Bearish'
  if (s < -0.1) return 'Bearish'
  if (s < 0) return 'Slightly Bearish'
  return 'Neutral'
}
const labelText = (l) => typeof l === 'string' && l.includes('Bullish') ? 'text-emerald-400'
  : typeof l === 'string' && l.includes('Bearish') ? 'text-rose-400' : 'text-neutral-300'
const ageText = (h) => {
  if (!isNum(h) || h >= 999) return ''
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`
  if (h < 48) return `${Math.round(h)}h ago`
  return `${Math.round(h / 24)}d ago`
}
const SOURCE_ORDER = ['Yahoo Finance', 'Bloomberg', 'CNBC', 'CNN']

function Chip({ children, tone = 'neutral' }) {
  const map = {
    neutral: 'bg-neutral-900 border-neutral-700 text-neutral-300',
    green: 'bg-emerald-950/50 border-emerald-800/40 text-emerald-300',
    red: 'bg-rose-950/50 border-rose-800/40 text-rose-300',
    white: 'bg-white/10 border-white/25 text-white',
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-bold ${map[tone] || map.neutral}`}>
      {children}
    </span>
  )
}

// ─── Tape read ────────────────────────────────────────────────────────────────
// Headline sentiment is a weak, fast signal. It is useful for confirming or
// questioning a position you already hold — it is not a reason to open one.
// Scored on recency-weighted keyword analysis across four approved wires.
// ─── System stance ────────────────────────────────────────────────────────────
// The autonomous site-wide verdict from the backend decision engine. Falls back
// to a client-side derivation from the tape when an older cached payload lacks
// the field — the strip never renders a stance it cannot justify.
function SystemStance({ news }) {
  let stance = news?.market_stance
  if (!stance || !stance.stance) {
    const s = Number(news?.market_sentiment?.score)
    if (!isFinite(s)) return null
    stance = s >= 0.1
      ? { stance: 'RISK_ON', notes: [], instruction: 'Favor long exposure. Lean into the strongest technical setups; the tape supports risk.' }
      : s <= -0.1
        ? { stance: 'RISK_OFF', notes: [], instruction: 'Defense first. No new longs into weakness; tighten stops until the tape clears.' }
        : { stance: 'NEUTRAL', notes: [], instruction: 'Mixed tape. Trade single-name setups only — no market-direction bets.' }
  }
  const on = stance.stance === 'RISK_ON'
  const off = stance.stance === 'RISK_OFF'
  const cls = on
    ? 'bg-emerald-950/25 border-emerald-700/40 text-emerald-300'
    : off
      ? 'bg-rose-950/25 border-rose-700/40 text-rose-300'
      : 'bg-neutral-900/40 border-neutral-700 text-neutral-200'
  return (
    <div className={`border rounded-xl px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-1 ${cls}`}>
      <div>
        <span className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 block">System Decision</span>
        <span className="text-xl font-black">{stance.stance.replace('_', ' ')}</span>
      </div>
      <div className="h-8 w-px bg-neutral-800 hidden sm:block" />
      <div className="flex-1 min-w-[220px]">
        <p className="text-sm text-neutral-300">{stance.instruction}</p>
        {Array.isArray(stance.notes) && stance.notes.length > 0 && (
          <p className="text-[10px] text-neutral-500 mt-0.5">{stance.notes.join(' · ')}</p>
        )}
      </div>
    </div>
  )
}

function TapeRead({ news }) {
  const ms = news?.market_sentiment || {}
  const heads = Array.isArray(news?.headlines) ? news.headlines : []
  const fresh = heads.filter(h => isNum(h?.age_hours) && h.age_hours < 6).length
  const confirmed = heads.filter(h => h?.multi_source_confirmed).length
  const bull = isNum(ms.bullish_pct) ? ms.bullish_pct : 0
  const bear = isNum(ms.bearish_pct) ? ms.bearish_pct : 0
  const neut = Math.max(0, 100 - bull - bear)
  const net = bull - bear

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60">
        <h2 className="text-lg font-bold text-white">Tape Read</h2>
        <p className="text-xs text-neutral-500 mt-0.5">
          Recency-weighted sentiment across {news?.total_fetched ?? heads.length} deduplicated headlines from{' '}
          {(Array.isArray(news?.sources) ? news.sources.length : 0)} wires. A confirming signal for positions you hold —
          not a reason to open one.
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-neutral-800/60 border-b border-neutral-800/60">
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Overall Tone</div>
          <div className={`text-xl font-bold ${labelText(ms.label)}`}>{ms.label || '—'}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">
            avg weighted score {isNum(ms.score) ? (ms.score > 0 ? '+' : '') + ms.score.toFixed(3) : '—'}
          </div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Net Breadth</div>
          <div className={`text-2xl font-bold font-mono ${net > 5 ? 'text-emerald-400' : net < -5 ? 'text-rose-400' : 'text-neutral-100'}`}>
            {net > 0 ? '+' : ''}{net}
          </div>
          <div className="text-[10px] text-neutral-600 mt-0.5">{bull}% bullish vs {bear}% bearish</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Fresh &lt; 6h</div>
          <div className={`text-2xl font-bold font-mono ${fresh ? 'text-white' : 'text-neutral-600'}`}>{fresh}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Carry 1.5–2× the weight</div>
        </div>
        <div className="px-4 py-3">
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Multi-Source</div>
          <div className={`text-2xl font-bold font-mono ${confirmed ? 'text-white' : 'text-neutral-600'}`}>{confirmed}</div>
          <div className="text-[10px] text-neutral-600 mt-0.5">Corroborated by 2+ wires</div>
        </div>
      </div>

      <div className="px-4 py-3">
        <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500 mb-2">Sentiment Distribution</div>
        <div className="flex h-2 rounded-full overflow-hidden bg-neutral-800 mb-2">
          {bull > 0 && <div className="bg-emerald-500" style={{ width: `${bull}%` }} />}
          {neut > 0 && <div className="bg-neutral-500" style={{ width: `${neut}%` }} />}
          {bear > 0 && <div className="bg-rose-500" style={{ width: `${bear}%` }} />}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          {[['Bullish', bull, 'bg-emerald-500', 'text-emerald-400'],
            ['Neutral', neut, 'bg-neutral-500', 'text-neutral-300'],
            ['Bearish', bear, 'bg-rose-500', 'text-rose-400']].map(([l, v, bar, txt]) => (
            <div key={l} className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-sm ${bar}`} />
              <span className="text-[10px] text-neutral-400">{l}</span>
              <span className={`text-[10px] font-mono font-bold ${txt}`}>{v}%</span>
            </div>
          ))}
        </div>
        {Math.abs(net) < 6 && (
          <div className="mt-2 text-[11px] text-neutral-500">
            Breadth is split — the wires are not pointing anywhere. Read this as no information, not as balance.
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Sector tone ──────────────────────────────────────────────────────────────
// Where the news flow is actually pointing. Thin coverage is flagged, because
// a "sector read" built on two headlines is noise wearing a conclusion's hat.
const THIN_COVERAGE = 3

function SectorTone({ news }) {
  const rows = useMemo(() => {
    const ss = news?.sector_sentiment
    if (!ss || typeof ss !== 'object') return []
    return Object.entries(ss)
      .filter(([, v]) => v && isNum(v.score))
      .map(([name, v]) => ({ name, score: v.score, count: v.headline_count || 0, label: v.label }))
      .sort((a, b) => b.score - a.score)
  }, [news])

  if (!rows.length) return null
  const max = Math.max(...rows.map(r => Math.abs(r.score)), 0.1)
  const thin = rows.filter(r => r.count < THIN_COVERAGE).length

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60">
        <h2 className="text-lg font-bold text-white">Sector Tone</h2>
        <p className="text-xs text-neutral-500 mt-0.5">
          Where the news flow is pointing, by sector. Cross-check this against a position before you add to it —
          a long into a sector the wires are selling needs a better reason than the chart.
        </p>
      </div>
      <div className="px-4 py-3 space-y-1.5">
        {rows.map(r => (
          <div key={r.name} className="flex items-center gap-3">
            <span className="text-[11px] text-neutral-300 w-40 shrink-0 truncate">{r.name}</span>
            <span className="flex-1 flex items-center h-2">
              <span className="w-1/2 flex justify-end">
                {r.score < 0 && (
                  <span className="h-2 bg-rose-500 rounded-l-sm" style={{ width: `${Math.abs(r.score) / max * 100}%` }} />
                )}
              </span>
              <span className="w-px h-3 bg-neutral-700 shrink-0" />
              <span className="w-1/2">
                {r.score > 0 && (
                  <span className="block h-2 bg-emerald-500 rounded-r-sm" style={{ width: `${r.score / max * 100}%` }} />
                )}
              </span>
            </span>
            <span className={`text-[11px] font-mono font-bold w-16 text-right shrink-0 ${sentText(r.score)}`}>
              {r.score > 0 ? '+' : ''}{r.score.toFixed(3)}
            </span>
            <span className={`text-[10px] font-mono w-16 text-right shrink-0 ${r.count < THIN_COVERAGE ? 'text-neutral-600' : 'text-neutral-500'}`}>
              {r.count} hl
            </span>
          </div>
        ))}
      </div>
      {thin > 0 && (
        <div className="px-4 pb-3 text-[10px] text-neutral-600">
          {thin} sector{thin !== 1 ? 's' : ''} scored on fewer than {THIN_COVERAGE} headlines — treat those rows as
          anecdote, not signal.
        </div>
      )}
    </div>
  )
}

// ─── Macro watch ──────────────────────────────────────────────────────────────
// Macro repriced the whole book at once. These get their own panel because a
// single-stock headline is an opinion and a macro headline is a discount rate.
function MacroWatch({ news }) {
  const events = (Array.isArray(news?.macro_events) ? news.macro_events : []).slice(0, 8)
  if (!events.length) return null
  const scores = events.map(e => e?.weighted_sentiment).filter(isNum)
  const avg = scores.length ? scores.reduce((s, v) => s + v, 0) / scores.length : null

  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-white">Macro Watch</h2>
          <p className="text-xs text-neutral-500 mt-0.5">
            Rates, inflation, policy, trade. A single-stock headline is an opinion; a macro headline reprices
            every position you hold at once.
          </p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-xl font-bold font-mono ${sentText(avg)}`}>
            {isNum(avg) ? (avg > 0 ? '+' : '') + avg.toFixed(2) : '—'}
          </div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Macro tone</div>
        </div>
      </div>
      <div>
        {events.map((e, i) => (
          <a
            key={i}
            href={typeof e?.link === 'string' && /^https?:\/\//.test(e.link) ? e.link : undefined}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-3 px-4 py-2 border-b border-neutral-800/40 last:border-b-0 hover:bg-neutral-800/20 transition-colors"
          >
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${sentBar(e?.weighted_sentiment)}`} />
            <span className="flex-1 min-w-0 text-[12px] text-neutral-200 truncate">{e?.title || '—'}</span>
            <span className="text-[10px] text-neutral-600 shrink-0">{e?.source || ''}</span>
            <span className={`text-[10px] font-mono font-bold w-12 text-right shrink-0 ${sentText(e?.weighted_sentiment)}`}>
              {isNum(e?.weighted_sentiment) ? (e.weighted_sentiment > 0 ? '+' : '') + e.weighted_sentiment.toFixed(2) : '—'}
            </span>
          </a>
        ))}
      </div>
    </div>
  )
}

// ─── Headline row ─────────────────────────────────────────────────────────────
function HeadlineRow({ h }) {
  const safeLink = typeof h?.link === 'string' && /^https?:\/\//.test(h.link) ? h.link : undefined
  const w = h?.weighted_sentiment
  const raw = h?.sentiment
  return (
    <a
      href={safeLink}
      target="_blank"
      rel="noopener noreferrer"
      className="block px-4 py-3 border-b border-neutral-800/40 last:border-b-0 hover:bg-neutral-800/20 transition-colors group"
    >
      <div className="flex items-start gap-3">
        <span className={`w-1 self-stretch rounded-full shrink-0 ${sentBar(w)}`} />
        <div className="flex-1 min-w-0">
          <p className="text-[13px] text-neutral-100 font-medium leading-snug group-hover:text-white">
            {h?.title || '—'}
          </p>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <Chip tone="neutral">{h?.source || '—'}</Chip>
            {h?.is_macro && <Chip tone="white">MACRO</Chip>}
            {h?.multi_source_confirmed && <Chip tone="white">2+ WIRES</Chip>}
            {isNum(h?.age_hours) && h.age_hours < 1 && <Chip tone="white">BREAKING</Chip>}
            <span className="text-[10px] text-neutral-600">{ageText(h?.age_hours) || h?.pub_date || ''}</span>
            {isNum(h?.recency_weight) && (
              <span className="text-[10px] text-neutral-600">· {h.recency_weight}× recency</span>
            )}
          </div>
        </div>
        <div className="text-right shrink-0 w-24">
          <div className={`text-[12px] font-bold ${sentText(w)}`}>{sentLabel(w)}</div>
          <div className={`text-[11px] font-mono ${sentText(w)}`}>
            {isNum(w) ? (w > 0 ? '+' : '') + w.toFixed(2) : '—'}
          </div>
          <div className="text-[9px] text-neutral-600 font-mono">
            raw {isNum(raw) ? (raw > 0 ? '+' : '') + raw.toFixed(2) : '—'}
          </div>
        </div>
      </div>
    </a>
  )
}

// ─── Data layer ───────────────────────────────────────────────────────────────
const CACHE_KEY = 'marketNews:v1'
const REFRESH_MS = 5 * 60 * 1000   // matches the backend's 5-minute news cache exactly
const REQ_TIMEOUT = 15000

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

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'bullish', label: 'Bullish' },
  { key: 'bearish', label: 'Bearish' },
  { key: 'macro', label: 'Macro' },
  { key: 'confirmed', label: '2+ Wires' },
  { key: 'fresh', label: 'Fresh < 6h' },
]
const SORTS = {
  signal: { label: 'Signal strength', fn: (a, b) => Math.abs(b?.weighted_sentiment || 0) - Math.abs(a?.weighted_sentiment || 0) },
  newest: { label: 'Newest first', fn: (a, b) => (isNum(a?.age_hours) ? a.age_hours : 999) - (isNum(b?.age_hours) ? b.age_hours : 999) },
  bullish: { label: 'Most bullish', fn: (a, b) => (b?.weighted_sentiment || 0) - (a?.weighted_sentiment || 0) },
  bearish: { label: 'Most bearish', fn: (a, b) => (a?.weighted_sentiment || 0) - (b?.weighted_sentiment || 0) },
}

export default function News() {
  const cached = (() => {
    try { const r = localStorage.getItem(CACHE_KEY); return r ? JSON.parse(r) : null } catch { return null }
  })()
  const [news, setNews] = useState(cached?.data || null)
  const [loading, setLoading] = useState(!cached?.data)
  const [refreshing, setRefreshing] = useState(false)
  const [stale, setStale] = useState(false)
  const [filter, setFilter] = useState('all')
  const [sortBy, setSortBy] = useState('signal')
  const [sourceFilter, setSourceFilter] = useState('all')
  const inFlight = useRef(false)

  async function load() {
    if (inFlight.current) return          // single in-flight guard: never stack requests
    inFlight.current = true
    setRefreshing(true)
    const j = await fetchJSON('/api/market-news')
    if (j && Array.isArray(j.headlines)) {
      setNews(j)
      setStale(false)
      try { localStorage.setItem(CACHE_KEY, JSON.stringify({ data: j, ts: Date.now() })) } catch {}
    } else {
      // Keep the last-good payload on screen rather than blanking the page.
      setStale(true)
    }
    setLoading(false)
    setRefreshing(false)
    inFlight.current = false
  }

  useEffect(() => {
    load()
    // Visibility-gated interval: a backgrounded tab left open overnight must not
    // keep polling. On refocus we top up once if the data has gone stale.
    const tick = () => { if (document.visibilityState === 'visible') load() }
    const id = setInterval(tick, REFRESH_MS)
    const onVis = () => {
      if (document.visibilityState !== 'visible') return
      let ts = 0
      try { ts = (JSON.parse(localStorage.getItem(CACHE_KEY) || '{}').ts) || 0 } catch {}
      if (Date.now() - ts > REFRESH_MS) load()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearInterval(id); document.removeEventListener('visibilitychange', onVis) }
  }, [])

  const heads = Array.isArray(news?.headlines) ? news.headlines.filter(h => h && typeof h.title === 'string') : []
  const sources = useMemo(() => {
    const present = new Set(heads.map(h => h.source).filter(Boolean))
    return SOURCE_ORDER.filter(s => present.has(s)).concat([...present].filter(s => !SOURCE_ORDER.includes(s)))
  }, [heads])

  const counts = {
    all: heads.length,
    bullish: heads.filter(h => (h.weighted_sentiment || 0) > 0).length,
    bearish: heads.filter(h => (h.weighted_sentiment || 0) < 0).length,
    macro: heads.filter(h => h.is_macro).length,
    confirmed: heads.filter(h => h.multi_source_confirmed).length,
    fresh: heads.filter(h => isNum(h.age_hours) && h.age_hours < 6).length,
  }

  const shown = useMemo(() => {
    const pass = (h) => {
      if (sourceFilter !== 'all' && h.source !== sourceFilter) return false
      if (filter === 'bullish') return (h.weighted_sentiment || 0) > 0
      if (filter === 'bearish') return (h.weighted_sentiment || 0) < 0
      if (filter === 'macro') return !!h.is_macro
      if (filter === 'confirmed') return !!h.multi_source_confirmed
      if (filter === 'fresh') return isNum(h.age_hours) && h.age_hours < 6
      return true
    }
    return heads.filter(pass).sort((SORTS[sortBy] || SORTS.signal).fn)
  }, [heads, filter, sortBy, sourceFilter])

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6 flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-3xl font-black text-white tracking-tight">Market News</h1>
          <p className="text-sm text-neutral-400 mt-1">
            Yahoo Finance, Bloomberg, CNBC and CNN wires, deduplicated and scored with recency weighting and
            multi-source corroboration. One cached request serves this entire page — no per-headline fan-out.
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
          {news?.fetched_at && (
            <div className="text-[10px] text-neutral-600 mt-1">
              wire time {new Date(news.fetched_at).toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>

      {stale && news && (
        <div className="mb-4 bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-4 py-2.5 text-xs text-neutral-300">
          Last refresh failed — showing the previous good wire pull rather than an empty page. Sentiment below may
          be up to a few minutes behind.
        </div>
      )}

      {loading && !news ? (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Pulling the wires…
        </div>
      ) : !heads.length ? (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center">
          <p className="text-neutral-300 font-bold">No headlines available.</p>
          <p className="text-neutral-500 text-sm mt-1">
            Every upstream feed returned empty. Nothing is inferred from silence — no sentiment is reported.
          </p>
          <button onClick={load} className="mt-3 text-xs underline text-neutral-400 hover:text-white">Retry</button>
        </div>
      ) : (
        <div className="space-y-6">
          <PanelBoundary label="System Stance"><SystemStance news={news} /></PanelBoundary>

          <PanelBoundary label="Tape Read"><TapeRead news={news} /></PanelBoundary>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <PanelBoundary label="Sector Tone"><SectorTone news={news} /></PanelBoundary>
            <PanelBoundary label="Macro Watch"><MacroWatch news={news} /></PanelBoundary>
          </div>

          <PanelBoundary label="Headlines">
            <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
              <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60">
                <div className="flex items-center gap-2 flex-wrap">
                  <h2 className="text-lg font-bold text-white">Headlines</h2>
                  <span className="text-xs text-neutral-500">{shown.length} of {heads.length}</span>
                  <div className="ml-auto flex items-center gap-1 flex-wrap">
                    <span className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold mr-1">Sort</span>
                    {Object.entries(SORTS).map(([k, v]) => (
                      <button
                        key={k}
                        onClick={() => setSortBy(k)}
                        className={`px-2 py-1 rounded border text-[10px] font-bold transition-colors ${sortBy === k ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
                      >
                        {v.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-1 flex-wrap mt-2">
                  {FILTERS.map(f => (
                    <button
                      key={f.key}
                      onClick={() => setFilter(f.key)}
                      className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${
                        filter === f.key
                          ? (f.key === 'bullish' ? 'bg-emerald-500 text-black border-emerald-500'
                            : f.key === 'bearish' ? 'bg-rose-500 text-black border-rose-500'
                            : 'bg-white text-black border-white')
                          : `bg-neutral-900 border-neutral-700 hover:bg-neutral-800 ${
                              f.key === 'bullish' ? 'text-emerald-400' : f.key === 'bearish' ? 'text-rose-400' : 'text-neutral-300'}`
                      }`}
                    >
                      {f.label} {counts[f.key]}
                    </button>
                  ))}
                  {sources.length > 1 && (
                    <>
                      <span className="w-px h-4 bg-neutral-700 mx-1" />
                      <button
                        onClick={() => setSourceFilter('all')}
                        className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${sourceFilter === 'all' ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
                      >
                        All wires
                      </button>
                      {sources.map(s => (
                        <button
                          key={s}
                          onClick={() => setSourceFilter(s)}
                          className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${sourceFilter === s ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
                        >
                          {s}
                        </button>
                      ))}
                    </>
                  )}
                </div>
              </div>
              {shown.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-neutral-500">
                  No headlines match this filter.
                </div>
              ) : (
                shown.map((h, i) => <HeadlineRow key={`${h.title?.slice(0, 40)}-${i}`} h={h} />)
              )}
              <div className="px-4 py-2.5 border-t border-neutral-800/60 text-[10px] text-neutral-600 leading-snug">
                Score = keyword sentiment × recency weight (2× under 1h, 1.5× under 6h, 1× under 24h, 0.5× older),
                ×1.5 again when 2+ wires carry the same story. Headline sentiment is a weak, fast, mean-reverting
                signal: use it to question a position, never as the sole reason to take one.
              </div>
            </div>
          </PanelBoundary>
        </div>
      )}
    </div>
  )
}
