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

export default function Watchlist() {
  const [watchlist, setWatchlist] = useState([])
  const [loading, setLoading] = useState({})
  const [addTicker, setAddTicker] = useState('')
  const [adding, setAdding] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [showSuggestions, setShowSuggestions] = useState(false)

  useEffect(() => {
    const wl = getWatchlist()
    setWatchlist(wl)
    // Refresh prices for all watchlist stocks
    wl.forEach(stock => refreshPrice(stock.ticker))

    const isMarketHours = () => {
      const now = new Date()
      const hours = now.getHours()
      const mins = now.getMinutes()
      const t = hours * 60 + mins
      return t >= 390 && t <= 1050
    }

    // Auto-refresh every 60s during market hours
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
    } catch {
      // Silent fail
    }
    setAdding(false)
  }

  const removeStock = (ticker) => {
    const updated = watchlist.filter(s => s.ticker !== ticker)
    setWatchlist(updated)
    saveWatchlist(updated)
  }

  const totalValue = watchlist.reduce((sum, s) => sum + (s.current_price || 0), 0)
  const totalChange = watchlist.reduce((sum, s) => sum + (s.change || 0), 0)

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Watchlist</h1>
          <p className="text-neutral-400">
            Track stocks you own or are watching. Prices auto-update every 60s during market hours.
          </p>
        </div>

        {/* Summary */}
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

      {/* Watchlist table */}
      {watchlist.length > 0 ? (
        <div className="bg-black border border-neutral-700 rounded-xl overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-neutral-800">
                <th className="text-left text-neutral-500 text-xs font-medium py-3 px-5">STOCK</th>
                <th className="text-right text-neutral-500 text-xs font-medium py-3 px-4">ENTRY PRICE</th>
                <th className="text-right text-neutral-500 text-xs font-medium py-3 px-4">CURRENT</th>
                <th className="text-right text-neutral-500 text-xs font-medium py-3 px-4">P/L</th>
                <th className="text-right text-neutral-500 text-xs font-medium py-3 px-4">P/L %</th>
                <th className="text-center text-neutral-500 text-xs font-medium py-3 px-4">ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {watchlist.map((stock) => (
                <tr key={stock.ticker} className="border-b border-neutral-900 hover:bg-neutral-900/50 transition-colors">
                  <td className="py-4 px-5">
                    <div>
                      <span className="text-white font-mono font-bold text-sm">{stock.ticker}</span>
                      {stock.name && (
                        <p className="text-neutral-500 text-xs mt-0.5 truncate max-w-[200px]">{stock.name}</p>
                      )}
                    </div>
                  </td>
                  <td className="py-4 px-4 text-right">
                    <span className="text-neutral-400 font-mono text-sm">${stock.entry_price}</span>
                  </td>
                  <td className="py-4 px-4 text-right">
                    <span className="text-white font-mono text-sm font-semibold">
                      {loading[stock.ticker] ? '...' : `$${stock.current_price}`}
                    </span>
                  </td>
                  <td className="py-4 px-4 text-right">
                    <span className={`font-mono text-sm font-bold ${
                      (stock.change || 0) >= 0 ? 'text-green-500' : 'text-red-500'
                    }`}>
                      {(stock.change || 0) >= 0 ? '+' : ''}{stock.change || 0}
                    </span>
                  </td>
                  <td className="py-4 px-4 text-right">
                    <span className={`font-mono text-sm font-bold px-2 py-0.5 rounded ${
                      (stock.change_pct || 0) >= 0
                        ? 'text-green-400 bg-green-500/10'
                        : 'text-red-400 bg-red-500/10'
                    }`}>
                      {(stock.change_pct || 0) >= 0 ? '+' : ''}{stock.change_pct || 0}%
                    </span>
                  </td>
                  <td className="py-4 px-4 text-center">
                    <div className="flex items-center justify-center gap-2">
                      <button
                        onClick={() => refreshPrice(stock.ticker)}
                        disabled={loading[stock.ticker]}
                        className="text-neutral-500 hover:text-white text-xs transition-colors"
                        title="Refresh price"
                      >
                        {loading[stock.ticker] ? '...' : 'Refresh'}
                      </button>
                      <span className="text-neutral-800">|</span>
                      <button
                        onClick={() => removeStock(stock.ticker)}
                        className="text-neutral-500 hover:text-red-500 text-xs transition-colors"
                        title="Remove from watchlist"
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
          <div className="w-16 h-16 bg-neutral-900 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-neutral-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 4v16m8-8H4" />
            </svg>
          </div>
          <p className="text-neutral-400 font-medium mb-1">Your watchlist is empty</p>
          <p className="text-neutral-600 text-sm">Add stocks above to start tracking your portfolio</p>
        </div>
      )}

      <p className="text-neutral-700 text-[11px] mt-6 text-center">
        Watchlist is stored locally on your device. Data from Yahoo Finance. Not financial advice.
      </p>
    </div>
  )
}
