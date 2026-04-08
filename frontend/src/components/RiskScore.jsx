export default function RiskScore({ risk, signal }) {
  if (!risk || !signal) return null

  const riskColor = risk.score <= 3 ? 'text-green-500' :
    risk.score <= 6 ? 'text-white' :
    'text-red-500'

  const dir = signal.direction || ''
  const signalColor = dir.includes('Buy') ? 'text-gradient-green' :
    dir.includes('Sell') ? 'text-gradient-red' : 'text-white'

  const signalBg = dir.includes('Buy') ? 'bg-green-950/50 border-green-800' :
    dir.includes('Sell') ? 'bg-red-950/50 border-red-800' : 'bg-neutral-900 border-neutral-700'

  const signalGlow = dir.includes('Buy') ? 'pulse-glow-green' :
    dir.includes('Sell') ? 'pulse-glow-red' : ''

  return (
    <div className="space-y-4">
      {/* Signal Card */}
      <div className={`rounded-xl p-6 border ${signalBg} ${signalGlow}`}>
        <div className="text-center">
          <p className="text-[10px] font-bold tracking-[0.2em] uppercase text-neutral-400 mb-2">Analysis Signal</p>
          <p className={`text-5xl font-black ${signalColor}`}>{dir || 'Hold'}</p>
          <div className="mt-3 flex items-center justify-center gap-2">
            <span className="text-neutral-500 text-sm">Confidence:</span>
            <span className="text-white font-mono font-bold text-lg">{signal.confidence}%</span>
          </div>
          {/* Confidence bar */}
          <div className="mt-2 mx-auto max-w-[200px] h-1.5 bg-neutral-800 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${signal.confidence}%`,
                background: dir.includes('Buy') ? 'linear-gradient(90deg, #16a34a, #4ade80)' :
                  dir.includes('Sell') ? 'linear-gradient(90deg, #dc2626, #f87171)' : '#fff'
              }}
            />
          </div>
        </div>
        <div className="mt-5 space-y-2">
          {(signal.reasons || []).map((reason, i) => (
            <div key={i} className="flex items-start gap-2.5">
              <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${
                dir.includes('Buy') ? 'bg-green-500/60' :
                dir.includes('Sell') ? 'bg-red-500/60' : 'bg-neutral-500'
              }`} />
              <p className="text-neutral-300 text-sm">{reason}</p>
            </div>
          ))}
        </div>
        <p className="text-neutral-600 text-xs mt-4 italic">{signal.disclaimer}</p>
      </div>

      {/* Risk Score Card */}
      <div className="bg-black rounded-xl p-6 border border-neutral-700 card-accent-top">
        <p className="text-[10px] font-bold tracking-[0.2em] uppercase text-neutral-500 mb-2">Risk Score</p>
        <div className="flex items-baseline gap-2">
          <span className={`text-4xl font-black font-mono ${riskColor}`}>{risk.score}</span>
          <span className="text-neutral-500 font-mono">/10</span>
          <span className={`text-sm font-semibold ml-1 px-2 py-0.5 rounded ${
            risk.score <= 3 ? 'bg-green-500/10 text-green-400' :
            risk.score <= 6 ? 'bg-neutral-800 text-neutral-300' :
            'bg-red-500/10 text-red-400'
          }`}>
            {risk.label}
          </span>
        </div>

        {/* Risk bar with gradient */}
        <div className="mt-3 h-2.5 bg-neutral-800 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${risk.score * 10}%`,
              background: risk.score <= 3
                ? 'linear-gradient(90deg, #16a34a, #22c55e, #4ade80)'
                : risk.score <= 6
                ? 'linear-gradient(90deg, #d4d4d8, #ffffff)'
                : 'linear-gradient(90deg, #dc2626, #ef4444, #f87171)',
            }}
          />
        </div>
        <div className="flex justify-between mt-1">
          <span className="text-[10px] text-green-500/50 font-mono">Low</span>
          <span className="text-[10px] text-red-500/50 font-mono">High</span>
        </div>

        <div className="mt-4 space-y-1.5">
          {risk.factors.map((factor, i) => (
            <div key={i} className="flex justify-between items-center py-1.5 px-2 rounded-lg hover:bg-neutral-900/50">
              <span className="text-neutral-400 text-sm">{factor.name}</span>
              <div className="flex items-center gap-2">
                <span className="text-neutral-300 text-sm font-mono">{factor.detail}</span>
                <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                  factor.score <= 3 ? 'bg-green-500/10 text-green-400' :
                  factor.score <= 6 ? 'bg-neutral-800 text-neutral-300' :
                  'bg-red-500/10 text-red-400'
                }`}>
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
