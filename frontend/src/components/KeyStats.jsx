export default function KeyStats({ info, latest, supportResistance }) {
  if (!info) return null

  const formatNum = (n) => {
    if (n == null || n === '') return 'N/A'
    if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`
    if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`
    if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`
    return `$${n.toLocaleString()}`
  }

  const formatVol = (n) => {
    if (n == null || n === '') return 'N/A'
    if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
    return n.toLocaleString()
  }

  const priceChange = (info.current_price || 0) - (info.previous_close || 0)
  const priceChangePct = info.previous_close ? ((priceChange / info.previous_close) * 100).toFixed(2) : '0.00'
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
    <div className={`bg-black rounded-xl p-6 border border-neutral-700 ${isUp ? 'card-accent-green' : 'card-accent-red'}`}>
      <div className="flex items-baseline justify-between mb-5">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <h2 className="text-2xl font-bold text-white">{info.name}</h2>
            <span className="text-[10px] font-mono font-bold tracking-wider px-2 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700">
              {info.ticker}
            </span>
          </div>
          <p className="text-neutral-500 text-sm">{info.industry} · {info.sector || ''}</p>
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-white font-mono">${info.current_price?.toFixed(2)}</p>
          <p className={`font-mono font-semibold ${isUp ? 'text-green-500' : 'text-red-500'}`}>
            {isUp ? '+' : ''}{priceChange.toFixed(2)} ({isUp ? '+' : ''}{priceChangePct}%)
            <span className={`inline-block ml-1 text-xs ${isUp ? 'text-green-500/60' : 'text-red-500/60'}`}>
              {isUp ? '\u25B2' : '\u25BC'}
            </span>
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {stats.map((stat) => (
          <div key={stat.label} className="stat-card rounded-lg p-3">
            <p className="text-neutral-500 text-[11px] uppercase tracking-wider font-medium">{stat.label}</p>
            <p className={`text-sm font-semibold font-mono mt-0.5 ${stat.color || 'text-white'}`}>{stat.value}</p>
          </div>
        ))}
      </div>

      {supportResistance && (
        <div className="mt-4 grid grid-cols-2 gap-4">
          <div className="bg-green-500/[0.03] rounded-lg p-4 border border-green-500/20 card-accent-green">
            <p className="text-green-400 text-[11px] font-bold uppercase tracking-wider mb-2">Support Levels</p>
            <div className="flex flex-wrap gap-2">
              {supportResistance.support.length > 0
                ? supportResistance.support.map((s, i) => (
                    <span key={i} className="text-green-400 text-sm font-mono font-bold bg-green-500/10 px-2 py-0.5 rounded">${s.toFixed(2)}</span>
                  ))
                : <span className="text-neutral-500 text-sm">No clear levels</span>
              }
            </div>
          </div>
          <div className="bg-red-500/[0.03] rounded-lg p-4 border border-red-500/20 card-accent-red">
            <p className="text-red-400 text-[11px] font-bold uppercase tracking-wider mb-2">Resistance Levels</p>
            <div className="flex flex-wrap gap-2">
              {supportResistance.resistance.length > 0
                ? supportResistance.resistance.map((r, i) => (
                    <span key={i} className="text-red-400 text-sm font-mono font-bold bg-red-500/10 px-2 py-0.5 rounded">${r.toFixed(2)}</span>
                  ))
                : <span className="text-neutral-500 text-sm">No clear levels</span>
              }
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
