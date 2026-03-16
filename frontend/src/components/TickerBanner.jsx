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
        // Silent fail
      } finally {
        setLoading(false)
      }
    }

    const isMarketHours = () => {
      const now = new Date()
      const hours = now.getHours()
      const mins = now.getMinutes()
      const t = hours * 60 + mins
      // 6:30 AM = 390, 5:30 PM = 1050
      return t >= 390 && t <= 1050
    }

    fetchBanner()
    // During market hours refresh every 60s, otherwise every 5 min
    const interval = setInterval(() => {
      fetchBanner()
    }, isMarketHours() ? 60000 : 300000)
    return () => clearInterval(interval)
  }, [])

  if (loading || tickers.length === 0) return null

  // Triple the items for seamless loop with 100+ stocks
  const scrollItems = [...tickers, ...tickers, ...tickers]

  return (
    <div className="bg-black/80 backdrop-blur-sm border-b border-neutral-800/50 overflow-hidden">
      <div className="flex items-center">
        {/* Live indicator */}
        <div className="flex items-center gap-1.5 px-4 border-r border-neutral-800/50 py-1.5 shrink-0">
          <div className="w-1.5 h-1.5 rounded-full bg-green-500 pulse-dot"></div>
          <span className="text-neutral-500 text-[10px] font-medium tracking-widest uppercase">Live</span>
        </div>

        <div className="overflow-hidden flex-1">
          <div className="ticker-scroll flex items-center whitespace-nowrap py-1.5">
            {scrollItems.map((t, i) => (
              <div key={`${t.symbol}-${i}`} className="inline-flex items-center gap-1.5 px-3">
                <span className="text-neutral-500 text-[11px] font-medium tracking-wide">{t.name}</span>
                <span className="text-white text-[11px] font-mono font-semibold">${t.price.toLocaleString()}</span>
                <span className={`text-[11px] font-mono font-bold px-1.5 py-0.5 rounded ${
                  t.change >= 0
                    ? 'text-green-400 bg-green-500/10'
                    : 'text-red-400 bg-red-500/10'
                }`}>
                  {t.change >= 0 ? '+' : ''}{t.change_pct}%
                </span>
                <span className="text-neutral-800 text-[10px] mx-1.5">|</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <style>{`
        .ticker-scroll {
          animation: scroll-left 15s linear infinite;
        }
        .ticker-scroll:hover {
          animation-play-state: paused;
        }
        @keyframes scroll-left {
          0% { transform: translateX(0); }
          100% { transform: translateX(-33.333%); }
        }
      `}</style>
    </div>
  )
}
