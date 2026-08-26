import { useState, useEffect, useRef } from 'react'

// ─── LocalStorage helpers ─────────────────────────────────────────────────────
function getWatchlist() {
  try {
    const data = localStorage.getItem('sentinel_quant_watchlist')
    if (!data) return []
    const parsed = JSON.parse(data)
    if (!Array.isArray(parsed)) return []
    // Normalize + drop corrupt entries (missing/blank ticker). Backward-compatible
    // with legacy entries that lack entry_price / previous_close — those fields stay
    // undefined and the daily-P&L math null-guards them (shows '—').
    return parsed
      .filter(s => s && typeof s.ticker === 'string' && s.ticker.trim())
      .map(s => ({ ...s, ticker: s.ticker.trim().toUpperCase() }))
  } catch { return [] }
}
function saveWatchlist(list) {
  try { localStorage.setItem('sentinel_quant_watchlist', JSON.stringify(list)) } catch {}
}

// ─── Math helpers ─────────────────────────────────────────────────────────────
function round(n, d) { return Math.round(n * Math.pow(10, d)) / Math.pow(10, d) }
function fmtPrice(v) {
  if (v == null || isNaN(v) || v <= 0) return '—'
  return '$' + Number(v).toFixed(2)
}
function fmtDate(v) {
  if (!v) return '—'
  const d = new Date(v)
  return isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

// ─── Sell-signal engine (LONG positions) ──────────────────────────────────────
// Correlates a holding with its STB stop/target levels + the QHF signal to
// produce a clear HOLD / SELL verdict — so "when it says sell, you sell".
// Long-only, matching the buy-a-pick-then-hold workflow. Every branch is
// null-guarded; missing levels simply yield a neutral verdict.
const TONE = {
  emerald: { text: 'text-emerald-300', border: 'border-emerald-600/60', ring: 'border-l-emerald-500', bg: 'bg-emerald-950/30', dot: 'bg-emerald-400' },
  rose:    { text: 'text-rose-300',    border: 'border-rose-600/60',    ring: 'border-l-rose-500',    bg: 'bg-rose-950/30',    dot: 'bg-rose-400' },
  amber:   { text: 'text-amber-300',   border: 'border-amber-600/60',   ring: 'border-l-amber-500',   bg: 'bg-amber-950/30',   dot: 'bg-amber-400' },
  neutral: { text: 'text-neutral-400', border: 'border-neutral-800/60', ring: 'border-l-transparent', bg: '',                 dot: 'bg-neutral-600' },
}
function pctBetween(from, to) {
  if (!(from > 0) || !(to > 0)) return null
  return ((to - from) / from) * 100
}
function computeAction(stock, qhf) {
  const px   = Number(stock.current_price) || 0
  const stop = Number(stock.stop_loss)     || 0
  const tgt  = Number(stock.target_price)  || 0
  const hasLevels  = stop > 0 || tgt > 0
  const qhfBearish = !!(qhf && qhf.direction === 'SHORT')

  if (px > 0 && tgt > 0 && px >= tgt)
    return { status: 'target', label: 'SELL · TARGET HIT', tone: 'emerald',
      detail: `Live ${fmtPrice(px)} reached target ${fmtPrice(tgt)} — take profit.` }
  if (px > 0 && stop > 0 && px <= stop)
    return { status: 'stop', label: 'SELL · STOP HIT', tone: 'rose',
      detail: `Live ${fmtPrice(px)} hit stop ${fmtPrice(stop)} — cut the loss.` }

  const dTgt  = px > 0 && tgt  > 0 ? pctBetween(px, tgt)  : null   // +% remaining to target
  const dStop = px > 0 && stop > 0 ? pctBetween(px, stop) : null   // −% cushion above stop
  if (dTgt != null && dTgt >= 0 && dTgt <= 2)
    return { status: 'near_target', label: 'NEAR TARGET', tone: 'emerald',
      detail: `Only ${dTgt.toFixed(1)}% from target ${fmtPrice(tgt)} — watch to take profit.` }
  if (dStop != null && dStop <= 0 && dStop >= -2)
    return { status: 'near_stop', label: 'NEAR STOP', tone: 'rose',
      detail: `Only ${Math.abs(dStop).toFixed(1)}% above stop ${fmtPrice(stop)} — watch closely.` }
  if (qhfBearish)
    return { status: 'strong_sell', label: 'STRONG SELL', tone: 'rose',
      detail: 'Quant model turned bearish on a long you hold — exit the position.' }
  if (hasLevels)
    return { status: 'hold', label: 'HOLD', tone: 'neutral',
      detail: 'Between stop and target — let the position work.' }
  return { status: 'none', label: '', tone: 'neutral', detail: '' }
}
const ACTION_NEEDED = new Set(['target', 'stop', 'strong_sell', 'near_target', 'near_stop'])

// ─── Market-hours check ───────────────────────────────────────────────────────
function isMarketHours() {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', hour: 'numeric', minute: 'numeric', hour12: false,
  }).formatToParts(new Date())
  const h = parseInt(parts.find(p => p.type === 'hour')?.value ?? '0')
  const m = parseInt(parts.find(p => p.type === 'minute')?.value ?? '0')
  const t = h * 60 + m
  return t >= 570 && t <= 960 // 9:30am – 4:00pm ET
}

