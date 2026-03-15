export default function RiskScore({ risk, signal }) {
  if (!risk || !signal) return null

  const riskColor = risk.score <= 3 ? 'text-green-400' :
    risk.score <= 6 ? 'text-yellow-400' :
    risk.score <= 8 ? 'text-orange-400' : 'text-red-400'

  const signalColor = signal.direction.includes('Buy') ? 'text-green-400' :
    signal.direction.includes('Sell') ? 'text-red-400' : 'text-yellow-400'

  const signalBg = signal.direction.includes('Buy') ? 'bg-green-900/30 border-green-700' :
    signal.direction.includes('Sell') ? 'bg-red-900/30 border-red-700' : 'bg-yellow-900/30 border-yellow-700'

  return (
    <div className="space-y-4">
      {/* Signal Card */}
      <div className={`rounded-xl p-6 border ${signalBg}`}>
        <div className="text-center">
          <p className="text-slate-400 text-sm mb-1">Analysis Signal</p>
          <p className={`text-4xl font-bold ${signalColor}`}>{signal.direction}</p>
          <p className="text-slate-400 mt-1">Confidence: {signal.confidence}%</p>
        </div>
        <div className="mt-4 space-y-2">
          {signal.reasons.map((reason, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-slate-500 mt-0.5">•</span>
              <p className="text-slate-300 text-sm">{reason}</p>
            </div>
          ))}
        </div>
        <p className="text-slate-600 text-xs mt-4 italic">{signal.disclaimer}</p>
      </div>

      {/* Risk Score Card */}
      <div className="bg-slate-800 rounded-xl p-6 border border-slate-700">
        <p className="text-slate-400 text-sm mb-1">Risk Score</p>
        <div className="flex items-baseline gap-2">
          <span className={`text-4xl font-bold ${riskColor}`}>{risk.score}</span>
          <span className="text-slate-400">/10</span>
          <span className={`text-lg ${riskColor}`}>({risk.label})</span>
        </div>

        {/* Risk bar */}
        <div className="mt-3 h-3 bg-slate-700 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all"
            style={{
              width: `${risk.score * 10}%`,
              background: risk.score <= 3 ? '#22c55e' : risk.score <= 6 ? '#eab308' : risk.score <= 8 ? '#f97316' : '#ef4444',
            }}
          />
        </div>

        <div className="mt-4 space-y-2">
          {risk.factors.map((factor, i) => (
            <div key={i} className="flex justify-between items-center">
              <span className="text-slate-400 text-sm">{factor.name}</span>
              <div className="flex items-center gap-2">
                <span className="text-slate-300 text-sm">{factor.detail}</span>
                <span className="text-xs px-2 py-0.5 bg-slate-700 rounded-full text-slate-400">
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
