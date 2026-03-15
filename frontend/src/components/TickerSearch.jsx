import { useState, useEffect, useRef } from 'react'

function getRecentSearches() {
  try {
    const data = localStorage.getItem('epic_fury_recent_searches')
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function addRecentSearch(ticker) {
  try {
    let recent = getRecentSearches()
    recent = recent.filter(t => t !== ticker)
    recent.unshift(ticker)
    if (recent.length > 10) recent = recent.slice(0, 10)
    localStorage.setItem('epic_fury_recent_searches', JSON.stringify(recent))
  } catch {
    // Silent fail
  }
}

export default function TickerSearch({ onAnalyze, loading }) {
  const [ticker, setTicker] = useState('')
  const [suggestions, setSuggestions] = useState([])
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [highlightIndex, setHighlightIndex] = useState(-1)
  const [recentSearches, setRecentSearches] = useState([])
  const wrapperRef = useRef(null)
  const debounceRef = useRef(null)

  useEffect(() => {
    setRecentSearches(getRecentSearches())
  }, [])

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
        setShowSuggestions(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

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
      setHighlightIndex(-1)
    } catch {
      setSuggestions([])
      setShowSuggestions(false)
    }
  }

  const handleInputChange = (e) => {
    const value = e.target.value.toUpperCase()
    setTicker(value)

    // Debounce search — wait 200ms after user stops typing
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      fetchSuggestions(value)
    }, 200)
  }

  const handleSelect = (selectedTicker) => {
    setTicker(selectedTicker)
    setShowSuggestions(false)
    setSuggestions([])
    addRecentSearch(selectedTicker)
    setRecentSearches(getRecentSearches())
    onAnalyze(selectedTicker)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (ticker.trim()) {
      const t = ticker.trim().toUpperCase()
      setShowSuggestions(false)
      addRecentSearch(t)
      setRecentSearches(getRecentSearches())
      onAnalyze(t)
    }
  }

  const handleKeyDown = (e) => {
    if (!showSuggestions || suggestions.length === 0) return

    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlightIndex(prev => Math.min(prev + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightIndex(prev => Math.max(prev - 1, 0))
    } else if (e.key === 'Enter' && highlightIndex >= 0) {
      e.preventDefault()
      handleSelect(suggestions[highlightIndex].ticker)
    } else if (e.key === 'Escape') {
      setShowSuggestions(false)
    }
  }

  const popularTickers = ['AAPL', 'TSLA', 'NVDA', 'MSFT', 'AMZN', 'GOOGL', 'META']

  return (
    <div className="text-center py-12">
      <h1 className="text-5xl font-bold text-white mb-4">
        Epic Fury Stock Analyzer
      </h1>
      <p className="text-neutral-400 text-lg mb-8 max-w-2xl mx-auto">
        Enter a stock ticker or company name to get real-time technical analysis, price forecasts,
        and risk assessment powered by real Yahoo Finance data.
      </p>

      <form onSubmit={handleSubmit} className="flex justify-center gap-3 mb-6">
        <div className="relative" ref={wrapperRef}>
          <input
            type="text"
            value={ticker}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onFocus={() => { if (suggestions.length > 0) setShowSuggestions(true) }}
            placeholder="Search ticker or company name..."
            className="px-6 py-3 bg-black border border-neutral-600 rounded-lg text-white text-lg
                       focus:outline-none focus:border-white focus:ring-1 focus:ring-white w-80"
            disabled={loading}
            autoComplete="off"
          />

          {/* Autocomplete dropdown */}
          {showSuggestions && suggestions.length > 0 && (
            <div className="absolute z-50 w-full mt-1 bg-neutral-900 border border-neutral-700
                            rounded-lg shadow-2xl overflow-hidden max-h-80 overflow-y-auto">
              {suggestions.map((item, index) => (
                <button
                  key={item.ticker}
                  type="button"
                  onClick={() => handleSelect(item.ticker)}
                  className={`w-full px-4 py-3 flex items-center gap-3 text-left transition-colors
                    ${index === highlightIndex
                      ? 'bg-neutral-700'
                      : 'hover:bg-neutral-800'
                    }
                    ${index !== suggestions.length - 1 ? 'border-b border-neutral-800' : ''}`}
                >
                  <span className="font-mono font-bold text-white text-sm bg-neutral-800
                                   px-2 py-1 rounded min-w-[60px] text-center">
                    {item.ticker}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="text-neutral-200 text-sm truncate">{item.name}</div>
                    <div className="text-neutral-500 text-xs">{item.sector}</div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

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

      {/* Recent Searches */}
      {recentSearches.length > 0 && (
        <div className="flex justify-center gap-2 flex-wrap mb-4">
          <span className="text-neutral-500 text-sm py-1">Recent:</span>
          {recentSearches.slice(0, 6).map((t) => (
            <button
              key={t}
              onClick={() => handleSelect(t)}
              disabled={loading}
              className="px-3 py-1 text-sm bg-neutral-900 text-white rounded-full
                         hover:bg-neutral-700 transition-colors border border-neutral-600"
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {/* Popular Tickers */}
      <div className="flex justify-center gap-2 flex-wrap">
        <span className="text-neutral-500 text-sm py-1">Popular:</span>
        {popularTickers.map((t) => (
          <button
            key={t}
            onClick={() => handleSelect(t)}
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
