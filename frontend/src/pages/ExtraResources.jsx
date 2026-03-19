import { useState, useEffect } from 'react'

export default function ExtraResources() {
  const [picks, setPicks] = useState(null)
  const [earnings, setEarnings] = useState(null)
  const [news, setNews] = useState(null)
  const [loadingPicks, setLoadingPicks] = useState(true)
  const [loadingEarnings, setLoadingEarnings] = useState(true)
  const [loadingNews, setLoadingNews] = useState(true)

  useEffect(() => {
    const fetchPicks = async () => {
      try {
        const res = await fetch('/api/daily-picks')
        const data = await res.json()
        setPicks(data)
      } catch {
        setPicks({ picks: [], error: 'Failed to load picks' })
      } finally {
        setLoadingPicks(false)
      }
    }

    const fetchEarnings = async () => {
      try {
        const res = await fetch('/api/earnings-calendar')
        const data = await res.json()
        setEarnings(data)
      } catch {
        setEarnings({ earnings: [], error: 'Failed to load earnings' })
      } finally {
        setLoadingEarnings(false)
      }
    }

    const fetchNews = async () => {
      try {
        const res = await fetch('/api/market-news')
        const data = await res.json()
        setNews(data)
      } catch {
        setNews(null)
      } finally {
        setLoadingNews(false)
      }
    }

    fetchPicks()
    fetchEarnings()
    fetchNews()
  }, [])

  const signalColor = (signal) => {
    if (signal === 'Strong Buy') return 'text-green-400 bg-green-500/10 border-green-500/30'
    if (signal === 'Buy') return 'text-green-500 bg-green-500/5 border-green-500/20'
    if (signal === 'Strong Sell') return 'text-red-400 bg-red-500/10 border-red-500/30'
    if (signal === 'Sell') return 'text-red-500 bg-red-500/5 border-red-500/20'
    return 'text-neutral-400 bg-neutral-500/5 border-neutral-500/20'
  }

  const actionColor = (action) => {
    if (action === 'Buy Now') return 'text-green-400'
    if (action === 'Buy') return 'text-green-500'
    if (action === 'Sell') return 'text-red-500'
    return 'text-neutral-400'
  }

  const sentimentColor = (score) => {
    if (score > 0.1) return 'text-green-500'
    if (score < -0.1) return 'text-red-500'
    return 'text-neutral-400'
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-white mb-2">Extra Resources</h1>
        <p className="text-neutral-400">
          Hedge fund grade analysis — symbols to buy, market news sentiment, and upcoming earnings.
        </p>
      </div>

      {/* Market Sentiment Summary */}
      {news?.market_sentiment && (
        <div className="bg-black border border-neutral-700 rounded-xl p-5 mb-6">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-lg font-bold text-white mb-1">Market Sentiment</h2>
              <p className="text-neutral-500 text-sm">Based on {news.market_sentiment.total_analyzed} headlines from Yahoo Finance, CNN, CNBC</p>
            </div>
            <div className="text-right">
              <span className={`text-2xl font-bold ${
                news.market_sentiment.label.includes('Bullish') ? 'text-green-500' :
                news.market_sentiment.label.includes('Bearish') ? 'text-red-500' : 'text-neutral-300'
              }`}>
                {news.market_sentiment.label}
              </span>
              <div className="flex items-center gap-3 mt-1">
                <span className="text-green-500 text-xs">{news.market_sentiment.bullish_pct}% Bullish</span>
                <span className="text-neutral-500 text-xs">|</span>
                <span className="text-red-500 text-xs">{news.market_sentiment.bearish_pct}% Bearish</span>
              </div>
            </div>
          </div>

          {/* Macro Events */}
          {news.macro_events && news.macro_events.length > 0 && (
            <div className="mt-4 pt-4 border-t border-neutral-800">
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-2">Current Events & Macro Factors</p>
              <div className="space-y-1.5">
                {news.macro_events.slice(0, 5).map((event, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <span className={`w-1.5 h-1.5 rounded-full ${
                      event.sentiment > 0 ? 'bg-green-500' : event.sentiment < 0 ? 'bg-red-500' : 'bg-neutral-500'
                    }`}></span>
                    <span className="text-neutral-300 text-sm truncate">{event.title}</span>
                    <span className="text-neutral-600 text-xs shrink-0">{event.source}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Symbols to Buy */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold text-white">Symbols to Buy</h2>
            <p className="text-neutral-500 text-sm mt-1">
              Ranked by EMA alignment, RSI, MACD, pivot points, and momentum
            </p>
          </div>
          {picks?.generated_at && (
            <span className="text-neutral-600 text-xs">
              Updated: {new Date(picks.generated_at).toLocaleTimeString()}
            </span>
          )}
        </div>

        {loadingPicks ? (
          <div className="text-center py-12">
            <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
            <p className="text-neutral-500 mt-3">Running hedge fund analysis... This takes a moment.</p>
          </div>
        ) : picks?.picks?.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-neutral-800">
                  <th className="text-left text-neutral-500 text-xs font-medium py-3 px-2">#</th>
                  <th className="text-left text-neutral-500 text-xs font-medium py-3 px-2">SYMBOL</th>
                  <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">PRICE</th>
                  <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">RSI</th>
                  <th className="text-center text-neutral-500 text-xs font-medium py-3 px-2">ACTION</th>
                  <th className="text-center text-neutral-500 text-xs font-medium py-3 px-2">SIGNAL</th>
                  <th className="text-center text-neutral-500 text-xs font-medium py-3 px-2">HOLD FOR</th>
                  <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">ENTRY</th>
                  <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">TARGET</th>
                  <th className="text-right text-neutral-500 text-xs font-medium py-3 px-2">30D UP%</th>
                </tr>
              </thead>
              <tbody>
                {picks.picks.map((pick) => (
                  <tr key={pick.symbol} className="border-b border-neutral-900 hover:bg-neutral-900/50 transition-colors">
                    <td className="py-3 px-2">
                      <span className={`text-sm font-bold ${
                        pick.rank <= 3 ? 'text-white' : 'text-neutral-500'
                      }`}>
                        {pick.rank}
                      </span>
                    </td>
                    <td className="py-3 px-2">
                      <span className="text-white font-mono font-bold text-sm">{pick.symbol}</span>
                    </td>
                    <td className="py-3 px-2 text-right">
                      <span className="text-white font-mono text-sm">${pick.price}</span>
                    </td>
                    <td className="py-3 px-2 text-right">
                      <span className={`font-mono text-sm ${
                        pick.rsi < 30 ? 'text-green-500' :
                        pick.rsi > 70 ? 'text-red-500' : 'text-neutral-300'
                      }`}>
                        {pick.rsi}
                      </span>
                    </td>
                    <td className="py-3 px-2 text-center">
                      <span className={`font-bold text-sm ${actionColor(pick.action)}`}>
                        {pick.action}
                      </span>
                    </td>
                    <td className="py-3 px-2 text-center">
                      <span className={`inline-block px-2 py-1 rounded text-xs font-bold border ${signalColor(pick.signal)}`}>
                        {pick.signal}
                      </span>
                    </td>
                    <td className="py-3 px-2 text-center">
                      <span className="text-neutral-300 text-sm font-medium">
                        {pick.hold_label}
                      </span>
                    </td>
                    <td className="py-3 px-2 text-right">
                      <span className="text-neutral-400 text-xs">{pick.entry}</span>
                    </td>
                    <td className="py-3 px-2 text-right">
                      <span className="text-green-500 font-mono text-sm">${pick.target}</span>
                    </td>
                    <td className="py-3 px-2 text-right">
                      <span className={`font-mono text-sm ${
                        pick.prob_up_30d >= 50 ? 'text-green-500' : 'text-red-500'
                      }`}>
                        {pick.prob_up_30d}%
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {/* Reasons for top 3 */}
            <div className="mt-6 pt-4 border-t border-neutral-800">
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-3">Why These Picks</p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {picks.picks.slice(0, 3).map((pick) => (
                  <div key={pick.symbol} className="bg-neutral-900 rounded-lg p-3">
                    <span className="text-white font-mono font-bold text-sm">{pick.symbol}</span>
                    <div className="mt-2 space-y-1">
                      {pick.reasons.map((r, i) => (
                        <p key={i} className="text-neutral-400 text-xs">• {r}</p>
                      ))}
                      {pick.stop_loss && (
                        <p className="text-red-400 text-xs mt-1">Stop loss: ${pick.stop_loss}</p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <p className="text-neutral-500 text-center py-8">No picks available right now. Check back later.</p>
        )}

        {picks?.total_analyzed > 0 && (
          <p className="text-neutral-600 text-xs mt-4">
            Screened {picks.total_analyzed} stocks using EMA crossovers, RSI, MACD, pivot points, and momentum analysis.
          </p>
        )}
      </div>

      {/* Latest Headlines */}
      {news?.headlines && news.headlines.length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
          <h2 className="text-xl font-bold text-white mb-4">Latest Market News</h2>
          <div className="space-y-2">
            {news.headlines.slice(0, 12).map((h, i) => (
              <div key={i} className="flex items-center justify-between py-2 border-b border-neutral-900">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${
                    h.sentiment > 0 ? 'bg-green-500' : h.sentiment < 0 ? 'bg-red-500' : 'bg-neutral-600'
                  }`}></span>
                  <span className="text-neutral-300 text-sm truncate">{h.title}</span>
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-3">
                  <span className={`text-xs font-mono ${sentimentColor(h.sentiment)}`}>
                    {h.sentiment > 0 ? '+' : ''}{h.sentiment}
                  </span>
                  <span className="text-neutral-600 text-xs w-20 text-right">{h.source}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Earnings Calendar */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold text-white">Upcoming Earnings</h2>
            <p className="text-neutral-500 text-sm mt-1">
              Major companies reporting earnings in the next 14 days
            </p>
          </div>
          {earnings?.week_start && (
            <span className="text-neutral-600 text-xs">
              {earnings.week_start} to {earnings.week_end}
            </span>
          )}
        </div>

        {loadingEarnings ? (
          <div className="text-center py-12">
            <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
            <p className="text-neutral-500 mt-3">Loading earnings calendar...</p>
          </div>
        ) : earnings?.earnings?.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {earnings.earnings.map((e) => (
              <div key={e.symbol} className="bg-neutral-900 border border-neutral-800 rounded-lg p-4 hover:border-neutral-600 transition-colors">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-white font-mono font-bold text-lg">{e.symbol}</span>
                  <span className="text-neutral-400 text-sm">{e.day_of_week}</span>
                </div>
                {e.name && <p className="text-neutral-400 text-xs mb-1">{e.name}</p>}
                <p className="text-neutral-500 text-sm mb-3">{e.date}</p>
                <div className="space-y-1">
                  {e.eps_estimate && (
                    <div className="flex justify-between">
                      <span className="text-neutral-500 text-xs">EPS Estimate</span>
                      <span className="text-white text-xs font-mono">${e.eps_estimate}</span>
                    </div>
                  )}
                  {!e.eps_estimate && (
                    <span className="text-neutral-600 text-xs">Estimates pending</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center py-8">
            <p className="text-neutral-400 mb-2">No major earnings scheduled this week.</p>
            <p className="text-neutral-600 text-sm">Check back during earnings season.</p>
          </div>
        )}
      </div>

      <p className="text-neutral-600 text-xs mt-6 text-center italic">
        Data from Yahoo Finance, CNN, CNBC. Analysis uses EMA crossovers, RSI, MACD, pivot points, and news sentiment.
        NOT financial advice. Always do your own research before investing.
      </p>
    </div>
  )
}
