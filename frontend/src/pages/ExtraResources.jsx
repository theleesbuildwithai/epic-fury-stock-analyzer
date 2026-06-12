import { useState, useEffect } from 'react'
import CompoundInterestCalc from '../components/CompoundInterestCalc'
import SectorHeatmap from '../components/SectorHeatmap'

export default function ExtraResources() {
  const [earnings, setEarnings] = useState(null)
  const [news, setNews] = useState(null)
  const [loadingEarnings, setLoadingEarnings] = useState(true)
  const [loadingNews, setLoadingNews] = useState(true)

  useEffect(() => {
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

    fetchEarnings()
    fetchNews()
  }, [])

  const sentimentColor = (score) => {
    if (score > 0.1) return 'text-green-500'
    if (score < -0.1) return 'text-red-500'
    return 'text-neutral-400'
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-8">
        <h1 className="text-4xl font-bold mb-2"><span className="text-gradient">Extra</span> <span className="text-white">Resources</span></h1>
        <p className="text-neutral-400">
          Market news sentiment, sector heatmap, earnings calendar, and tools.
        </p>
      </div>

      {/* Sector Heatmap */}
      <SectorHeatmap />

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
                    {event.link ? (
                      <a href={event.link} target="_blank" rel="noopener noreferrer" className="text-neutral-300 text-sm truncate hover:text-white transition-colors">{event.title}</a>
                    ) : (
                      <span className="text-neutral-300 text-sm truncate">{event.title}</span>
                    )}
                    <span className="text-neutral-600 text-xs shrink-0">{event.source}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Latest Headlines */}
      {news?.headlines && news.headlines.length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
          <h2 className="text-xl font-bold text-white mb-4">Latest Market News</h2>
          <div className="space-y-2">
            {news.headlines.slice(0, 12).map((h, i) => (
              <a key={i} href={h.link || '#'} target="_blank" rel="noopener noreferrer"
                 className="flex items-center justify-between py-2 border-b border-neutral-900 hover:bg-neutral-900/50 transition-colors px-1 rounded">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${
                    h.sentiment > 0 ? 'bg-green-500' : h.sentiment < 0 ? 'bg-red-500' : 'bg-neutral-600'
                  }`}></span>
                  <span className="text-neutral-300 text-sm truncate hover:text-white">{h.title}</span>
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-3">
                  <span className={`text-xs font-mono ${sentimentColor(h.sentiment)}`}>
                    {h.sentiment > 0 ? '+' : ''}{h.sentiment}
                  </span>
                  <span className="text-neutral-600 text-xs w-20 text-right">{h.source}</span>
                </div>
              </a>
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

      {/* Compound Interest Calculator */}
      <div className="mb-8">
        <CompoundInterestCalc />
      </div>

      <p className="text-neutral-600 text-xs mt-6 text-center italic">
        Data from Yahoo Finance, CNN, CNBC. Analysis uses EMA crossovers, RSI, MACD, pivot points, and news sentiment.
        NOT financial advice. Always do your own research before investing.
      </p>
    </div>
  )
}
