import { useState, useEffect } from 'react'

export default function News() {
  const [news, setNews] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all') // all, bullish, bearish, macro

  useEffect(() => {
    const fetchNews = async () => {
      try {
        const res = await fetch('/api/market-news')
        const data = await res.json()
        setNews(data)
      } catch {
        setNews(null)
      } finally {
        setLoading(false)
      }
    }
    fetchNews()
    // Auto-refresh every 5 minutes
    const interval = setInterval(fetchNews, 300000)
    return () => clearInterval(interval)
  }, [])

  const filteredHeadlines = news?.headlines?.filter(h => {
    if (filter === 'bullish') return h.sentiment > 0
    if (filter === 'bearish') return h.sentiment < 0
    if (filter === 'macro') return h.is_macro
    return true
  }) || []

  const sentimentColor = (score) => {
    if (score > 0.1) return 'text-green-400'
    if (score > 0) return 'text-green-500/70'
    if (score < -0.1) return 'text-red-400'
    if (score < 0) return 'text-red-500/70'
    return 'text-neutral-500'
  }

  const sentimentLabel = (score) => {
    if (score > 0.3) return 'Very Bullish'
    if (score > 0.1) return 'Bullish'
    if (score > 0) return 'Slightly Bullish'
    if (score < -0.3) return 'Very Bearish'
    if (score < -0.1) return 'Bearish'
    if (score < 0) return 'Slightly Bearish'
    return 'Neutral'
  }

  const sourceIcon = (source) => {
    if (source === 'Yahoo Finance') return 'YF'
    if (source === 'CNN') return 'CNN'
    if (source === 'CNBC') return 'CNBC'
    return source
  }

  const sourceColor = (source) => {
    if (source === 'Yahoo Finance') return 'bg-purple-500/20 text-purple-400 border-purple-500/30'
    if (source === 'CNN') return 'bg-red-500/20 text-red-400 border-red-500/30'
    if (source === 'CNBC') return 'bg-blue-500/20 text-blue-400 border-blue-500/30'
    return 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30'
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-white mb-2">Market News</h1>
          <p className="text-neutral-400">
            Live headlines from Yahoo Finance, CNN, and CNBC with sentiment analysis.
          </p>
        </div>
        {news?.fetched_at && (
          <span className="text-neutral-600 text-xs mt-2 sm:mt-0">
            Last updated: {new Date(news.fetched_at).toLocaleTimeString()}
          </span>
        )}
      </div>

      {/* Market Sentiment Overview */}
      {news?.market_sentiment && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
            <div>
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-1">Overall Sentiment</p>
              <p className={`text-2xl font-bold ${
                news.market_sentiment.label.includes('Bullish') ? 'text-green-500' :
                news.market_sentiment.label.includes('Bearish') ? 'text-red-500' : 'text-neutral-300'
              }`}>
                {news.market_sentiment.label}
              </p>
            </div>
            <div>
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-1">Bullish Headlines</p>
              <p className="text-green-500 text-2xl font-bold">{news.market_sentiment.bullish_pct}%</p>
            </div>
            <div>
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-1">Bearish Headlines</p>
              <p className="text-red-500 text-2xl font-bold">{news.market_sentiment.bearish_pct}%</p>
            </div>
            <div>
              <p className="text-neutral-500 text-xs uppercase tracking-wider mb-1">Total Analyzed</p>
              <p className="text-white text-2xl font-bold">{news.market_sentiment.total_analyzed}</p>
            </div>
          </div>
        </div>
      )}

      {/* Filter tabs */}
      <div className="flex gap-2 mb-6">
        {[
          { key: 'all', label: 'All News' },
          { key: 'bullish', label: 'Bullish' },
          { key: 'bearish', label: 'Bearish' },
          { key: 'macro', label: 'Macro Events' },
        ].map(tab => (
          <button
            key={tab.key}
            onClick={() => setFilter(tab.key)}
            className={`px-4 py-2 text-sm font-medium rounded-lg border transition-colors ${
              filter === tab.key
                ? 'bg-white text-black border-white'
                : 'bg-neutral-900 text-neutral-400 border-neutral-700 hover:border-neutral-500 hover:text-white'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Headlines */}
      {loading ? (
        <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
          <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
          <p className="text-neutral-500 mt-3">Fetching latest news...</p>
        </div>
      ) : filteredHeadlines.length > 0 ? (
        <div className="space-y-3">
          {filteredHeadlines.map((h, i) => (
            <a
              key={i}
              href={h.link || '#'}
              target="_blank"
              rel="noopener noreferrer"
              className="block bg-black border border-neutral-700 rounded-xl p-5 hover:border-neutral-500 transition-all group"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <p className="text-white text-base font-medium group-hover:text-neutral-100 mb-2 leading-snug">
                    {h.title}
                  </p>
                  <div className="flex items-center gap-3">
                    <span className={`inline-block px-2 py-0.5 rounded text-xs font-bold border ${sourceColor(h.source)}`}>
                      {sourceIcon(h.source)}
                    </span>
                    {h.is_macro && (
                      <span className="inline-block px-2 py-0.5 rounded text-xs font-bold bg-yellow-500/10 text-yellow-400 border border-yellow-500/30">
                        MACRO
                      </span>
                    )}
                    {h.pub_date && (
                      <span className="text-neutral-600 text-xs">{h.pub_date}</span>
                    )}
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <p className={`text-sm font-bold ${sentimentColor(h.sentiment)}`}>
                    {sentimentLabel(h.sentiment)}
                  </p>
                  <p className={`text-xs font-mono ${sentimentColor(h.sentiment)}`}>
                    {h.sentiment > 0 ? '+' : ''}{h.sentiment}
                  </p>
                </div>
              </div>
            </a>
          ))}
        </div>
      ) : (
        <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
          <p className="text-neutral-400">No headlines match this filter.</p>
        </div>
      )}

      <p className="text-neutral-600 text-xs mt-6 text-center italic">
        Headlines sourced from Yahoo Finance, CNN, and CNBC RSS feeds. Sentiment scored using keyword analysis.
        Click any headline to read the full article. Not financial advice.
      </p>
    </div>
  )
}
