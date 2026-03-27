import { useState, useEffect } from 'react'

export default function SectorHeatmap() {
  const [sectors, setSectors] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchSectors = async () => {
      try {
        const res = await fetch('/api/sector-heatmap')
        const data = await res.json()
        setSectors(data)
      } catch {
        setSectors({ sectors: [] })
      } finally {
        setLoading(false)
      }
    }
    fetchSectors()
  }, [])

  const getHeatColor = (pct) => {
    if (pct >= 2) return 'bg-green-500 text-white'
    if (pct >= 1) return 'bg-green-600 text-white'
    if (pct >= 0.5) return 'bg-green-700 text-white'
    if (pct >= 0) return 'bg-green-900 text-green-300'
    if (pct >= -0.5) return 'bg-red-900 text-red-300'
    if (pct >= -1) return 'bg-red-700 text-white'
    if (pct >= -2) return 'bg-red-600 text-white'
    return 'bg-red-500 text-white'
  }

  if (loading) {
    return (
      <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
        <h2 className="text-xl font-bold text-white mb-4">Sector Heatmap</h2>
        <div className="text-center py-12">
          <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
          <p className="text-neutral-500 mt-3">Loading sector data...</p>
        </div>
      </div>
    )
  }

  if (!sectors?.sectors?.length) {
    return (
      <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
        <h2 className="text-xl font-bold text-white mb-4">Sector Heatmap</h2>
        <p className="text-neutral-500 text-center py-8">Sector data unavailable. Check back later.</p>
      </div>
    )
  }

  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-6 mb-8">
      <div className="mb-4">
        <h2 className="text-xl font-bold text-white">Sector Heatmap</h2>
        <p className="text-neutral-500 text-sm mt-1">S&P 500 sectors — today's performance via SPDR ETFs</p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        {sectors.sectors.map((s) => (
          <div
            key={s.symbol}
            className={`rounded-xl p-4 transition-transform hover:scale-105 cursor-default ${getHeatColor(s.change_pct)}`}
          >
            <div className="text-sm font-bold opacity-90">{s.name}</div>
            <div className="text-2xl font-bold font-mono mt-1">
              {s.change_pct >= 0 ? '+' : ''}{s.change_pct}%
            </div>
            <div className="text-xs opacity-70 font-mono mt-1">{s.symbol} &middot; ${s.price}</div>
          </div>
        ))}
      </div>

      {sectors.generated_at && (
        <p className="text-neutral-600 text-xs mt-4 text-right">
          Updated: {new Date(sectors.generated_at).toLocaleTimeString()}
        </p>
      )}
    </div>
  )
}
