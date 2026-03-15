import { useState } from 'react'

export default function TickerSearch({ onAnalyze, loading }) {
  const [ticker, setTicker] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (ticker.trim()) {
      onAnalyze(ticker.trim().toUpperCase())
    }
  }

  const popularTickers = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN', 'GOOGL', 'META']

  return (
    <div className="text-center py-12">
      <h1 className="text-5xl font-bold text-white mb-4">
        Epic Fury Stock Analyzer
      </h1>
      <p className="text-neutral-400 text-lg mb-8 max-w-2xl mx-auto">
        Enter a stock ticker to get real-time technical analysis, price forecasts,
        and risk assessment powered by real Yahoo Finance data.
      </p>

      <form onSubmit={handleSubmit} className="flex justify-center gap-3 mb-6">
        <input
          type="text"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="Enter ticker (e.g. AAPL)"
          className="px-6 py-3 bg-black border border-neutral-600 rounded-lg text-white text-lg
                     focus:outline-none focus:border-white focus:ring-1 focus:ring-white w-72"
          disabled={loading}
        />
        <button
          type="submit"
          disabled={loading || !ticker.trim()}
          className="px-8 py-3 bg-white hover:bg-neutral-200 disabled:bg-neutral-800
                     disabled:text-neutral-500 text-black font-semibold rounded-lg text-lg
                     transition-colors"
        >
          {loading ? 'Analyzing...' : 'Analyze'}
        </button>
      </form>

      <div className="flex justify-center gap-2 flex-wrap">
        <span className="text-neutral-500 text-sm py-1">Popular:</span>
        {popularTickers.map((t) => (
          <button
            key={t}
            onClick={() => { setTicker(t); onAnalyze(t) }}
            disabled={loading}
            className="px-3 py-1 text-sm bg-black text-neutral-300 rounded-full
                       hover:bg-neutral-800 hover:text-white transition-colors border border-neutral-700"
          >
            {t}
          </button>
        ))}
      </div>

      <p className="text-neutral-600 text-xs mt-8">
        Not financial advice. For educational purposes only. All data from Yahoo Finance.
      </p>
    </div>
  )
}
