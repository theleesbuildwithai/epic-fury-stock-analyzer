import { useState, useEffect } from 'react'

export default function TickerBanner() {
  const [tickers, setTickers] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchBanner = async () => {
      try {
        const res = await fetch('/api/banner')
        const data = await res.json()
        if (data.tickers && data.tickers.length > 0) {
          setTickers(data.tickers)
        }
      } catch {
        // Silent fail — banner is non-critical
      } finally {
        setLoading(false)
      }
    }
    fetchBanner()

    // Refresh every 5 minutes
    const interval = setInterval(fetchBanner, 300000)
    return () => clearInterval(interval)
  }, [])

  if (loading || tickers.length === 0) return null

  // Double the tickers for seamless infinite scroll
  const scrollItems = [...tickers, ...tickers]

  return (
    <div className="bg-black border-b border-neutral-800 overflow-hidden">
      <div className="ticker-scroll flex items-center whitespace-nowrap py-2">
        {scrollItems.map((t, i) => (
          <div key={`${t.symbol}-${i}`} className="inline-flex items-center gap-2 px-4">
            <span className="text-neutral-400 text-xs font-medium">{t.name}</span>
            <span className="text-white text-xs font-mono font-bold">${t.price}</span>
            <span className={`text-xs font-mono font-bold ${
              t.change >= 0 ? 'text-green-500' : 'text-red-500'
            }`}>
              {t.change >= 0 ? '+' : ''}{t.change_pct}%
            </span>
            <span className="text-neutral-700 text-xs mx-2">|</span>
          </div>
        ))}
      </div>

      <style>{`
        .ticker-scroll {
          animation: scroll-left 60s linear infinite;
        }
        .ticker-scroll:hover {
          animation-play-state: paused;
        }
        @keyframes scroll-left {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
      `}</style>
    </div>
  )
}
