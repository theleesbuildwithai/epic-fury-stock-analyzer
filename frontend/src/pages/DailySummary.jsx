import { useState, useEffect } from 'react'

function getWatchlistTickers() {
  try {
    const data = localStorage.getItem('epic_fury_watchlist')
    if (!data) return ''
    const list = JSON.parse(data)
    return list.map(s => s.ticker).filter(Boolean).join(',')
  } catch {
    return ''
  }
}

export default function DailySummary() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastRefresh, setLastRefresh] = useState(null)

  const fetchSummary = async () => {
    setLoading(true)
    try {
      const wl = getWatchlistTickers()
      const url = wl ? `/api/daily-summary?watchlist=${encodeURIComponent(wl)}` : '/api/daily-summary'
      const res = await fetch(url)
      const json = await res.json()
      setData(json)
      setLastRefresh(new Date().toLocaleTimeString())
    } catch {
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchSummary()
    const interval = setInterval(fetchSummary, 300000)
    return () => clearInterval(interval)
  }, [])

  const changeColor = (pct) => {
    if (pct > 0) return 'text-green-500'
    if (pct < 0) return 'text-red-500'
    return 'text-neutral-400'
  }

  const changeBg = (pct) => {
    if (pct > 2) return 'bg-green-500/10 border-green-500/30'
    if (pct > 0) return 'bg-green-500/5 border-green-500/20'
    if (pct < -2) return 'bg-red-500/10 border-red-500/30'
    if (pct < 0) return 'bg-red-500/5 border-red-500/20'
    return 'bg-neutral-800/50 border-neutral-700/30'
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Daily AI Summary</h1>
          <p className="text-neutral-400">
            Today's market movers, biggest gains & losses, and your watchlist at a glance.
          </p>
        </div>
        <div className="text-right">
          {lastRefresh && (
            <p className="text-neutral-600 text-xs mb-2">Updated: {lastRefresh}</p>
          )}
          <button
            onClick={fetchSummary}
            disabled={loading}
            className="px-4 py-2 bg-white text-black text-sm font-bold rounded-lg hover:bg-neutral-200 transition-colors disabled:opacity-50"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      {loading && !data ? (
        <div className="text-center py-20">
          <div className="inline-block w-10 h-10 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
          <p className="text-neutral-500 mt-4">Analyzing today's market...</p>
        </div>
      ) : data?.error && !data.gainers?.length ? (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-6 text-center">
          <p className="text-red-400">{data.error}</p>
        </div>
      ) : data ? (
        <>
          {/* Market Overview Card */}
          {data.market_overview && (
            <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-6">
              <h2 className="text-lg font-bold text-white mb-4">Market Overview</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-neutral-900 rounded-lg p-4 text-center">
                  <p className="text-neutral-500 text-xs uppercase mb-1">Mood</p>
                  <p className={`text-xl font-bold ${
                    data.market_overview.mood.includes('Bullish') ? 'text-green-500' : 'text-red-500'
                  }`}>
                    {data.market_overview.mood}
                  </p>
                </div>
                <div className="bg-neutral-900 rounded-lg p-4 text-center">
                  <p className="text-neutral-500 text-xs uppercase mb-1">Avg Change</p>
                  <p className={`text-xl font-bold ${changeColor(data.market_overview.avg_change_pct)}`}>
                    {data.market_overview.avg_change_pct > 0 ? '+' : ''}{data.market_overview.avg_change_pct}%
                  </p>
                </div>
                <div className="bg-neutral-900 rounded-lg p-4 text-center">
                  <p className="text-neutral-500 text-xs uppercase mb-1">Advancing</p>
                  <p className="text-xl font-bold text-green-500">{data.market_overview.advancing}</p>
                </div>
                <div className="bg-neutral-900 rounded-lg p-4 text-center">
                  <p className="text-neutral-500 text-xs uppercase mb-1">Declining</p>
                  <p className="text-xl font-bold text-red-500">{data.market_overview.declining}</p>
                </div>
              </div>
            </div>
          )}

          {/* Gainers & Losers side by side */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
            {/* Top Gainers */}
            <div className="bg-black border border-neutral-700 rounded-xl p-6">
              <h2 className="text-xl font-bold text-white mb-1">Top Gainers</h2>
              <p className="text-neutral-500 text-sm mb-4">Biggest movers up today</p>
              <div className="space-y-2">
                {data.gainers?.map((stock, i) => (
                  <div key={stock.symbol} className={`flex items-center justify-between p-3 rounded-lg border ${changeBg(stock.change_pct)}`}>
                    <div className="flex items-center gap-3">
                      <span className="text-neutral-500 text-sm font-bold w-5">{i + 1}</span>
                      <div>
                        <span className="text-white font-mono font-bold text-sm">{stock.symbol}</span>
                        <p className="text-neutral-500 text-xs">{stock.name}</p>
                      </div>
                    </div>
                    <div className="text-right">
                      <span className="text-white font-mono text-sm">${stock.price}</span>
                      <p className={`text-sm font-bold ${changeColor(stock.change_pct)}`}>
                        +{stock.change_pct}%
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Top Losers */}
            <div className="bg-black border border-neutral-700 rounded-xl p-6">
              <h2 className="text-xl font-bold text-white mb-1">Top Losers</h2>
              <p className="text-neutral-500 text-sm mb-4">Biggest decliners today</p>
              <div className="space-y-2">
                {data.losers?.map((stock, i) => (
                  <div key={stock.symbol} className={`flex items-center justify-between p-3 rounded-lg border ${changeBg(stock.change_pct)}`}>
                    <div className="flex items-center gap-3">
                      <span className="text-neutral-500 text-sm font-bold w-5">{i + 1}</span>
                      <div>
                        <span className="text-white font-mono font-bold text-sm">{stock.symbol}</span>
                        <p className="text-neutral-500 text-xs">{stock.name}</p>
                      </div>
                    </div>
                    <div className="text-right">
                      <span className="text-white font-mono text-sm">${stock.price}</span>
                      <p className={`text-sm font-bold ${changeColor(stock.change_pct)}`}>
                        {stock.change_pct}%
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Watchlist Summary */}
          {data.watchlist_summary && data.watchlist_summary.length > 0 && (
            <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-6">
              <h2 className="text-xl font-bold text-white mb-1">Your Watchlist Summary</h2>
              <p className="text-neutral-500 text-sm mb-4">Performance of stocks on your watchlist</p>
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-neutral-800">
                      <th className="text-left text-neutral-500 text-xs font-medium py-3 px-2">SYMBOL</th>
                      <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">PRICE</th>
                      <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">TODAY</th>
                      <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">WEEK</th>
                      <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">MONTH</th>
                      <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">RSI</th>
                      <th className="text-left text-neutral-500 text-xs font-medium py-3 px-2">SIGNAL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.watchlist_summary.map((stock) => (
                      <tr key={stock.symbol} className="border-b border-neutral-900 hover:bg-neutral-900/50 transition-colors">
                        <td className="py-3 px-2">
                          <span className="text-white font-mono font-bold text-sm">{stock.symbol}</span>
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className="text-white font-mono text-sm">${stock.price}</span>
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className={`font-mono text-sm font-bold ${changeColor(stock.day_change_pct)}`}>
                            {stock.day_change_pct > 0 ? '+' : ''}{stock.day_change_pct}%
                          </span>
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className={`font-mono text-sm ${changeColor(stock.week_change_pct)}`}>
                            {stock.week_change_pct > 0 ? '+' : ''}{stock.week_change_pct}%
                          </span>
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className={`font-mono text-sm ${changeColor(stock.month_change_pct)}`}>
                            {stock.month_change_pct > 0 ? '+' : ''}{stock.month_change_pct}%
                          </span>
                        </td>
                        <td className="py-3 px-2 text-right">
                          <span className={`font-mono text-sm ${
                            stock.rsi < 30 ? 'text-green-500' :
                            stock.rsi > 70 ? 'text-red-500' : 'text-neutral-300'
                          }`}>
                            {stock.rsi}
                          </span>
                        </td>
                        <td className="py-3 px-2">
                          <span className={`text-xs font-medium px-2 py-1 rounded ${
                            stock.signal.includes('Buy') ? 'text-green-400 bg-green-500/10' :
                            stock.signal.includes('Sell') ? 'text-red-400 bg-red-500/10' :
                            'text-neutral-400 bg-neutral-500/10'
                          }`}>
                            {stock.signal}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {!data.watchlist_summary?.length && (
            <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-6 text-center">
              <p className="text-neutral-400">Add stocks to your <a href="/watchlist" className="text-white underline hover:text-neutral-300">Watchlist</a> to see a personalized summary here.</p>
            </div>
          )}

          <p className="text-neutral-600 text-xs mt-6 text-center italic">
            Data from Yahoo Finance. Auto-refreshes every 5 minutes during market hours.
            NOT financial advice. Always do your own research.
          </p>
        </>
      ) : (
        <div className="text-center py-20">
          <p className="text-neutral-500">Failed to load summary. Try refreshing.</p>
        </div>
      )}
    </div>
  )
}
