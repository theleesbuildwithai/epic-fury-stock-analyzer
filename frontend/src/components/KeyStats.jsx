export default function KeyStats({ info, latest, supportResistance }) {
  if (!info) return null

  const formatNum = (n) => {
    if (!n) return 'N/A'
    if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`
    if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
    if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`
    return `$${n.toLocaleString()}`
  }

  const formatVol = (n) => {
    if (!n) return 'N/A'
    if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
    return n.toLocaleString()
  }

  const priceChange = info.current_price - info.previous_close
  const priceChangePct = ((priceChange / info.previous_close) * 100).toFixed(2)
  const isUp = priceChange >= 0

  const stats = [
    { label: 'Current Price', value: `$${info.current_price?.toFixed(2)}` },
    { label: 'Change', value: `${isUp ? '+' : ''}$${priceChange.toFixed(2)} (${isUp ? '+' : ''}${priceChangePct}%)`, color: isUp ? 'text-green-500' : 'text-red-500' },
    { label: 'Open', value: `$${info.open?.toFixed(2)}` },
    { label: 'Day Range', value: `$${info.day_low?.toFixed(2)} - $${info.day_high?.toFixed(2)}` },
    { label: '52-Week High', value: `$${info.fifty_two_week_high?.toFixed(2)}` },
    { label: '52-Week Low', value: `$${info.fifty_two_week_low?.toFixed(2)}` },
    { label: 'Volume', value: formatVol(info.volume) },
    { label: 'Avg Volume', value: formatVol(info.avg_volume) },
    { label: 'Market Cap', value: formatNum(info.market_cap) },
    { label: 'P/E Ratio', value: info.pe_ratio ? info.pe_ratio.toFixed(2) : 'N/A' },
    { label: 'Beta', value: info.beta ? info.beta.toFixed(2) : 'N/A' },
    { label: 'Sector', value: info.sector || 'N/A' },
  ]

  return (
    <div className="bg-black rounded-xl p-6 border border-neutral-700">
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <h2 className="text-2xl font-bold text-white">{info.name}</h2>
          <p className="text-neutral-400">{info.ticker} · {info.industry}</p>
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-white">${info.current_price?.toFixed(2)}</p>
          <p className={isUp ? 'text-green-500' : 'text-red-500'}>
            {isUp ? '+' : ''}{priceChange.toFixed(2)} ({isUp ? '+' : ''}{priceChangePct}%)
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {stats.map((stat) => (
          <div key={stat.label} className="bg-neutral-900 rounded-lg p-3">
            <p className="text-neutral-500 text-xs">{stat.label}</p>
            <p className={`text-sm font-medium ${stat.color || 'text-white'}`}>{stat.value}</p>
          </div>
        ))}
      </div>

      {supportResistance && (
        <div className="mt-4 grid grid-cols-2 gap-4">
          <div className="bg-neutral-900 rounded-lg p-3 border border-green-900/50">
            <p className="text-green-500 text-xs font-medium mb-1">Support Levels (Floor)</p>
            {supportResistance.support.length > 0
              ? supportResistance.support.map((s, i) => (
                  <span key={i} className="text-green-500 text-sm mr-2">${s.toFixed(2)}</span>
                ))
              : <span className="text-neutral-500 text-sm">No clear levels</span>
            }
          </div>
          <div className="bg-neutral-900 rounded-lg p-3 border border-red-900/50">
            <p className="text-red-500 text-xs font-medium mb-1">Resistance Levels (Ceiling)</p>
            {supportResistance.resistance.length > 0
              ? supportResistance.resistance.map((r, i) => (
                  <span key={i} className="text-red-500 text-sm mr-2">${r.toFixed(2)}</span>
                ))
              : <span className="text-neutral-500 text-sm">No clear levels</span>
            }
          </div>
        </div>
      )}
    </div>
  )
}
