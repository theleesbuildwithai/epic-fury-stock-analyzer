import { useState, useEffect, useRef } from 'react'

// ─── LocalStorage helpers ─────────────────────────────────────────────────────
function getWatchlist() {
  try {
    const data = localStorage.getItem('sentinel_quant_watchlist')
    return data ? JSON.parse(data) : []
  } catch { return [] }
}
function saveWatchlist(list) {
  try { localStorage.setItem('sentinel_quant_watchlist', JSON.stringify(list)) } catch {}
}

// ─── Math helpers ─────────────────────────────────────────────────────────────
function round(n, d) { return Math.round(n * Math.pow(10, d)) / Math.pow(10, d) }
function fmtDollar(v) {
  if (v == null || isNaN(v)) return '—'
  return (v >= 0 ? '+$' : '-$') + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })
}
function fmtPrice(v) {
  if (v == null || isNaN(v) || v <= 0) return '—'
  return '$' + Number(v).toFixed(2)
}
function fmtPct(v) {
  if (v == null || isNaN(v)) return '—'
  return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%'
}
function fmtValue(v) {
  if (v == null || isNaN(v)) return '—'
  return '$' + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

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

// ─── QHF Signal Badge ─────────────────────────────────────────────────────────
function QHFBadge({ sig }) {
  if (!sig) return null
  const isLong  = sig.direction === 'LONG'
  const isShort = sig.direction === 'SHORT'
  const col   = isLong ? 'bg-emerald-950/50 border-emerald-800/40 text-emerald-300'
              : isShort ? 'bg-rose-950/50 border-rose-800/40 text-rose-300'
              : 'bg-neutral-900 border-neutral-700 text-neutral-500'
  const arrow = isLong ? '▲' : isShort ? '▼' : '·'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-bold font-mono ${col}`}>
      {arrow} {sig.direction} {sig.confidence}%
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

// ─── Correlation Cell ─────────────────────────────────────────────────────────
function CorrelationCell({ value }) {
  if (value == null || isNaN(value)) return <td className="px-2 py-1.5 text-center text-[11px] font-mono bg-neutral-800 text-neutral-400">—</td>
  const bg = value >= 0.7  ? 'bg-red-500/30 text-red-300'
           : value >= 0.4  ? 'bg-neutral-500/20 text-neutral-300'
           : value >= -0.1 ? 'bg-neutral-800 text-neutral-400'
           : 'bg-emerald-500/20 text-emerald-300'
  return <td className={`px-2 py-1.5 text-center text-[11px] font-mono ${bg}`}>{value.toFixed(2)}</td>
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
  const [addShares, setAddShares]       = useState('')
  const [adding, setAdding]             = useState(false)
  const [suggestions, setSuggestions]   = useState([])
  const [showSugg, setShowSugg]         = useState(false)
  const [expanded, setExpanded]         = useState(null)
  const [quantSignals, setQuantSignals] = useState({})   // ticker → {direction, confidence, ...}
  const [backtestData, setBacktestData] = useState(null)
  const [backtestLoading, setBtLoading] = useState(false)
  const [showBacktest, setShowBacktest] = useState(false)

  // ── Mount: load watchlist, fetch prices, quant signals ──────────────────────
  useEffect(() => {
    const wl = getWatchlist()
    setWatchlist(wl)
    wl.forEach(s => refreshPrice(s.ticker))
    fetchQuantSignals()

    const iv = setInterval(() => {
      getWatchlist().forEach(s => refreshPrice(s.ticker))
    }, isMarketHours() ? 30000 : 300000)
    return () => clearInterval(iv)
  }, [])

  // ── QHF signals: one cache-only call, no external API hits ──────────────────
  function fetchQuantSignals() {
    fetch('/api/quant-picks')
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (!j) return
        const map = {}
        for (const p of [...(j.long_picks || []), ...(j.short_picks || [])]) {
          if (p.ticker) map[p.ticker] = { direction: p.direction, confidence: p.confidence, composite_score: p.composite_score, factors: p.factors }
        }
        setQuantSignals(map)
      })
      .catch(() => {})
  }

  // ── Live price refresh (quote endpoint, cached on backend) ──────────────────
  async function refreshPrice(ticker) {
    setPriceLoading(prev => ({ ...prev, [ticker]: true }))
    try {
      const res = await fetch(`/api/quote/${ticker}`)
      if (res.ok) {
        const data = await res.json()
        const livePrice = data.current_price || 0
        setWatchlist(prev => {
          const updated = prev.map(s => {
            if (s.ticker !== ticker) return s
            return { ...s, current_price: livePrice, name: data.name || s.name, last_updated: new Date().toISOString() }
          })
          saveWatchlist(updated)
          return updated
        })
      }
    } catch {}
    setPriceLoading(prev => ({ ...prev, [ticker]: false }))
  }

  // ── Add holding ─────────────────────────────────────────────────────────────
  async function addStock(ticker, nameHint) {
    if (!ticker || watchlist.some(s => s.ticker === ticker)) return
    setAdding(true)
    setShowSugg(false)
    try {
      const res = await fetch(`/api/quote/${ticker}`)
      let livePrice = 0, stockName = nameHint || ticker
      if (res.ok) {
        const d = await res.json()
        livePrice = d.current_price || 0
        stockName = d.name || stockName
      }
      const entryPx = parseFloat(addEntryPx) > 0 ? parseFloat(addEntryPx) : livePrice
      const shares  = parseFloat(addShares)   > 0 ? parseFloat(addShares)  : null
      const newStock = {
        ticker, name: stockName,
        entry_price: round(entryPx, 2),
        shares: shares,
        current_price: livePrice,
        added_at: new Date().toISOString(),
        last_updated: new Date().toISOString(),
      }
      const updated = [...watchlist, newStock]
      setWatchlist(updated)
      saveWatchlist(updated)
      setAddTicker('')
      setAddEntryPx('')
      setAddShares('')
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

  // ── Portfolio Visualizer ──────────────────────────────────────────────────────
  async function fetchBacktest() {
    const wl = getWatchlist()
    const tickers = wl.map(s => s.ticker).join(',')
    if (!tickers) return
    const addDates = {}
    wl.forEach(s => { addDates[s.ticker] = s.added_at })
    setBtLoading(true)
    try {
      const res = await fetch(`/api/watchlist-backtest?tickers=${tickers}&add_dates=${encodeURIComponent(JSON.stringify(addDates))}`)
      if (res.ok) setBacktestData(await res.json())
    } catch {}
    setBtLoading(false)
  }

  // ── Portfolio summary ─────────────────────────────────────────────────────────
  const hasPositions = watchlist.some(s => s.shares > 0)
  const totalCost    = watchlist.reduce((sum, s) => sum + (s.shares > 0 ? (s.entry_price || 0) * s.shares : 0), 0)
  const totalVal     = watchlist.reduce((sum, s) => sum + (s.shares > 0 ? (s.current_price || 0) * s.shares : 0), 0)
  const totalPnl     = totalVal - totalCost
  const totalPnlPct  = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between mb-8 gap-4">
        <div>
          <h1 className="text-3xl font-black text-white tracking-tight">Holdings & Watchlist</h1>
          <p className="text-sm text-neutral-400 mt-1">
            Track your positions · Enter purchase price + shares to see live P&amp;L · QHF signal wired in
          </p>
        </div>

        {/* Portfolio totals */}
        {hasPositions && (
          <div className="flex items-center gap-6 bg-neutral-900/60 border border-neutral-800 rounded-xl px-5 py-3 shrink-0">
            <div className="text-right">
              <p className="text-[10px] uppercase tracking-wider text-neutral-500">Cost basis</p>
              <p className="font-bold text-white font-mono">{fmtValue(totalCost)}</p>
            </div>
            <div className="text-right">
              <p className="text-[10px] uppercase tracking-wider text-neutral-500">Market value</p>
              <p className="font-bold text-white font-mono">{fmtValue(totalVal)}</p>
            </div>
            <div className="text-right">
              <p className="text-[10px] uppercase tracking-wider text-neutral-500">Unrealized P&amp;L</p>
              <p className={`font-bold text-lg font-mono ${totalPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {fmtDollar(totalPnl)}
              </p>
              <p className={`text-[10px] font-mono ${totalPnl >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                {fmtPct(totalPnlPct)}
              </p>
            </div>
          </div>
        )}
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

          {/* Shares */}
          <div>
            <label className="text-[10px] uppercase tracking-wider text-neutral-500 block mb-1">Shares</label>
            <input type="number" min="0" step="any" value={addShares}
              onChange={e => setAddShares(e.target.value)}
              placeholder="optional"
              className="w-28 px-3 py-2 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm font-mono
                         focus:outline-none focus:border-blue-600 placeholder-neutral-600"
            />
          </div>

          <button
            onClick={() => { if (addTicker.trim()) addStock(addTicker.trim()) }}
            disabled={adding || !addTicker.trim()}
            className="px-6 py-2 bg-white text-black font-semibold text-sm rounded-lg
                       hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 transition-all"
          >
            {adding ? 'Adding…' : 'Add'}
          </button>
          <p className="text-[11px] text-neutral-600 self-center">
            Leave entry price blank to use today's price. Shares optional but required for P&amp;L.
          </p>
        </div>
      </div>

      {/* Portfolio Visualizer toggle */}
      {watchlist.length >= 2 && (
        <div className="mb-6 flex items-center gap-3">
          <button
            onClick={() => { setShowBacktest(v => !v); if (!showBacktest && !backtestData) fetchBacktest() }}
            className="px-5 py-2 bg-blue-950/40 border border-blue-800/40 text-blue-300 font-semibold text-sm rounded-lg hover:bg-blue-950/60 transition-all"
          >
            {showBacktest ? 'Hide' : 'Show'} Portfolio Visualizer
          </button>
          <button onClick={fetchQuantSignals} className="text-xs text-neutral-500 hover:text-white">
            Refresh QHF signals
          </button>
        </div>
      )}

      {/* Portfolio Visualizer panel */}
      {showBacktest && (
        <div className="bg-neutral-900/40 border border-blue-800/30 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-white font-bold">Portfolio Visualizer</h2>
              <p className="text-neutral-500 text-xs">Returns since added · correlations · risk metrics</p>
            </div>
            <button onClick={fetchBacktest} disabled={backtestLoading}
              className="px-3 py-1.5 text-xs bg-blue-500/20 text-blue-300 rounded hover:bg-blue-500/30 disabled:opacity-50">
              {backtestLoading ? 'Loading…' : 'Refresh'}
            </button>
          </div>

          {backtestLoading && !backtestData && (
            <p className="text-neutral-500 text-sm text-center py-8">Analyzing portfolio…</p>
          )}

          {backtestData && (
            <div className="space-y-6">
              {/* Equal-weight stats */}
              <div className="bg-neutral-950/60 border border-neutral-800 rounded-lg p-4">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-3">Equal-Weight Portfolio</p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                  <div>
                    <p className="text-neutral-500 text-[10px]">Return since added</p>
                    <p className={`text-lg font-bold font-mono ${backtestData.portfolio_stats.total_return >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {backtestData.portfolio_stats.total_return >= 0 ? '+' : ''}{backtestData.portfolio_stats.total_return}%
                    </p>
                  </div>
                  <div>
                    <p className="text-neutral-500 text-[10px]">Volatility</p>
                    <p className="text-white text-lg font-bold font-mono">{backtestData.portfolio_stats.annualized_vol}%</p>
                  </div>
                  <div>
                    <p className="text-neutral-500 text-[10px]">Sharpe</p>
                    <p className={`text-lg font-bold font-mono ${backtestData.portfolio_stats.sharpe_ratio >= 1 ? 'text-emerald-400' : backtestData.portfolio_stats.sharpe_ratio >= 0 ? 'text-neutral-400' : 'text-rose-400'}`}>
                      {backtestData.portfolio_stats.sharpe_ratio}
                    </p>
                  </div>
                  {backtestData.portfolio_stats.diversification_benefit != null && (
                    <div>
                      <p className="text-neutral-500 text-[10px]">Diversification</p>
                      <p className="text-emerald-400 text-lg font-bold font-mono">-{backtestData.portfolio_stats.diversification_benefit}% vol</p>
                    </div>
                  )}
                </div>
              </div>

              {/* Per-stock table */}
              <div>
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-2">Stock Performance</p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-neutral-800">
                        {['TICKER', 'RETURN', 'ANN VOL', 'SHARPE', 'MAX DD'].map(h => (
                          <th key={h} className={`py-2 px-3 text-[10px] text-neutral-500 ${h === 'TICKER' ? 'text-left' : 'text-right'}`}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(backtestData.stock_stats)
                        .sort(([,a],[,b]) => b.total_return - a.total_return)
                        .map(([sym, stats]) => (
                          <tr key={sym} className="border-b border-neutral-900 hover:bg-neutral-900/50">
                            <td className="py-2 px-3 font-mono font-bold text-white text-xs">{sym}</td>
                            <td className={`py-2 px-3 text-right font-mono text-xs font-bold ${stats.total_return >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {stats.total_return >= 0 ? '+' : ''}{stats.total_return}%
                            </td>
                            <td className="py-2 px-3 text-right font-mono text-xs text-neutral-400">{stats.annualized_vol}%</td>
                            <td className={`py-2 px-3 text-right font-mono text-xs ${stats.sharpe_ratio >= 1 ? 'text-emerald-400' : stats.sharpe_ratio >= 0 ? 'text-neutral-300' : 'text-rose-400'}`}>
                              {stats.sharpe_ratio}
                            </td>
                            <td className="py-2 px-3 text-right font-mono text-xs text-rose-400">{stats.max_drawdown}%</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Correlation matrix */}
              {backtestData.correlation_matrix && Object.keys(backtestData.correlation_matrix).length >= 2 && (
                <div>
                  <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-2">
                    Correlation Matrix <span className="normal-case text-neutral-600">(green = diversified · red = correlated)</span>
                  </p>
                  <div className="overflow-x-auto">
                    <table className="text-[11px]">
                      <thead>
                        <tr>
                          <th className="px-2 py-1.5 text-neutral-500" />
                          {backtestData.tickers.map(t => <th key={t} className="px-2 py-1.5 text-neutral-400 font-mono">{t}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {backtestData.tickers.map(t1 => (
                          <tr key={t1}>
                            <td className="px-2 py-1.5 text-neutral-400 font-mono font-bold">{t1}</td>
                            {backtestData.tickers.map(t2 => (
                              <CorrelationCell key={t2} value={backtestData.correlation_matrix[t1]?.[t2] ?? null} />
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Holdings list */}
      {watchlist.length > 0 ? (
        <div className="space-y-3">
          {/* Column headers */}
          <div className="hidden md:grid grid-cols-12 gap-2 px-5 pb-1 text-[10px] font-bold uppercase tracking-wider text-neutral-600">
            <div className="col-span-2">Ticker</div>
            <div className="col-span-1 text-right">Live price</div>
            <div className="col-span-1 text-right">Entry</div>
            <div className="col-span-1 text-right">Shares</div>
            <div className="col-span-1 text-right">Position</div>
            <div className="col-span-2 text-right">Unrealized P&amp;L</div>
            <div className="col-span-2 text-center">QHF Signal</div>
            <div className="col-span-2 text-right">Actions</div>
          </div>

          {watchlist.map(stock => {
            const qhf       = quantSignals[stock.ticker] || null
            const isExp     = expanded === stock.ticker
            const px        = stock.current_price || 0
            const entry     = stock.entry_price   || 0
            const shares    = stock.shares        || 0
            const posVal    = shares > 0 ? px    * shares : null
            const costBasis = shares > 0 ? entry * shares : null
            const pnlDollar = posVal != null && costBasis != null ? posVal - costBasis : null
            const pnlPct    = costBasis > 0 ? ((px - entry) / entry) * 100 : null
            const pnlColor  = pnlDollar == null ? 'text-neutral-500' : pnlDollar >= 0 ? 'text-emerald-400' : 'text-rose-400'
            const dayChgPct = entry > 0 ? ((px - entry) / entry) * 100 : null

            return (
              <div key={stock.ticker} className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
                {/* Main row */}
                <div
                  className="grid grid-cols-12 gap-2 items-center px-5 py-4 cursor-pointer hover:bg-neutral-800/30 transition-colors"
                  onClick={() => setExpanded(isExp ? null : stock.ticker)}
                >
                  {/* Ticker + Name */}
                  <div className="col-span-2 min-w-0">
                    <div className="font-mono font-bold text-white text-sm">{stock.ticker}</div>
                    {stock.name && <div className="text-neutral-500 text-[10px] truncate">{stock.name}</div>}
                  </div>

                  {/* Live price */}
                  <div className="col-span-1 text-right">
                    <div className="font-mono text-sm text-white">{priceLoading[stock.ticker] ? '…' : fmtPrice(px)}</div>
                  </div>

                  {/* Entry price (inline edit) */}
                  <div className="col-span-1 text-right text-sm font-mono text-neutral-400" onClick={e => e.stopPropagation()}>
                    <InlineEdit
                      value={entry || null}
                      onSave={v => updateField(stock.ticker, 'entry_price', v)}
                      prefix="$"
                    />
                  </div>

                  {/* Shares (inline edit) */}
                  <div className="col-span-1 text-right text-sm font-mono text-neutral-400" onClick={e => e.stopPropagation()}>
                    <InlineEdit
                      value={shares || null}
                      onSave={v => updateField(stock.ticker, 'shares', v)}
                      min={0}
                    />
                  </div>

                  {/* Position value */}
                  <div className="col-span-1 text-right text-sm font-mono text-neutral-300">
                    {posVal != null ? fmtValue(posVal) : <span className="text-neutral-600 text-xs">—</span>}
                  </div>

                  {/* P&L */}
                  <div className="col-span-2 text-right">
                    {pnlDollar != null ? (
                      <>
                        <div className={`font-mono text-sm font-bold ${pnlColor}`}>{fmtDollar(pnlDollar)}</div>
                        <div className={`font-mono text-[10px] ${pnlColor}`}>{fmtPct(pnlPct)}</div>
                      </>
                    ) : pnlPct != null ? (
                      <div className={`font-mono text-sm ${pnlColor}`}>{fmtPct(dayChgPct)}</div>
                    ) : (
                      <span className="text-neutral-600 text-xs">Add shares</span>
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
                            <span className="text-neutral-500">Shares</span>
                            <span className="font-mono text-neutral-300">{shares > 0 ? shares : '—'}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Cost basis</span>
                            <span className="font-mono text-neutral-300">{costBasis > 0 ? fmtValue(costBasis) : '—'}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Market value</span>
                            <span className="font-mono text-neutral-300">{posVal != null ? fmtValue(posVal) : '—'}</span>
                          </div>
                          <div className="flex justify-between border-t border-neutral-800 pt-1.5 mt-1.5">
                            <span className="text-neutral-400 font-bold">Unrealized P&amp;L</span>
                            <span className={`font-mono font-bold ${pnlColor}`}>
                              {pnlDollar != null ? fmtDollar(pnlDollar) : pnlPct != null ? fmtPct(pnlPct) : '—'}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-500">Added</span>
                            <span className="text-neutral-500 text-[10px]">{new Date(stock.added_at).toLocaleDateString()}</span>
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
                              {stock.ticker} is not in the current quant engine queue.{' '}
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
          <p className="text-neutral-600 text-sm">Add a ticker above — include your entry price and shares for full P&amp;L tracking</p>
        </div>
      )}

      <p className="text-neutral-700 text-[11px] mt-6 text-center">
        Stored locally on your device · Prices from Yahoo Finance · QHF signal from in-memory cache · Not financial advice
      </p>
    </div>
  )
}
