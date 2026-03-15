export default function RiskScore({ risk, signal }) {
  if (!risk || !signal) return null

  const riskColor = risk.score <= 3 ? 'text-green-500' :
    risk.score <= 6 ? 'text-white' :
    'text-red-500'

  const signalColor = signal.direction.includes('Buy') ? 'text-green-500' :
    signal.direction.includes('Sell') ? 'text-red-500' : 'text-white'

  const signalBg = signal.direction.includes('Buy') ? 'bg-green-950/50 border-green-800' :
    signal.direction.includes('Sell') ? 'bg-red-950/50 border-red-800' : 'bg-neutral-900 border-neutral-700'

  return (
    <div className="space-y-4">
      {/* Signal Card */}
      <div className={`rounded-xl p-6 border ${signalBg}`}>
        <div className="text-center">
          <p className="text-neutral-400 text-sm mb-1">Analysis Signal</p>
          <p className={`text-4xl font-bold ${signalColor}`}>{signal.direction}</p>
          <p className="text-neutral-400 mt-1">Confidence: {signal.confidence}%</p>
        </div>
        <div className="mt-4 space-y-2">
          {signal.reasons.map((reason, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-neutral-500 mt-0.5">-</span>
              <p className="text-neutral-300 text-sm">{reason}</p>
            </div>
          ))}
        </div>
        <p className="text-neutral-600 text-xs mt-4 italic">{signal.disclaimer}</p>
      </div>

      {/* Risk Score Card */}
      <div className="bg-black rounded-xl p-6 border border-neutral-700">
        <p className="text-neutral-400 text-sm mb-1">Risk Score</p>
        <div className="flex items-baseline gap-2">
          <span className={`text-4xl font-bold ${riskColor}`}>{risk.score}</span>
          <span className="text-neutral-400">/10</span>
          <span className={`text-lg ${riskColor}`}>({risk.label})</span>
        </div>

        {/* Risk bar */}
        <div className="mt-3 h-3 bg-neutral-800 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all"
            style={{
              width: `${risk.score * 10}%`,
              background: risk.score <= 3 ? '#22c55e' : risk.score <= 6 ? '#ffffff' : '#ef4444',
            }}
          />
        </div>

        <div className="mt-4 space-y-2">
          {risk.factors.map((factor, i) => (
            <div key={i} className="flex justify-between items-center">
              <span className="text-neutral-400 text-sm">{factor.name}</span>
              <div className="flex items-center gap-2">
                <span className="text-neutral-300 text-sm">{factor.detail}</span>
                <span className="text-xs px-2 py-0.5 bg-neutral-800 rounded-full text-neutral-400">
                  {factor.score}/10
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
