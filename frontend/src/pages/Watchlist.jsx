import { useState, useEffect } from 'react'

function getWatchlist() {
  try {
    const data = localStorage.getItem('epic_fury_watchlist')
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function saveWatchlist(list) {
  localStorage.setItem('epic_fury_watchlist', JSON.stringify(list))
}

// Signal badge colors
const SIGNAL_COLORS = {
  "STRONG BUY": "bg-green-500/20 text-green-400 border-green-500/30",
  "BUY": "bg-green-500/10 text-green-400 border-green-500/20",
  "HOLD": "bg-yellow-500/10 text-yellow-400 border-yellow-500/20",
  "SELL": "bg-red-500/10 text-red-400 border-red-500/20",
  "STRONG SELL": "bg-red-500/20 text-red-400 border-red-500/30",
}

function ConfidenceBar({ value }) {
  const color = value >= 70 ? 'bg-green-500' : value >= 50 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${value}%` }} />
      </div>
      <span className="text-neutral-400 text-[10px] font-mono">{value}%</span>
    </div>
  )
}

function CorrelationCell({ value }) {
  const abs = Math.abs(value)
  const bg = value >= 0.7 ? 'bg-red-500/30 text-red-300'
    : value >= 0.4 ? 'bg-yellow-500/20 text-yellow-300'
    : value >= -0.1 ? 'bg-neutral-800 text-neutral-400'
    : 'bg-green-500/20 text-green-300'
  return (
    <td className={`px-2 py-1.5 text-center text-[11px] font-mono ${bg}`}>
      {value.toFixed(2)}
    </td>
  )
}

export default function Watchlist() {
  const [watchlist, setWatchlist] = useState([])
  const [loading, setLoading] = useState({})
  const [addTicker, setAddTicker] = useState('')
  const [adding, setAdding] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [quantData, setQuantData] = useState({})  // ticker -> quant analysis
  const [quantLoading, setQuantLoading] = useState({})
  const [expandedStock, setExpandedStock] = useState(null)
  const [backtestData, setBacktestData] = useState(null)
  const [backtestLoading, setBacktestLoading] = useState(false)
  const [showBacktest, setShowBacktest] = useState(false)

  useEffect(() => {
    const wl = getWatchlist()
    setWatchlist(wl)
    wl.forEach(stock => refreshPrice(stock.ticker))
    // Auto-fetch quant data for all stocks
    wl.forEach(stock => fetchQuantAnalysis(stock.ticker))

    const isMarketHours = () => {
      const now = new Date()
      const hours = now.getHours()
      const mins = now.getMinutes()
      const t = hours * 60 + mins
      return t >= 390 && t <= 1050
    }

    const interval = setInterval(() => {
      if (isMarketHours()) {
        const current = getWatchlist()
        current.forEach(stock => refreshPrice(stock.ticker))
      }
    }, 60000)
    return () => clearInterval(interval)
  }, [])

  const refreshPrice = async (ticker) => {
    setLoading(prev => ({ ...prev, [ticker]: true }))
    try {
      const res = await fetch(`/api/quote/${ticker}`)
      if (res.ok) {
        const data = await res.json()
        setWatchlist(prev => {
          const updated = prev.map(s => {
            if (s.ticker === ticker) {
              const currentPrice = data.current_price || s.current_price
              const change = currentPrice - (s.entry_price || currentPrice)
              const changePct = s.entry_price ? ((currentPrice - s.entry_price) / s.entry_price) * 100 : 0
              return {
                ...s,
                current_price: currentPrice,
                name: data.name || s.name,
                change: round(change, 2),
                change_pct: round(changePct, 2),
                last_updated: new Date().toISOString(),
              }
            }
            return s
          })
          saveWatchlist(updated)
          return updated
        })
      }
    } catch {
      // Silent fail
    }
    setLoading(prev => ({ ...prev, [ticker]: false }))
  }

  const fetchQuantAnalysis = async (ticker) => {
    setQuantLoading(prev => ({ ...prev, [ticker]: true }))
    try {
      const res = await fetch(`/api/watchlist-analysis/${ticker}`)
      if (res.ok) {
        const data = await res.json()
        if (data.analyzed) {
          setQuantData(prev => ({ ...prev, [ticker]: data }))
        }
      }
    } catch {
      // Silent fail
    }
    setQuantLoading(prev => ({ ...prev, [ticker]: false }))
  }

  const fetchBacktest = async () => {
    const tickers = watchlist.map(s => s.ticker).join(',')
    if (!tickers) return
    // Send add dates so backend calculates return since added to watchlist
    const addDates = {}
    watchlist.forEach(s => { addDates[s.ticker] = s.added_at })
    setBacktestLoading(true)
    try {
      const res = await fetch(`/api/watchlist-backtest?tickers=${tickers}&add_dates=${encodeURIComponent(JSON.stringify(addDates))}`)
      if (res.ok) {
        const data = await res.json()
        setBacktestData(data)
      }
    } catch {
      // Silent fail
    }
    setBacktestLoading(false)
  }

  const round = (n, d) => Math.round(n * Math.pow(10, d)) / Math.pow(10, d)

  const fetchSuggestions = async (query) => {
    if (!query || query.length < 1) {
      setSuggestions([])
      setShowSuggestions(false)
      return
    }
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`)
      const data = await res.json()
      setSuggestions(data.results || [])
      setShowSuggestions(data.results && data.results.length > 0)
    } catch {
      setSuggestions([])
    }
  }

  const addStock = async (ticker, name) => {
    if (watchlist.some(s => s.ticker === ticker)) return
    setAdding(true)
    setShowSuggestions(false)
    setAddTicker('')

    try {
      const res = await fetch(`/api/quote/${ticker}`)
      let price = 0
      let stockName = name || ticker
      if (res.ok) {
        const data = await res.json()
        price = data.current_price || 0
        stockName = data.name || stockName
      }

      const newStock = {
        ticker,
        name: stockName,
        entry_price: price,
        current_price: price,
        change: 0,
        change_pct: 0,
        added_at: new Date().toISOString(),
        last_updated: new Date().toISOString(),
      }

      const updated = [...watchlist, newStock]
      setWatchlist(updated)
      saveWatchlist(updated)

      // Auto-fetch quant analysis for the new stock
      fetchQuantAnalysis(ticker)
    } catch {
      // Silent fail
    }
    setAdding(false)
  }

  const removeStock = (ticker) => {
    const updated = watchlist.filter(s => s.ticker !== ticker)
    setWatchlist(updated)
    saveWatchlist(updated)
    setQuantData(prev => {
      const next = { ...prev }
      delete next[ticker]
      return next
    })
    if (expandedStock === ticker) setExpandedStock(null)
  }

  const totalValue = watchlist.reduce((sum, s) => sum + (s.current_price || 0), 0)
  const totalChange = watchlist.reduce((sum, s) => sum + (s.change || 0), 0)

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Watchlist</h1>
          <p className="text-neutral-400">
            Track stocks with full quant analytics. Prices auto-update every 60s during market hours.
          </p>
        </div>

        {watchlist.length > 0 && (
          <div className="mt-4 sm:mt-0 flex items-center gap-6">
            <div className="text-right">
              <p className="text-neutral-500 text-xs uppercase tracking-wider">Stocks</p>
              <p className="text-white font-bold text-lg">{watchlist.length}</p>
            </div>
            <div className="text-right">
              <p className="text-neutral-500 text-xs uppercase tracking-wider">Total P/L</p>
              <p className={`font-bold text-lg font-mono ${totalChange >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                {totalChange >= 0 ? '+' : ''}{round(totalChange, 2)}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Add stock */}
      <div className="bg-black border border-neutral-700 rounded-xl p-5 mb-6">
        <p className="text-white font-medium text-sm mb-3">Add a stock to your watchlist</p>
        <div className="relative flex gap-2">
          <div className="relative flex-1 max-w-sm">
            <input
              type="text"
              value={addTicker}
              onChange={(e) => {
                const v = e.target.value.toUpperCase()
                setAddTicker(v)
                fetchSuggestions(v)
              }}
              onFocus={() => { if (suggestions.length > 0) setShowSuggestions(true) }}
              placeholder="Search ticker or company..."
              className="w-full px-4 py-2.5 bg-neutral-900 border border-neutral-700 rounded-lg text-white text-sm
                         focus:outline-none focus:border-neutral-500 placeholder-neutral-500"
              disabled={adding}
              autoComplete="off"
            />

            {showSuggestions && suggestions.length > 0 && (
              <div className="absolute z-50 w-full mt-1 bg-neutral-900 border border-neutral-700
                              rounded-lg shadow-2xl overflow-hidden max-h-60 overflow-y-auto">
                {suggestions.map((item) => (
                  <button
                    key={item.ticker}
                    type="button"
                    onClick={() => addStock(item.ticker, item.name)}
                    className="w-full px-4 py-2.5 flex items-center gap-3 text-left hover:bg-neutral-800
                               border-b border-neutral-800/50 transition-colors"
                  >
                    <span className="font-mono font-bold text-white text-xs bg-neutral-800 px-2 py-0.5 rounded">
                      {item.ticker}
                    </span>
                    <span className="text-neutral-300 text-sm truncate">{item.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={() => { if (addTicker.trim()) addStock(addTicker.trim()) }}
            disabled={adding || !addTicker.trim()}
            className="px-6 py-2.5 bg-white text-black font-semibold text-sm rounded-lg
                       hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 transition-all"
          >
            {adding ? 'Adding...' : 'Add'}
          </button>
        </div>
      </div>

      {/* Portfolio Visualizer Button */}
      {watchlist.length >= 2 && (
        <div className="mb-6">
          <button
            onClick={() => {
              setShowBacktest(!showBacktest)
              if (!showBacktest && !backtestData) fetchBacktest()
            }}
            className="px-5 py-2.5 bg-purple-500/10 border border-purple-500/30 text-purple-400
                       font-semibold text-sm rounded-lg hover:bg-purple-500/20 transition-all"
          >
            {showBacktest ? 'Hide' : 'Show'} Portfolio Visualizer
          </button>
        </div>
      )}

      {/* Portfolio Visualizer Panel */}
      {showBacktest && (
        <div className="bg-black border border-purple-500/30 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-5">
            <div>
              <h2 className="text-white font-bold text-lg">Portfolio Visualizer</h2>
              <p className="text-neutral-500 text-xs mt-1">Returns since added, correlations, and risk metrics</p>
            </div>
            <button
              onClick={fetchBacktest}
              disabled={backtestLoading}
              className="px-3 py-1.5 text-xs bg-purple-500/20 text-purple-300 rounded hover:bg-purple-500/30"
            >
              {backtestLoading ? 'Loading...' : 'Refresh'}
            </button>
          </div>

          {backtestLoading && !backtestData && (
            <div className="text-neutral-500 text-sm text-center py-8">Analyzing portfolio...</div>
          )}

          {backtestData && (
            <div className="space-y-6">
              {/* Equal-Weight Portfolio Stats */}
              <div className="bg-neutral-900/50 border border-neutral-800 rounded-lg p-4">
                <p className="text-neutral-500 text-xs uppercase tracking-wider mb-3">Equal-Weight Portfolio</p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                  <div>
                    <p className="text-neutral-500 text-[10px]">Return Since Added</p>
                    <p className={`text-lg font-bold font-mono ${
                      backtestData.portfolio_stats.total_return >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}>
                      {backtestData.portfolio_stats.total_return >= 0 ? '+' : ''}{backtestData.portfolio_stats.total_return}%
                    </p>
                  </div>
                  <div>
                    <p className="text-neutral-500 text-[10px]">Volatility</p>
                    <p className="text-white text-lg font-bold font-mono">{backtestData.portfolio_stats.annualized_vol}%</p>
                  </div>
                  <div>
                    <p className="text-neutral-500 text-[10px]">Sharpe Ratio</p>
                    <p className={`text-lg font-bold font-mono ${
                      backtestData.portfolio_stats.sharpe_ratio >= 1 ? 'text-green-400' :
                      backtestData.portfolio_stats.sharpe_ratio >= 0 ? 'text-yellow-400' : 'text-red-400'
                    }`}>
                      {backtestData.portfolio_stats.sharpe_ratio}
                    </p>
                  </div>
                  {backtestData.portfolio_stats.diversification_benefit !== undefined && (
                    <div>
                      <p className="text-neutral-500 text-[10px]">Diversification Benefit</p>
                      <p className="text-green-400 text-lg font-bold font-mono">
                        -{backtestData.portfolio_stats.diversification_benefit}% vol
                      </p>
                    </div>
                  )}
                </div>
              </div>

              {/* Individual Stock Stats */}
              <div>
                <p className="text-neutral-500 text-xs uppercase tracking-wider mb-3">Stock Performance</p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-neutral-800">
                        <th className="text-left text-neutral-500 text-[10px] py-2 px-3">TICKER</th>
                        <th className="text-right text-neutral-500 text-[10px] py-2 px-3">RETURN SINCE ADDED</th>
                        <th className="text-right text-neutral-500 text-[10px] py-2 px-3">ANN. VOL</th>
                        <th className="text-right text-neutral-500 text-[10px] py-2 px-3">SHARPE</th>
                        <th className="text-right text-neutral-500 text-[10px] py-2 px-3">MAX DD</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(backtestData.stock_stats)
                        .sort(([,a], [,b]) => b.total_return - a.total_return)
                        .map(([sym, stats]) => (
                        <tr key={sym} className="border-b border-neutral-900 hover:bg-neutral-900/50">
                          <td className="py-2 px-3 font-mono font-bold text-white text-xs">{sym}</td>
                          <td className={`py-2 px-3 text-right font-mono text-xs font-bold ${
                            stats.total_return >= 0 ? 'text-green-400' : 'text-red-400'
                          }`}>
                            {stats.total_return >= 0 ? '+' : ''}{stats.total_return}%
                          </td>
                          <td className="py-2 px-3 text-right font-mono text-xs text-neutral-400">{stats.annualized_vol}%</td>
                          <td className={`py-2 px-3 text-right font-mono text-xs ${
                            stats.sharpe_ratio >= 1 ? 'text-green-400' : stats.sharpe_ratio >= 0 ? 'text-neutral-300' : 'text-red-400'
                          }`}>
                            {stats.sharpe_ratio}
                          </td>
                          <td className="py-2 px-3 text-right font-mono text-xs text-red-400">{stats.max_drawdown}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Correlation Matrix */}
              {backtestData.correlation_matrix && Object.keys(backtestData.correlation_matrix).length >= 2 && (
                <div>
                  <p className="text-neutral-500 text-xs uppercase tracking-wider mb-3">
                    Correlation Matrix
                    <span className="text-neutral-600 ml-2 normal-case">
                      (green = diversified, red = correlated)
                    </span>
                  </p>
                  <div className="overflow-x-auto">
                    <table className="text-[11px]">
                      <thead>
                        <tr>
                          <th className="px-2 py-1.5 text-neutral-500"></th>
                          {backtestData.tickers.map(t => (
                            <th key={t} className="px-2 py-1.5 text-neutral-400 font-mono">{t}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {backtestData.tickers.map(t1 => (
                          <tr key={t1}>
                            <td className="px-2 py-1.5 text-neutral-400 font-mono font-bold">{t1}</td>
                            {backtestData.tickers.map(t2 => (
                              <CorrelationCell
                                key={t2}
                                value={backtestData.correlation_matrix[t1]?.[t2] ?? 0}
                              />
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

      {/* Watchlist with quant stats */}
      {watchlist.length > 0 ? (
        <div className="space-y-3">
          {watchlist.map((stock) => {
            const qd = quantData[stock.ticker]
            const isExpanded = expandedStock === stock.ticker
            const isQuantLoading = quantLoading[stock.ticker]

            return (
              <div key={stock.ticker} className="bg-black border border-neutral-700 rounded-xl overflow-hidden">
                {/* Main row */}
                <div
                  className="flex items-center justify-between px-5 py-4 cursor-pointer hover:bg-neutral-900/50 transition-colors"
                  onClick={() => setExpandedStock(isExpanded ? null : stock.ticker)}
                >
                  <div className="flex items-center gap-4 flex-1 min-w-0">
                    {/* Ticker + Name */}
                    <div className="min-w-[120px]">
                      <span className="text-white font-mono font-bold text-sm">{stock.ticker}</span>
                      {stock.name && (
                        <p className="text-neutral-500 text-[11px] mt-0.5 truncate max-w-[160px]">{stock.name}</p>
                      )}
                    </div>

                    {/* Price */}
                    <div className="text-right min-w-[80px]">
                      <p className="text-white font-mono text-sm font-semibold">
                        {loading[stock.ticker] ? '...' : `$${stock.current_price}`}
                      </p>
                      <p className={`font-mono text-[11px] ${
                        (stock.change_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}>
                        {(stock.change_pct || 0) >= 0 ? '+' : ''}{stock.change_pct || 0}%
                      </p>
                    </div>

                    {/* Quant Signal Badge */}
                    {qd && (
                      <div className="flex items-center gap-3">
                        <span className={`px-2.5 py-1 text-[11px] font-bold rounded border ${
                          SIGNAL_COLORS[qd.signal] || 'bg-neutral-800 text-neutral-400'
                        }`}>
                          {qd.signal}
                        </span>
                        <ConfidenceBar value={qd.confidence} />
                      </div>
                    )}

                    {isQuantLoading && !qd && (
                      <span className="text-neutral-600 text-[11px]">Analyzing...</span>
                    )}

                    {/* Quick factors */}
                    {qd && (
                      <div className="hidden lg:flex items-center gap-3">
                        <span className={`text-[10px] font-mono px-2 py-0.5 rounded ${
                          qd.factors.momentum.value > 0 ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
                        }`}>
                          MTM {qd.factors.momentum.label}
                        </span>
                        <span className={`text-[10px] font-mono px-2 py-0.5 rounded ${
                          qd.factors.rsi14.value < 30 ? 'bg-green-500/10 text-green-400' :
                          qd.factors.rsi14.value > 70 ? 'bg-red-500/10 text-red-400' :
                          'bg-neutral-800 text-neutral-400'
                        }`}>
                          RSI {qd.factors.rsi14.value}
                        </span>
                        <span className="text-[10px] font-mono text-neutral-500 px-2 py-0.5 rounded bg-neutral-900">
                          {qd.factors.volatility.label}
                        </span>
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 ml-4">
                    <button
                      onClick={(e) => { e.stopPropagation(); refreshPrice(stock.ticker); fetchQuantAnalysis(stock.ticker) }}
                      disabled={loading[stock.ticker]}
                      className="text-neutral-500 hover:text-white text-xs transition-colors"
                    >
                      {loading[stock.ticker] ? '...' : 'Refresh'}
                    </button>
                    <span className="text-neutral-800">|</span>
                    <button
                      onClick={(e) => { e.stopPropagation(); removeStock(stock.ticker) }}
                      className="text-neutral-500 hover:text-red-500 text-xs transition-colors"
                    >
                      Remove
                    </button>
                    <svg
                      className={`w-4 h-4 text-neutral-600 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                      fill="none" stroke="currentColor" viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </div>

                {/* Expanded quant details */}
                {isExpanded && qd && (
                  <div className="border-t border-neutral-800 px-5 py-4 bg-neutral-950">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                      {/* Factors */}
                      <div>
                        <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-2">Factor Analysis</p>
                        <div className="space-y-1.5">
                          {Object.entries(qd.factors).map(([key, val]) => (
                            <div key={key} className="flex items-center justify-between">
                              <span className="text-neutral-400 text-[11px] capitalize">{key.replace('_', ' ')}</span>
                              <span className="text-white text-[11px] font-mono">{val.label}</span>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* Technicals */}
                      <div>
                        <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-2">Technicals</p>
                        <div className="space-y-1.5">
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">EMA Trend</span>
                            <span className={`text-[11px] font-mono ${
                              qd.technicals.ema_trend === 'Bullish' ? 'text-green-400' :
                              qd.technicals.ema_trend === 'Bearish' ? 'text-red-400' : 'text-yellow-400'
                            }`}>{qd.technicals.ema_trend}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Above 200 SMA</span>
                            <span className={`text-[11px] font-mono ${qd.technicals.above_200sma ? 'text-green-400' : 'text-red-400'}`}>
                              {qd.technicals.above_200sma ? 'Yes' : 'No'}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Above 50 EMA</span>
                            <span className={`text-[11px] font-mono ${qd.technicals.above_50ema ? 'text-green-400' : 'text-red-400'}`}>
                              {qd.technicals.above_50ema ? 'Yes' : 'No'}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">EMA 9 / 21 / 50</span>
                            <span className="text-neutral-300 text-[11px] font-mono">
                              {qd.technicals.ema_9} / {qd.technicals.ema_21} / {qd.technicals.ema_50}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Bollinger</span>
                            <span className="text-neutral-300 text-[11px] font-mono">
                              {qd.technicals.bb_lower} - {qd.technicals.bb_upper}
                            </span>
                          </div>
                        </div>
                      </div>

                      {/* Context */}
                      <div>
                        <p className="text-neutral-500 text-[10px] uppercase tracking-wider mb-2">Market Context</p>
                        <div className="space-y-1.5">
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Regime</span>
                            <span className={`text-[11px] font-bold ${
                              qd.regime === 'BULL' ? 'text-green-400' : qd.regime === 'BEAR' ? 'text-red-400' : 'text-yellow-400'
                            }`}>{qd.regime}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Regime Confidence</span>
                            <span className="text-neutral-300 text-[11px] font-mono">{qd.regime_confidence}%</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Sector</span>
                            <span className="text-neutral-300 text-[11px]">{qd.sector}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">Macro Impact</span>
                            <span className={`text-[11px] font-mono ${
                              qd.macro_impact > 0 ? 'text-green-400' : qd.macro_impact < 0 ? 'text-red-400' : 'text-neutral-400'
                            }`}>
                              {qd.macro_impact > 0 ? '+' : ''}{qd.macro_impact}
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">1M Return</span>
                            <span className={`text-[11px] font-mono ${
                              qd.returns['1m'] >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {qd.returns['1m'] >= 0 ? '+' : ''}{qd.returns['1m']}%
                            </span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-neutral-400 text-[11px]">3M Return</span>
                            <span className={`text-[11px] font-mono ${
                              qd.returns['3m'] >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {qd.returns['3m'] >= 0 ? '+' : ''}{qd.returns['3m']}%
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Composite Score Bar */}
                    <div className="mt-4 pt-3 border-t border-neutral-800">
                      <div className="flex items-center justify-between">
                        <span className="text-neutral-500 text-[10px] uppercase tracking-wider">Composite Score</span>
                        <div className="flex items-center gap-3">
                          <span className={`text-sm font-bold font-mono ${
                            qd.composite_score > 0 ? 'text-green-400' : qd.composite_score < 0 ? 'text-red-400' : 'text-neutral-400'
                          }`}>
                            {qd.composite_score > 0 ? '+' : ''}{qd.composite_score}
                          </span>
                          <span className="text-neutral-600 text-[10px]">Direction: {qd.direction}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {isExpanded && !qd && (
                  <div className="border-t border-neutral-800 px-5 py-6 bg-neutral-950 text-center">
                    <button
                      onClick={() => fetchQuantAnalysis(stock.ticker)}
                      className="text-purple-400 text-sm hover:text-purple-300"
                    >
                      Load Quant Analysis
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
          <div className="w-16 h-16 bg-neutral-900 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-neutral-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 4v16m8-8H4" />
            </svg>
          </div>
          <p className="text-neutral-400 font-medium mb-1">Your watchlist is empty</p>
          <p className="text-neutral-600 text-sm">Add stocks above to start tracking with full quant analytics</p>
        </div>
      )}

      <p className="text-neutral-700 text-[11px] mt-6 text-center">
        Watchlist is stored locally on your device. Data from Yahoo Finance. Not financial advice.
      </p>
    </div>
  )
}