// Derive the STRONG SELL / SELL / HOLD / BUY / STRONG BUY verdict for a pick
// that only carries a direction + confidence (the pre-scanned quant-picks
// universe). The per-ticker scorer returns an authoritative `signal` field, so
// this is only used as a fallback for universe picks.
function deriveSignal(direction, confidence) {
  const c = Number(confidence) || 0
  if (direction === 'LONG')  return c >= 85 ? 'STRONG BUY'  : 'BUY'
  if (direction === 'SHORT') return c >= 85 ? 'STRONG SELL' : 'SELL'
  return 'HOLD'
}

// ─── QHF Signal Badge ─────────────────────────────────────────────────────────
function QHFBadge({ sig }) {
  if (!sig) return null
  const label = sig.signal || deriveSignal(sig.direction, sig.confidence)
  const isBuy  = label.includes('BUY')
  const isSell = label.includes('SELL')
  const col   = isBuy ? 'bg-emerald-950/50 border-emerald-800/40 text-emerald-300'
              : isSell ? 'bg-rose-950/50 border-rose-800/40 text-rose-300'
              : 'bg-neutral-900 border-neutral-700 text-neutral-400'
  const arrow = isBuy ? '▲' : isSell ? '▼' : '·'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-bold font-mono ${col}`}>
      {arrow} {label}{sig.confidence != null ? ` ${sig.confidence}%` : ''}
    </span>
  )
}

// ─── QHF Factor Bars ──────────────────────────────────────────────────────────
function QHFFactors({ sig }) {
  if (!sig?.factors) return null
  const entries = Object.entries(sig.factors)
    .map(([k, v]) => ({ name: k, contrib: v?.contribution ?? 0 }))
    .filter(e => Math.abs(e.contrib) > 0.02)
    .sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib))
    .slice(0, 6)
  if (!entries.length) return null
  const maxAbs = Math.max(...entries.map(e => Math.abs(e.contrib)), 0.01)
  return (
    <div className="mt-3 space-y-1">
      <p className="text-[9px] font-bold uppercase tracking-widest text-neutral-600 mb-1">QHF Factor breakdown</p>
      {entries.map(e => {
        const bull = e.contrib > 0
        return (
          <div key={e.name} className="flex items-center gap-2">
            <span className="text-[9px] text-neutral-500 w-24 truncate capitalize">{e.name.replace(/_/g, ' ')}</span>
            <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${bull ? 'bg-emerald-500/60' : 'bg-rose-500/60'}`}
                   style={{ width: `${Math.abs(e.contrib) / maxAbs * 100}%` }} />
            </div>
            <span className={`text-[9px] font-mono w-9 text-right ${bull ? 'text-emerald-400' : 'text-rose-400'}`}>
              {e.contrib > 0 ? '+' : ''}{e.contrib.toFixed(2)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// ─── Inline number editor ─────────────────────────────────────────────────────
function InlineEdit({ value, onSave, prefix = '', suffix = '', min = 0 }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef(null)

  function start() {
    setDraft(value != null ? String(value) : '')
    setEditing(true)
    setTimeout(() => inputRef.current?.select(), 30)
  }
  function commit() {
    const n = parseFloat(draft)
    if (!isNaN(n) && n >= min) onSave(n)
    setEditing(false)
  }

  if (!editing) {
    return (
      <button onClick={start} className="text-left hover:text-blue-300 transition-colors underline decoration-dotted underline-offset-2">
        {prefix}{value != null && !isNaN(value) ? Number(value).toFixed(2) : '—'}{suffix}
      </button>
    )
  }
  return (
    <input
      ref={inputRef}
      type="number"
      value={draft}
      onChange={e => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setEditing(false) }}
      className="w-20 px-1 py-0.5 bg-neutral-800 border border-blue-600 rounded text-white text-xs font-mono focus:outline-none"
    />
  )
}

