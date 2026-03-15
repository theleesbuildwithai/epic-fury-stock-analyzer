import { useState, useEffect } from 'react'
import PriceChart from './PriceChart'
import TechnicalIndicators from './TechnicalIndicators'
import RiskScore from './RiskScore'
import KeyStats from './KeyStats'
import PriceForecast from './PriceForecast'

function getWatchlist() {
  try {
    const data = localStorage.getItem('epic_fury_watchlist')
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function addToWatchlist(stock) {
  const list = getWatchlist()
  if (list.some(s => s.ticker === stock.ticker)) return
  list.push(stock)
  localStorage.setItem('epic_fury_watchlist', JSON.stringify(list))
}

function removeFromWatchlist(ticker) {
  const list = getWatchlist().filter(s => s.ticker !== ticker)
  localStorage.setItem('epic_fury_watchlist', JSON.stringify(list))
}

function isInWatchlist(ticker) {
  return getWatchlist().some(s => s.ticker === ticker)
}

export default function AnalysisDashboard({ data }) {
  const [added, setAdded] = useState(false)
  const [onWatchlist, setOnWatchlist] = useState(false)

  useEffect(() => {
    if (data?.info?.ticker) {
      setOnWatchlist(isInWatchlist(data.info.ticker))
      setAdded(false)
    }
  }, [data?.info?.ticker])

  if (!data) return null

  const handleAddToWatchlist = () => {
    const stock = {
      ticker: data.info.ticker,
      name: data.info.name || data.info.ticker,
      entry_price: data.latest.price,
      current_price: data.latest.price,
      change: 0,
      change_pct: 0,
      added_at: new Date().toISOString(),
      last_updated: new Date().toISOString(),
    }
    addToWatchlist(stock)
    setOnWatchlist(true)
    setAdded(true)
    setTimeout(() => setAdded(false), 3000)
  }

  const handleRemoveFromWatchlist = () => {
    removeFromWatchlist(data.info.ticker)
    setOnWatchlist(false)
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-8 space-y-6">
      {/* Top: Key Stats */}
      <KeyStats
        info={data.info}
        latest={data.latest}
        supportResistance={data.support_resistance}
      />

      {/* Price Forecast — the main prediction section */}
      <PriceForecast forecast={data.forecast} />

      {/* Middle: Signal + Risk side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <RiskScore risk={data.risk} signal={data.signal} />

          {/* Add to / Remove from Watchlist */}
          {onWatchlist ? (
            <div className="mt-4 space-y-2">
              <div className="w-full py-3 px-4 bg-neutral-900 text-green-500 font-medium rounded-lg
                              border border-green-500/30 text-center text-sm flex items-center justify-center gap-2">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                On Your Watchlist
              </div>
              <button
                onClick={handleRemoveFromWatchlist}
                className="w-full py-2 px-4 text-neutral-500 hover:text-red-500 text-xs
                           hover:bg-neutral-900 rounded-lg transition-colors"
              >
                Remove from watchlist
              </button>
            </div>
          ) : (
            <>
              <button
                onClick={handleAddToWatchlist}
                className="mt-4 w-full py-3 px-4 bg-white hover:bg-neutral-200
                           text-black font-semibold rounded-lg transition-colors shadow-lg shadow-white/5"
              >
                {added ? '✓ Added to Watchlist' : '+ Add to Watchlist'}
              </button>
              <p className="text-neutral-600 text-xs mt-2 text-center">
                Track this stock on the Watchlist page
              </p>
            </>
          )}
        </div>

        <div className="lg:col-span-2">
          {/* Price Chart */}
          <PriceChart chartData={data.chart_data} />
        </div>
      </div>

      {/* Bottom: Technical Indicators */}
      <TechnicalIndicators chartData={data.chart_data} />

      {/* Trend Summary */}
      <div className="bg-black rounded-xl p-6 border border-neutral-700">
        <h2 className="text-xl font-semibold text-white mb-3">Trend Analysis</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <p className="text-neutral-400 text-sm">Direction</p>
            <p className={`text-lg font-medium ${
              data.trend.direction.includes('bullish') ? 'text-green-500' :
              data.trend.direction.includes('bearish') ? 'text-red-500' : 'text-white'
            }`}>
              {data.trend.direction.replace('_', ' ')}
            </p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Strength</p>
            <p className="text-lg font-medium text-white">{data.trend.strength}%</p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Price vs 20-day MA</p>
            <p className={`text-lg font-medium ${data.trend.price_vs_sma20 >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {data.trend.price_vs_sma20 >= 0 ? '+' : ''}{data.trend.price_vs_sma20}%
            </p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Price vs 50-day MA</p>
            <p className={`text-lg font-medium ${data.trend.price_vs_sma50 >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {data.trend.price_vs_sma50 >= 0 ? '+' : ''}{data.trend.price_vs_sma50}%
            </p>
          </div>
        </div>

        {/* Volume Analysis */}
        {data.volume_analysis && (
          <div className="mt-4 pt-4 border-t border-neutral-700">
            <h3 className="text-sm font-medium text-neutral-400 mb-2">Volume Analysis</h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <p className="text-neutral-500 text-xs">Volume Trend</p>
                <p className="text-white">{data.volume_analysis.volume_trend}</p>
              </div>
              <div>
                <p className="text-neutral-500 text-xs">Volume Ratio</p>
                <p className="text-white">{data.volume_analysis.volume_ratio}x avg</p>
              </div>
              <div>
                <p className="text-neutral-500 text-xs">Unusual Volume?</p>
                <p className={data.volume_analysis.unusual_volume ? 'text-red-500' : 'text-neutral-400'}>
                  {data.volume_analysis.unusual_volume ? 'Yes' : 'No'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
