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
    <div className="text-center py-16 px-4">
      {/* Hero section */}
      <div className="mb-12">
        <h1 className="text-6xl sm:text-7xl font-black text-white mb-3 tracking-tight">
          Epic Fury
        </h1>
        <p className="text-xl text-neutral-500 font-light tracking-wide">
          Stock Analyzer
        </p>
      </div>

      <p className="text-neutral-400 text-base mb-10 max-w-xl mx-auto leading-relaxed">
        Real-time technical analysis, probability forecasts, and risk assessment.
        Search by ticker or company name.
      </p>

      {/* Search bar */}
      <form onSubmit={handleSubmit} className="flex justify-center gap-2 mb-8">
        <div className="relative" ref={wrapperRef}>
          <div className="absolute inset-y-0 left-4 flex items-center pointer-events-none">
            <svg className="w-5 h-5 text-neutral-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>
          <input
            type="text"
            value={ticker}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onFocus={() => { if (suggestions.length > 0) setShowSuggestions(true) }}
            placeholder="Search ticker or company name..."
            className="pl-12 pr-6 py-3.5 bg-neutral-900 border border-neutral-700 rounded-xl text-white text-base
                       focus:outline-none focus:border-neutral-500 focus:ring-1 focus:ring-neutral-500 w-96
                       placeholder-neutral-500 shadow-lg shadow-black/20"
            disabled={loading}
            autoComplete="off"
          />

          {/* Autocomplete dropdown */}
          {showSuggestions && suggestions.length > 0 && (
            <div className="absolute z-50 w-full mt-2 bg-neutral-900 border border-neutral-700
                            rounded-xl shadow-2xl shadow-black/50 overflow-hidden max-h-80 overflow-y-auto">
              {suggestions.map((item, index) => (
                <button
                  key={item.ticker}
                  type="button"
                  onClick={() => handleSelect(item.ticker)}
                  className={`w-full px-4 py-3 flex items-center gap-3 text-left transition-all
                    ${index === highlightIndex
                      ? 'bg-neutral-800'
                      : 'hover:bg-neutral-800/50'
                    }
                    ${index !== suggestions.length - 1 ? 'border-b border-neutral-800/50' : ''}`}
                >
                  <span className="font-mono font-bold text-white text-xs bg-neutral-800 border border-neutral-700
                                   px-2.5 py-1 rounded-md min-w-[56px] text-center">
                    {item.ticker}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="text-neutral-200 text-sm truncate">{item.name}</div>
                    <div className="text-neutral-500 text-xs">{item.sector}</div>
                  </div>
                  <svg className="w-4 h-4 text-neutral-600 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                </button>
              ))}
            </div>
          )}
        </div>

        <button
          type="submit"
          disabled={loading || !ticker.trim()}
          className="px-8 py-3.5 bg-white hover:bg-neutral-100 disabled:bg-neutral-800
                     disabled:text-neutral-600 text-black font-semibold rounded-xl text-base
                     transition-all shadow-lg shadow-white/5 hover:shadow-white/10
                     disabled:shadow-none"
        >
          {loading ? (
            <span className="flex items-center gap-2">
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
              Analyzing
            </span>
          ) : 'Analyze'}
        </button>
      </form>

      {/* Recent Searches */}
      {recentSearches.length > 0 && (
        <div className="flex justify-center items-center gap-2 flex-wrap mb-4">
          <span className="text-neutral-600 text-xs font-medium tracking-wider uppercase">Recent</span>
          <span className="text-neutral-700">|</span>
          {recentSearches.slice(0, 6).map((t) => (
            <button
              key={t}
              onClick={() => handleSelect(t)}
              disabled={loading}
              className="px-3 py-1 text-xs font-mono font-semibold bg-neutral-900 text-white rounded-md
                         hover:bg-neutral-800 transition-all border border-neutral-700 hover:border-neutral-500"
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {/* Popular Tickers */}
      <div className="flex justify-center items-center gap-2 flex-wrap">
        <span className="text-neutral-600 text-xs font-medium tracking-wider uppercase">Popular</span>
        <span className="text-neutral-700">|</span>
        {popularTickers.map((t) => (
          <button
            key={t}
            onClick={() => handleSelect(t)}
            disabled={loading}
            className="px-3 py-1 text-xs font-mono bg-transparent text-neutral-400 rounded-md
                       hover:bg-neutral-900 hover:text-white transition-all border border-neutral-800
                       hover:border-neutral-600"
          >
            {t}
          </button>
        ))}
      </div>

      <p className="text-neutral-700 text-[11px] mt-10 tracking-wide">
        Not financial advice. For educational purposes only. Data from Yahoo Finance.
      </p>
    </div>
  )
}