// ─── Main component ───────────────────────────────────────────────────────────
export default function Watchlist() {
  const [watchlist, setWatchlist]       = useState([])
  const [priceLoading, setPriceLoading] = useState({})
  const [addTicker, setAddTicker]       = useState('')
  const [addEntryPx, setAddEntryPx]     = useState('')
  const [addStop, setAddStop]           = useState('')
  const [addTarget, setAddTarget]       = useState('')
  const [adding, setAdding]             = useState(false)
  const [suggestions, setSuggestions]   = useState([])
  const [showSugg, setShowSugg]         = useState(false)
  const [expanded, setExpanded]         = useState(null)
  const [quantSignals, setQuantSignals] = useState({})   // ticker → {direction, confidence, ...}

  const refreshingAllRef = useRef(false)

  // ── Rate-limit-safe refresh: sequential, staggered, non-overlapping ─────────
  // Firing N parallel /api/quote calls (mount + every interval) risks tripping
  // Yahoo's rate limits on a cold backend cache. Instead we walk the tickers one
  // at a time with a small gap, and skip a cycle if the previous one is still running.
  async function refreshAll(tickers) {
    if (refreshingAllRef.current) return
    const list = (tickers || []).filter(t => typeof t === 'string' && t)
    if (!list.length) return
    refreshingAllRef.current = true
    try {
      for (const t of list) {
        await refreshPrice(t)
        if (list.length > 1) await new Promise(r => setTimeout(r, 250))
      }
    } finally {
      refreshingAllRef.current = false
    }
  }

  // ── Mount: load watchlist, fetch prices, quant signals ──────────────────────
  useEffect(() => {
    const wl = getWatchlist()
    setWatchlist(wl)
    refreshAll(wl.map(s => s.ticker))
    fetchQuantSignals()

    const iv = setInterval(() => {
      refreshAll(getWatchlist().map(s => s.ticker))
    }, isMarketHours() ? 30000 : 300000)
    return () => clearInterval(iv)
  }, [])

  // ── QHF signals: cover EVERY holding, not just the pick universe ─────────────
  // 1) One cache-only /api/quant-picks call instantly covers any holding that is
  //    already in the pre-scanned universe.
  // 2) Every remaining holding gets the on-demand per-ticker scorer
  //    (/api/watchlist-analysis/{ticker}) so a QHF verdict always shows.
  // Per-ticker calls are staggered to stay rate-limit-safe on a cold backend.
  const quantFetchingRef = useRef(false)
  async function fetchQuantSignals() {
    if (quantFetchingRef.current) return
    const tickers = getWatchlist().map(s => s.ticker).filter(Boolean)
    if (!tickers.length) return
    quantFetchingRef.current = true
    try {
      // Fast pass: pick universe (cache-only, zero external calls).
      const covered = new Set()
      try {
        const r = await fetch('/api/quant-picks')
        if (r.ok) {
          const j = await r.json()
          const map = {}
          for (const p of [...(j.long_picks || []), ...(j.short_picks || [])]) {
            if (p.ticker && tickers.includes(p.ticker)) {
              map[p.ticker] = {
                direction: p.direction, confidence: p.confidence,
                composite_score: p.composite_score, factors: p.factors,
                signal: deriveSignal(p.direction, p.confidence),
              }
              covered.add(p.ticker)
            }
          }
          if (Object.keys(map).length) setQuantSignals(prev => ({ ...prev, ...map }))
        }
      } catch { /* fall through to per-ticker */ }

      // Per-ticker pass: authoritative STRONG SELL…STRONG BUY signal for the rest.
      for (const t of tickers) {
        if (covered.has(t)) continue
        try {
          const wr = await fetch(`/api/watchlist-analysis/${t}`)
          if (wr.ok) {
            const w = await wr.json()
            if (w && w.analyzed && w.signal) {
              setQuantSignals(prev => ({
                ...prev,
                [t]: {
                  direction: w.direction, confidence: w.confidence,
                  composite_score: w.composite_score, factors: w.factors,
                  signal: w.signal,
                },
              }))
            }
          }
        } catch { /* skip this ticker; others still populate */ }
        await new Promise(r => setTimeout(r, 200))
      }
    } finally {
      quantFetchingRef.current = false
    }
  }

  // ── Live price refresh (quote endpoint, cached on backend) ──────────────────
  // CRITICAL: never overwrite a known-good value with a bad quote. A transient
  // 0 / null / NaN from the upstream feed would otherwise show a false ~-100%
  // daily P&L wipeout. We only commit price / previous_close when each is a
  // finite number > 0; otherwise we keep the previous value untouched.
  async function refreshPrice(ticker) {
    setPriceLoading(prev => ({ ...prev, [ticker]: true }))
    try {
      const res = await fetch(`/api/quote/${ticker}`)
      if (res.ok) {
        const data = await res.json()
        const raw = Number(data.current_price)
        const validPrice = Number.isFinite(raw) && raw > 0
        const prevRaw = Number(data.previous_close)
        const validPrev = Number.isFinite(prevRaw) && prevRaw > 0
        setWatchlist(prev => {
          const updated = prev.map(s => {
            if (s.ticker !== ticker) return s
            return {
              ...s,
              // Only update when the quote is sane; else preserve last good value.
              current_price: validPrice ? raw : s.current_price,
              previous_close: validPrev ? prevRaw : s.previous_close,
              name: data.name || s.name,
              last_updated: validPrice ? new Date().toISOString() : s.last_updated,
            }
          })
          saveWatchlist(updated)
          return updated
        })
      }
    } catch {}
    setPriceLoading(prev => ({ ...prev, [ticker]: false }))
  }

  // ── Add holding ─────────────────────────────────────────────────────────────
  async function addStock(rawTicker, nameHint) {
    const ticker = typeof rawTicker === 'string' ? rawTicker.trim().toUpperCase() : ''
    if (!ticker || watchlist.some(s => s.ticker === ticker)) return
    setAdding(true)
    setShowSugg(false)
    try {
      const res = await fetch(`/api/quote/${ticker}`)
      let livePrice = 0, prevClose = null, stockName = nameHint || ticker
      if (res.ok) {
        const d = await res.json()
        const raw = Number(d.current_price)
        livePrice = Number.isFinite(raw) && raw > 0 ? raw : 0
        const pRaw = Number(d.previous_close)
        prevClose = Number.isFinite(pRaw) && pRaw > 0 ? pRaw : null
        stockName = d.name || stockName
      }
      const typedEntry = parseFloat(addEntryPx)
      const entryPx = Number.isFinite(typedEntry) && typedEntry > 0 ? typedEntry : livePrice
      const typedStop = parseFloat(addStop)
      const stopLoss  = Number.isFinite(typedStop) && typedStop > 0 ? round(typedStop, 2) : null
      const typedTgt  = parseFloat(addTarget)
      const target    = Number.isFinite(typedTgt) && typedTgt > 0 ? round(typedTgt, 2) : null
      const newStock = {
        ticker, name: stockName,
        entry_price: round(entryPx, 2),
        stop_loss: stopLoss,
        target_price: target,
        current_price: livePrice,
        previous_close: prevClose,
        added_at: new Date().toISOString(),
        last_updated: new Date().toISOString(),
      }
      const updated = [...watchlist, newStock]
      setWatchlist(updated)
      saveWatchlist(updated)
      setAddTicker('')
      setAddEntryPx('')
      setAddStop('')
      setAddTarget('')
    } catch {}
    setAdding(false)
  }

  function removeStock(ticker) {
    const updated = watchlist.filter(s => s.ticker !== ticker)
    setWatchlist(updated)
    saveWatchlist(updated)
    if (expanded === ticker) setExpanded(null)
  }

  function updateField(ticker, field, value) {
    setWatchlist(prev => {
      const updated = prev.map(s => s.ticker === ticker ? { ...s, [field]: value } : s)
      saveWatchlist(updated)
      return updated
    })
  }

  // ── Autocomplete ─────────────────────────────────────────────────────────────
  async function fetchSuggestions(q) {
    if (!q || q.length < 1) { setSuggestions([]); setShowSugg(false); return }
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`)
      const data = await res.json()
      setSuggestions(data.results || [])
      setShowSugg((data.results || []).length > 0)
    } catch { setSuggestions([]) }
  }

  // Sell-signal alerts across all holdings (correlates STB stop/target + QHF).
  const actionable = watchlist
    .map(s => ({ stock: s, action: computeAction(s, quantSignals[s.ticker] || null) }))
    .filter(x => ACTION_NEEDED.has(x.action.status))
  const sellNow = actionable.filter(x => x.action.status === 'target' || x.action.status === 'stop' || x.action.status === 'strong_sell')

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between mb-8 gap-4">
        <div>
          <h1 className="text-3xl font-black text-white tracking-tight">Holdings & Watchlist</h1>
          <p className="text-sm text-neutral-400 mt-1">
            Add a pick with its stop &amp; target · get automatic BUY / SELL signals · QHF wired in
          </p>
        </div>
      </div>

      {/* Add holding form */}
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-5 mb-6">
        <p className="text-sm font-semibold text-white mb-3">Add holding</p>
        <div className="flex flex-wrap gap-3 items-end">
          {/* Ticker autocomplete */}
          <div className="relative">
            <label className="text-[10px] uppercase tracking-wider text-neutral-500 block mb-1">Ticker</label>
            <input
              type="text"
              value={addTicker}
              onChange={e => { const v = e.target.value.toUpperCase(); setAddTicker(v); fetchSuggestions(v) }}
              onFocus={() => { if (suggestions.length > 0) setShowSugg(true) }}
              onBlur={() => setTimeout(() => setShowSugg(false), 150)}
              placeholder="AAPL"
              autoComplete="off"
              className="w-28 px-3 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm font-mono
                         focus:outline-none focus:border-blue-600 placeholder-neutral-600"
            />
            {showSugg && suggestions.length > 0 && (
              <div className="absolute z-50 top-full mt-1 w-64 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl overflow-hidden max-h-52 overflow-y-auto">
                {suggestions.map(item => (
                  <button key={item.ticker} type="button"
                    onMouseDown={() => { setAddTicker(item.ticker); setShowSugg(false) }}
                    className="w-full px-3 py-2 flex gap-3 text-left hover:bg-neutral-800 border-b border-neutral-800/50">
                    <span className="font-mono font-bold text-white text-xs">{item.ticker}</span>
                    <span className="text-neutral-400 text-xs truncate">{item.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Entry price */}
          <div>
            <label className="text-[10px] uppercase tracking-wider text-neutral-500 block mb-1">Entry price</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500 text-sm">$</span>
              <input type="number" min="0" step="0.01" value={addEntryPx}
                onChange={e => setAddEntryPx(e.target.value)}
                placeholder="auto"
                className="w-28 pl-6 pr-3 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm font-mono
                           focus:outline-none focus:border-blue-600 placeholder-neutral-600"
              />
            </div>
          </div>

          {/* Stop loss */}
          <div>
            <label className="text-[10px] uppercase tracking-wider text-rose-400/80 block mb-1">Stop loss</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500 text-sm">$</span>
              <input type="number" min="0" step="0.01" value={addStop}
                onChange={e => setAddStop(e.target.value)}
                placeholder="optional"
                className="w-28 pl-6 pr-3 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm font-mono
                           focus:outline-none focus:border-rose-600 placeholder-neutral-600"
              />
            </div>
          </div>

          {/* Target */}
          <div>
            <label className="text-[10px] uppercase tracking-wider text-emerald-400/80 block mb-1">Target</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500 text-sm">$</span>
              <input type="number" min="0" step="0.01" value={addTarget}
                onChange={e => setAddTarget(e.target.value)}
                placeholder="optional"
                className="w-28 pl-6 pr-3 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm font-mono
                           focus:outline-none focus:border-emerald-600 placeholder-neutral-600"
              />
            </div>
          </div>

          <button
            onClick={() => { if (addTicker.trim()) addStock(addTicker.trim()) }}
            disabled={adding || !addTicker.trim()}
            className="px-6 py-2 bg-white text-black font-semibold text-sm rounded-lg
                       hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 transition-all"
          >
            {adding ? 'Adding…' : 'Add'}
          </button>
          <p className="text-[11px] text-neutral-600 self-center max-w-[16rem]">
            Blank entry = today's price. Add the STB stop &amp; target to get automatic SELL alerts.
          </p>
        </div>
      </div>

      {/* QHF signal refresh */}
      {watchlist.length > 0 && (
        <div className="mb-6 flex items-center gap-3">
          <button onClick={fetchQuantSignals} className="text-xs text-neutral-500 hover:text-white">
            Refresh QHF signals
          </button>
        </div>
      )}

      {/* Sell-signal alerts — the STB↔Watchlist correlation surface */}
      {actionable.length > 0 && (
        <div className={`rounded-xl border p-4 mb-6 ${sellNow.length > 0 ? 'bg-amber-950/25 border-amber-700/50' : 'bg-neutral-900/50 border-neutral-800/60'}`}>
          <div className="flex items-center gap-2 mb-3">
            <span className={`w-2 h-2 rounded-full ${sellNow.length > 0 ? 'bg-amber-400 animate-pulse' : 'bg-neutral-500'}`} />
            <p className="text-sm font-bold text-white">
              {sellNow.length > 0
                ? `${sellNow.length} holding${sellNow.length > 1 ? 's' : ''} hit a SELL level`
                : `${actionable.length} holding${actionable.length > 1 ? 's' : ''} to watch`}
            </p>
          </div>
          <div className="space-y-1.5">
            {actionable.map(({ stock, action }) => {
              const tone = TONE[action.tone] || TONE.neutral
              return (
                <button
                  key={stock.ticker}
                  onClick={() => setExpanded(stock.ticker)}
                  className="w-full flex items-center gap-3 text-left px-3 py-1.5 rounded-lg hover:bg-neutral-800/40 transition-colors"
                >
                  <span className={`text-[10px] font-black px-2 py-0.5 rounded border ${tone.bg} ${tone.border} ${tone.text} shrink-0`}>
                    {action.label}
                  </span>
                  <span className="font-mono font-bold text-white text-sm shrink-0">{stock.ticker}</span>
                  <span className="text-xs text-neutral-400 truncate">{action.detail}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Holdings list */}
      {watchlist.length > 0 ? (
        <div className="space-y-3">
          {/* Column headers */}
          <div className="hidden md:grid grid-cols-12 gap-2 px-5 pb-1 text-[10px] font-bold uppercase tracking-wider text-neutral-600">
            <div className="col-span-2">Ticker</div>
            <div className="col-span-2 text-right">Live price</div>
            <div className="col-span-2 text-right">Entry</div>
            <div className="col-span-2 text-right">Daily P&amp;L</div>
            <div className="col-span-2 text-center">QHF Signal</div>
            <div className="col-span-2 text-right">Actions</div>
          </div>

          {watchlist.map(stock => {
            const qhf       = quantSignals[stock.ticker] || null
            const isExp     = expanded === stock.ticker
            const px        = stock.current_price || 0
            const entry     = stock.entry_price   || 0
            // Daily P&L = today's move vs the official previous close. Both values
            // must be finite > 0 or we show '—' (never a fabricated number).
            const prevClose  = Number(stock.previous_close) || 0
            const dailyValid = px > 0 && prevClose > 0
            const dailyDelta = dailyValid ? px - prevClose : null                 // $ per share today
            const dailyPct   = dailyValid ? ((px - prevClose) / prevClose) * 100 : null
            const dailyColor = dailyPct == null ? 'text-neutral-500' : dailyPct >= 0 ? 'text-emerald-400' : 'text-rose-400'
            const action    = computeAction(stock, qhf)
            const tone      = TONE[action.tone] || TONE.neutral
            const flagged   = ACTION_NEEDED.has(action.status)

            return (
              <div key={stock.ticker} className={`bg-neutral-900/40 border border-neutral-800/60 border-l-4 ${flagged ? tone.ring : 'border-l-transparent'} rounded-xl overflow-hidden`}>
                {/* Main row */}
                <div
                  className="grid grid-cols-12 gap-2 items-center px-5 py-4 cursor-pointer hover:bg-neutral-800/30 transition-colors"
                  onClick={() => setExpanded(isExp ? null : stock.ticker)}
                >
                  {/* Ticker + Name + action pill */}
                  <div className="col-span-2 min-w-0">
                    <div className="font-mono font-bold text-white text-sm">{stock.ticker}</div>
                    {flagged ? (
                      <span className={`inline-block mt-0.5 text-[9px] font-black px-1.5 py-0.5 rounded border ${tone.bg} ${tone.border} ${tone.text}`}>
                        {action.label}
                      </span>
                    ) : (
                      stock.name && <div className="text-neutral-500 text-[10px] truncate">{stock.name}</div>
                    )}
                  </div>

                  {/* Live price */}
                  <div className="col-span-2 text-right">
                    <div className="font-mono text-sm text-white">{priceLoading[stock.ticker] ? '…' : fmtPrice(px)}</div>
                  </div>

                  {/* Entry price (inline edit) */}
                  <div className="col-span-2 text-right text-sm font-mono text-neutral-400" onClick={e => e.stopPropagation()}>
                    <InlineEdit
                      value={entry || null}
                      onSave={v => updateField(stock.ticker, 'entry_price', v)}
                      prefix="$"
                    />
                  </div>

                  {/* Daily P&L — today's move vs previous close */}
                  <div className="col-span-2 text-right">
                    {dailyPct != null ? (
                      <>
                        <div className={`font-mono text-sm font-bold ${dailyColor}`}>
                          {dailyPct >= 0 ? '+' : ''}{dailyPct.toFixed(2)}%
                        </div>
                        <div className={`font-mono text-[10px] ${dailyColor}`}>
                          {dailyDelta >= 0 ? '+$' : '-$'}{Math.abs(dailyDelta).toFixed(2)} today
                        </div>
                      </>
                    ) : (
                      <span className="text-neutral-600 text-xs">—</span>
                    )}
                  </div>

                  {/* QHF Badge */}
                  <div className="col-span-2 flex justify-center">
                    <QHFBadge sig={qhf} />
                  </div>

                  {/* Actions */}
                  <div className="col-span-2 flex items-center justify-end gap-3" onClick={e => e.stopPropagation()}>
                    <button
                      onClick={() => refreshPrice(stock.ticker)}
                      disabled={priceLoading[stock.ticker]}
                      className="text-[11px] text-neutral-500 hover:text-white transition-colors"
                    >
                      {priceLoading[stock.ticker] ? '…' : 'Refresh'}
                    </button>
                    <button
                      onClick={() => removeStock(stock.ticker)}
                      className="text-[11px] text-neutral-600 hover:text-rose-400 transition-colors"
                    >
                      Remove
                    </button>
                    <span className={`text-neutral-600 transition-transform text-xs ${isExp ? 'rotate-180 inline-block' : ''}`}>▼</span>
                  </div>
                </div>

                {/* Expanded panel */}
                {isExp && (
                  <div className="border-t border-neutral-800/60 px-5 py-4 bg-neutral-950/40">
                    {/* Recommended action — the sell/hold verdict */}
                    {action.status !== 'none' && (
                      <div className={`mb-4 rounded-lg border px-4 py-3 ${tone.bg || 'bg-neutral-900/40'} ${tone.border}`}>
                        <div className="flex items-center gap-2">
                          <span className={`w-2 h-2 rounded-full ${tone.dot}`} />
                          <span className={`text-sm font-black ${tone.text}`}>{action.label || 'HOLD'}</span>
                        </div>
                        <p className="text-xs text-neutral-300 mt-1">{action.detail}</p>
                      </div>
                    )}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                      {/* Position details */}
                      <div>
                        <p className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 mb-2">Position details</p>
                        <div className="space-y-1.5 text-xs">
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Live price</span>
                            <span className="font-mono text-white">{fmtPrice(px)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Entry price</span>
                            <span className="font-mono text-neutral-300">{fmtPrice(entry)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Prev close</span>
                            <span className="font-mono text-neutral-300">{fmtPrice(prevClose)}</span>
                          </div>
                          <div className="flex justify-between items-center" onClick={e => e.stopPropagation()}>
                            <span className="text-rose-400/80">Stop loss</span>
                            <span className="font-mono text-rose-200 flex items-center gap-2">
                              {stock.stop_loss > 0 && px > 0 && (
                                <span className="text-[10px] text-neutral-500">
                                  {pctBetween(px, stock.stop_loss)?.toFixed(1)}%
                                </span>
                              )}
                              <InlineEdit value={stock.stop_loss || null} onSave={v => updateField(stock.ticker, 'stop_loss', v)} prefix="$" />
                            </span>
                          </div>
                          <div className="flex justify-between items-center" onClick={e => e.stopPropagation()}>
                            <span className="text-emerald-400/80">Target</span>
                            <span className="font-mono text-emerald-200 flex items-center gap-2">
                              {stock.target_price > 0 && px > 0 && (
                                <span className="text-[10px] text-neutral-500">
                                  +{pctBetween(px, stock.target_price)?.toFixed(1)}%
                                </span>
                              )}
                              <InlineEdit value={stock.target_price || null} onSave={v => updateField(stock.ticker, 'target_price', v)} prefix="$" />
                            </span>
                          </div>
                          <div className="flex justify-between border-t border-neutral-800 pt-1.5 mt-1.5">
                            <span className="text-neutral-400 font-bold">Daily P&amp;L</span>
                            <span className={`font-mono font-bold ${dailyColor}`}>
                              {dailyPct != null
                                ? `${dailyPct >= 0 ? '+' : ''}${dailyPct.toFixed(2)}% (${dailyDelta >= 0 ? '+$' : '-$'}${Math.abs(dailyDelta).toFixed(2)})`
                                : '—'}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Added</span>
                            <span className="text-neutral-500 text-[10px]">{fmtDate(stock.added_at)}</span>
                          </div>
                        </div>
                      </div>

                      {/* QHF signal */}
                      <div>
                        {qhf ? (
                          <>
                            <div className="flex items-center gap-3 mb-2">
                              <p className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">Quant HF Signal</p>
                              <QHFBadge sig={qhf} />
                              <span className="text-[10px] text-neutral-600 font-mono">score {qhf.composite_score?.toFixed(2)}</span>
                            </div>
                            <QHFFactors sig={qhf} />
                          </>
                        ) : (
                          <div>
                            <p className="text-[10px] font-bold uppercase tracking-widest text-neutral-600 mb-1">Quant HF Signal</p>
                            <p className="text-xs text-neutral-600">
                              Signal loading for {stock.ticker}…{' '}
                              <button onClick={e => { e.stopPropagation(); fetchQuantSignals() }} className="text-blue-400 hover:text-blue-300 underline">Refresh signals</button>
                            </p>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-16 text-center">
          <p className="text-neutral-400 font-medium mb-1">No holdings yet</p>
          <p className="text-neutral-600 text-sm">Add a ticker above — include the stop &amp; target to get automatic BUY / SELL signals</p>
        </div>
      )}

      <p className="text-neutral-700 text-[11px] mt-6 text-center">
        Stored locally on your device · Prices from Yahoo Finance · QHF signal from in-memory cache · Not financial advice
      </p>
    </div>
  )
}
