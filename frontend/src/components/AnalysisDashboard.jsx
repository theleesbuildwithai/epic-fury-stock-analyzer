import { useState, useEffect } from 'react'
import PriceChart from './PriceChart'
import CandlestickChart from './CandlestickChart'
import TechnicalIndicators from './TechnicalIndicators'
import RiskScore from './RiskScore'
import KeyStats from './KeyStats'
import PriceForecast from './PriceForecast'

function getWatchlist() {
  try {
    const data = localStorage.getItem('sentinel_quant_watchlist')
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function addToWatchlist(stock) {
  const list = getWatchlist()
  if (list.some(s => s.ticker === stock.ticker)) return
  list.push(stock)
  localStorage.setItem('sentinel_quant_watchlist', JSON.stringify(list))
}

function removeFromWatchlist(ticker) {
  const list = getWatchlist().filter(s => s.ticker !== ticker)
  localStorage.setItem('sentinel_quant_watchlist', JSON.stringify(list))
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
    if (!data.info || !data.latest) return
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

      {/* Hold Duration & Action Recommendation */}
      {data.hold_duration && (
        <div className={`bg-black rounded-xl p-6 border border-neutral-700 ${
          data.signal?.direction?.includes('Buy') ? 'card-accent-green' :
          data.signal?.direction?.includes('Sell') ? 'card-accent-red' : 'card-accent-top'
        }`}>
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
            <div>
              <p className="text-[10px] font-bold tracking-[0.2em] uppercase text-neutral-500 mb-1">Investment Action</p>
              <div className="flex items-center gap-4">
                <span className={`text-4xl font-black ${
                  data.signal?.direction?.includes('Buy') ? 'text-gradient-green' :
                  data.signal?.direction?.includes('Sell') ? 'text-gradient-red' : 'text-neutral-300'
                }`}>
                  {data.signal?.direction || 'N/A'}
                </span>
                <div className="h-8 w-px bg-neutral-800" />
                <div>
                  <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Confidence</p>
                  <p className="text-white font-mono font-bold">{data.signal?.confidence ?? 0}%</p>
                </div>
              </div>
            </div>
            <div className="flex items-center gap-6">
              <div className="text-center bg-neutral-900/50 rounded-lg px-5 py-3 border border-neutral-800/50">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Hold For</p>
                <p className="text-white font-bold text-xl">{data.hold_duration.label}</p>
                <p className="text-neutral-600 text-xs font-mono">~{data.hold_duration.days} days</p>
              </div>
              <div className="text-center bg-neutral-900/50 rounded-lg px-5 py-3 border border-neutral-800/50">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Entry</p>
                <p className="text-white font-medium text-sm">{data.hold_duration.entry_guidance}</p>
              </div>
            </div>
          </div>

          {/* Hold reasoning */}
          {data.hold_duration.reasoning && (
            <div className="mt-4 pt-4 border-t border-neutral-800">
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">Analysis</p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {Array.isArray(data.hold_duration.reasoning)
                  ? data.hold_duration.reasoning.map((r, i) => (
                      <p key={i} className="text-neutral-400 text-sm">• {r}</p>
                    ))
                  : <p className="text-neutral-400 text-sm">• {data.hold_duration.reasoning}</p>
                }
              </div>
            </div>
          )}

          {/* EMA levels */}
          <div className="mt-4 pt-4 border-t border-neutral-800 flex flex-wrap gap-4">
            <div>
              <span className="text-neutral-500 text-xs">EMA 9</span>
              <p className="text-white font-mono text-sm">${data.hold_duration.ema_9}</p>
            </div>
            <div>
              <span className="text-neutral-500 text-xs">EMA 21</span>
              <p className="text-white font-mono text-sm">${data.hold_duration.ema_21}</p>
            </div>
            <div>
              <span className="text-neutral-500 text-xs">EMA 50</span>
              <p className="text-white font-mono text-sm">${data.hold_duration.ema_50}</p>
            </div>
          </div>
        </div>
      )}

      {/* News Sentiment & Current Events */}
      {data.news_sentiment && (
        <div className="bg-black rounded-xl p-6 border border-neutral-700 card-accent-top">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold text-white flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-green-500 pulse-dot" />
              News & Current Events
            </h2>
            <span className={`text-sm font-bold px-3 py-1 rounded-full ${
              data.news_sentiment.market_sentiment?.label?.includes('Bullish') ? 'bg-green-500/10 text-green-500' :
              data.news_sentiment.market_sentiment?.label?.includes('Bearish') ? 'bg-red-500/10 text-red-500' :
              'bg-neutral-500/10 text-neutral-400'
            }`}>
              Market: {data.news_sentiment.market_sentiment?.label || 'N/A'}
            </span>
          </div>

          {/* Stock-specific headlines */}
          {data.news_sentiment.stock_headlines && data.news_sentiment.stock_headlines.length > 0 && (
            <div className="mb-4">
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">
                {data.info.ticker} in the News
              </p>
              {data.news_sentiment.stock_headlines.map((h, i) => (
                <div key={i} className="flex items-center gap-2 py-1.5 border-b border-neutral-900">
                  <span className={`w-1.5 h-1.5 rounded-full ${
                    h.sentiment > 0 ? 'bg-green-500' : h.sentiment < 0 ? 'bg-red-500' : 'bg-neutral-500'
                  }`}></span>
                  {h.link ? (
                    <a href={h.link} target="_blank" rel="noopener noreferrer" className="text-neutral-300 text-sm truncate hover:text-white transition-colors">{h.title}</a>
                  ) : (
                    <span className="text-neutral-300 text-sm truncate">{h.title}</span>
                  )}
                  <span className="text-neutral-600 text-xs shrink-0">{h.source}</span>
                </div>
              ))}
            </div>
          )}

          {/* Macro events */}
          {data.news_sentiment.macro_events && data.news_sentiment.macro_events.length > 0 && (
            <div>
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">Macro Events Affecting Markets</p>
              {data.news_sentiment.macro_events.slice(0, 4).map((h, i) => (
                <div key={i} className="flex items-center gap-2 py-1.5 border-b border-neutral-900">
                  <span className={`w-1.5 h-1.5 rounded-full ${
                    h.sentiment > 0 ? 'bg-green-500' : h.sentiment < 0 ? 'bg-red-500' : 'bg-neutral-500'
                  }`}></span>
                  {h.link ? (
                    <a href={h.link} target="_blank" rel="noopener noreferrer" className="text-neutral-300 text-sm truncate hover:text-white transition-colors">{h.title}</a>
                  ) : (
                    <span className="text-neutral-300 text-sm truncate">{h.title}</span>
                  )}
                  <span className="text-neutral-600 text-xs shrink-0">{h.source}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Pivot Points */}
      {data.pivot_points && data.pivot_points.pivot && (
        <div className="bg-black rounded-xl p-6 border border-neutral-700 card-accent-top">
          <h2 className="text-xl font-semibold text-white mb-4">Pivot Points</h2>
          <div className="grid grid-cols-3 md:grid-cols-7 gap-3 text-center">
            <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-3">
              <p className="text-red-400 text-xs">S3</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.s3}</p>
            </div>
            <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-3">
              <p className="text-red-400 text-xs">S2</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.s2}</p>
            </div>
            <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-3">
              <p className="text-red-400 text-xs">S1</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.s1}</p>
            </div>
            <div className="bg-white/5 border border-white/20 rounded-lg p-3">
              <p className="text-white text-xs font-bold">PIVOT</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.pivot}</p>
            </div>
            <div className="bg-green-500/5 border border-green-500/20 rounded-lg p-3">
              <p className="text-green-400 text-xs">R1</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.r1}</p>
            </div>
            <div className="bg-green-500/5 border border-green-500/20 rounded-lg p-3">
              <p className="text-green-400 text-xs">R2</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.r2}</p>
            </div>
            <div className="bg-green-500/5 border border-green-500/20 rounded-lg p-3">
              <p className="text-green-400 text-xs">R3</p>
              <p className="text-white font-mono text-sm font-bold">${data.pivot_points.r3}</p>
            </div>
          </div>
          <p className="text-neutral-500 text-xs mt-3">
            Price is {data.pivot_points.position} the pivot point (${data.pivot_points.pivot}).
            20-day range: ${data.pivot_points.low_20d} — ${data.pivot_points.high_20d}
          </p>
        </div>
      )}

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
                {added ? 'Added to Watchlist' : '+ Add to Watchlist'}
              </button>
              <p className="text-neutral-600 text-xs mt-2 text-center">
                Track this stock on the Watchlist page
              </p>
            </>
          )}
        </div>

        <div className="lg:col-span-2">
          {/* Interactive Candlestick Chart */}
          <CandlestickChart ticker={data.info?.ticker} chartData={data.chart_data} />

          {/* Classic Price Chart */}
          <PriceChart chartData={data.chart_data} />
        </div>
      </div>

      {/* Bottom: Technical Indicators */}
      <TechnicalIndicators chartData={data.chart_data} />

      {/* Signal Reasoning */}
      {data.signal?.reasons && data.signal.reasons.length > 0 && (
        <div className={`bg-black rounded-xl p-6 border border-neutral-700 ${
          data.signal?.direction?.includes('Buy') ? 'card-accent-green' :
          data.signal?.direction?.includes('Sell') ? 'card-accent-red' : 'card-accent-top'
        }`}>
          <h2 className="text-xl font-semibold text-white mb-3">
            Why <span className={
              data.signal.direction?.includes('Buy') ? 'text-green-400' :
              data.signal.direction?.includes('Sell') ? 'text-red-400' : 'text-white'
            }>{data.signal.direction}</span>?
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {data.signal.reasons.map((r, i) => {
              const isBull = r.toLowerCase().includes('bullish') || r.toLowerCase().includes('oversold') || r.toLowerCase().includes('above') || r.toLowerCase().includes('positive')
              const isBear = r.toLowerCase().includes('bearish') || r.toLowerCase().includes('overbought') || r.toLowerCase().includes('below') || r.toLowerCase().includes('weak')
              return (
                <div key={i} className={`flex items-start gap-2.5 py-1.5 px-3 rounded-lg ${
                  isBull ? 'bg-green-500/[0.03]' : isBear ? 'bg-red-500/[0.03]' : ''
                }`}>
                  <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${
                    isBull ? 'bg-green-500' : isBear ? 'bg-red-500' : 'bg-neutral-500'
                  }`} />
                  <span className="text-neutral-300 text-sm">{r}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Trend Summary */}
      <div className="bg-black rounded-xl p-6 border border-neutral-700 card-accent-top">
        <h2 className="text-xl font-semibold text-white mb-4">Trend Analysis</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="stat-card rounded-lg p-3">
            <p className="text-neutral-500 text-[10px] uppercase tracking-wider font-medium">Direction</p>
            <p className={`text-lg font-bold mt-0.5 ${
              data.trend?.direction?.includes('bullish') ? 'text-green-500' :
              data.trend?.direction?.includes('bearish') ? 'text-red-500' : 'text-white'
            }`}>
              {(data.trend?.direction || 'neutral').replace('_', ' ')}
            </p>
          </div>
          <div className="stat-card rounded-lg p-3">
            <p className="text-neutral-500 text-[10px] uppercase tracking-wider font-medium">Strength</p>
            <p className="text-lg font-bold text-white mt-0.5 font-mono">{data.trend?.strength ?? 0}%</p>
          </div>
          <div className="stat-card rounded-lg p-3">
            <p className="text-neutral-500 text-[10px] uppercase tracking-wider font-medium">vs 20-day MA</p>
            <p className={`text-lg font-bold mt-0.5 font-mono ${(data.trend?.price_vs_sma20 || 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {(data.trend?.price_vs_sma20 || 0) >= 0 ? '+' : ''}{data.trend?.price_vs_sma20 ?? 0}%
            </p>
          </div>
          <div className="stat-card rounded-lg p-3">
            <p className="text-neutral-500 text-[10px] uppercase tracking-wider font-medium">vs 50-day MA</p>
            <p className={`text-lg font-bold mt-0.5 font-mono ${(data.trend?.price_vs_sma50 || 0) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {(data.trend?.price_vs_sma50 || 0) >= 0 ? '+' : ''}{data.trend?.price_vs_sma50 ?? 0}%
            </p>
          </div>
        </div>

        {/* Volume Analysis */}
        {data.volume_analysis && (
          <div className="mt-4 pt-4 border-t border-neutral-800">
            <h3 className="text-[10px] font-bold uppercase tracking-[0.15em] text-neutral-500 mb-3">Volume Analysis</h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="stat-card rounded-lg p-3">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Trend</p>
                <p className="text-white font-semibold mt-0.5">{data.volume_analysis.volume_trend}</p>
              </div>
              <div className="stat-card rounded-lg p-3">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Ratio</p>
                <p className="text-white font-mono font-semibold mt-0.5">{data.volume_analysis.volume_ratio}x avg</p>
              </div>
              <div className="stat-card rounded-lg p-3">
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Unusual Volume</p>
                <p className={`font-semibold mt-0.5 ${data.volume_analysis.unusual_volume ? 'text-red-400' : 'text-neutral-400'}`}>
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
