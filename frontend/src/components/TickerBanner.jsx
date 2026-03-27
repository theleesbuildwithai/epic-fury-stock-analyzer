import { useState, useEffect } from 'react'

export default function TickerBanner() {
  const [tickers, setTickers] = useState([])
  const [marketOpen, setMarketOpen] = useState(true)
  const [asOf, setAsOf] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchBanner = async () => {
      try {
        const res = await fetch('/api/banner')
        const data = await res.json()
        if (data.tickers && data.tickers.length > 0) {
          setTickers(data.tickers)
        }
        if (data.market_open !== undefined) setMarketOpen(data.market_open)
        if (data.as_of) setAsOf(data.as_of)
      } catch {
        // Silent fail
      } finally {
        setLoading(false)
      }
    }

    fetchBanner()
    const interval = setInterval(() => {
      fetchBanner()
    }, marketOpen ? 60000 : 300000)
    return () => clearInterval(interval)
  }, [marketOpen])

  // Categorize items for styling
  const isIndex = (sym) => ['^GSPC', '^IXIC', '^DJI'].includes(sym)
  const isCommodity = (sym) => ['GC=F', 'CL=F', '^TNX'].includes(sym)

  // Always render the banner container so there's no layout shift
  // Show a loading skeleton until data arrives
  return (
    <div className="bg-black/80 backdrop-blur-sm border-b border-neutral-800/50 overflow-hidden">
      <div className="flex items-center">
        {/* Market status indicator */}
        <div className="flex items-center gap-1.5 px-4 border-r border-neutral-800/50 py-1.5 shrink-0">
          {loading ? (
            <>
              <div className="w-1.5 h-1.5 rounded-full bg-neutral-600 animate-pulse"></div>
              <span className="text-neutral-600 text-[10px] font-medium tracking-widest uppercase">Loading</span>
            </>
          ) : (
            <>
              <div className={`w-1.5 h-1.5 rounded-full ${marketOpen ? 'bg-green-500 pulse-dot' : 'bg-yellow-500'}`}></div>
              <span className="text-neutral-500 text-[10px] font-medium tracking-widest uppercase">
                {marketOpen ? 'Live' : 'Last Close'}
              </span>
            </>
          )}
        </div>

        <div className="overflow-hidden flex-1">
          {loading || tickers.length === 0 ? (
            /* Skeleton placeholder — same height as real banner so no jump */
            <div className="flex items-center gap-6 py-1.5 px-3">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="flex items-center gap-2">
                  <div className="h-3 w-16 bg-neutral-800 rounded animate-pulse"></div>
                  <div className="h-3 w-12 bg-neutral-800 rounded animate-pulse"></div>
                  <div className="h-3 w-10 bg-neutral-800 rounded animate-pulse"></div>
                </div>
              ))}
            </div>
          ) : (
            <div className="ticker-scroll flex items-center whitespace-nowrap py-1.5">
              {[...tickers, ...tickers, ...tickers].map((t, i) => (
                <div key={`${t.symbol}-${i}`} className="inline-flex items-center gap-1.5 px-3">
                  <span className={`text-[11px] font-medium tracking-wide ${
                    isIndex(t.symbol) ? 'text-yellow-500' :
                    isCommodity(t.symbol) ? 'text-amber-400' :
                    'text-neutral-500'
                  }`}>{t.name}</span>
                  <span className="text-white text-[11px] font-mono font-semibold">
                    {t.symbol === '^TNX' ? `${t.price.toFixed(2)}%` : `$${t.price.toLocaleString()}`}
                  </span>
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
          )}
        </div>
      </div>

      <style>{`
        .ticker-scroll {
          animation: scroll-left 12s linear infinite;
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
