// SYSTEM DECISION — the autonomous verdict card.
// Renders the backend decision engine's combined read (technical + forecast +
// news + geopolitics + fundamentals) as one decisive instruction. Fail-safe:
// renders nothing when the decision object is absent, and renders an explicit
// UNRATED state when the engine withheld a verdict (fail-closed on bad data).
//
// Palette discipline: green = bullish actions, red = bearish actions,
// everything else white/neutral.

const ACTION_META = {
  BUY: {
    text: 'text-emerald-300',
    bg: 'bg-emerald-950/25',
    border: 'border-emerald-700/40',
    bar: 'bg-emerald-500/80',
    chip: 'bg-emerald-500/15 border-emerald-500/40 text-emerald-300',
  },
  ACCUMULATE: {
    text: 'text-emerald-400',
    bg: 'bg-emerald-950/15',
    border: 'border-emerald-800/40',
    bar: 'bg-emerald-500/60',
    chip: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
  },
  HOLD: {
    text: 'text-neutral-200',
    bg: 'bg-neutral-900/40',
    border: 'border-neutral-700',
    bar: 'bg-neutral-500/70',
    chip: 'bg-white/10 border-neutral-600 text-neutral-200',
  },
  REDUCE: {
    text: 'text-rose-400',
    bg: 'bg-rose-950/15',
    border: 'border-rose-800/40',
    bar: 'bg-rose-500/60',
    chip: 'bg-rose-500/10 border-rose-500/30 text-rose-400',
  },
  AVOID: {
    text: 'text-rose-300',
    bg: 'bg-rose-950/25',
    border: 'border-rose-700/40',
    bar: 'bg-rose-500/80',
    chip: 'bg-rose-500/15 border-rose-500/40 text-rose-300',
  },
  UNRATED: {
    text: 'text-neutral-400',
    bg: 'bg-neutral-900/40',
    border: 'border-neutral-700',
    bar: 'bg-neutral-600',
    chip: 'bg-white/5 border-neutral-700 text-neutral-400',
  },
}

const COMPONENT_LABELS = {
  technical: 'Technical',
  forecast: 'Forecast',
  news: 'News',
  macro_geo: 'Geo / Macro',
  fundamental: 'Fundamental',
}

function ComponentBar({ label, value }) {
  const v = Number(value)
  if (!isFinite(v)) return null
  const bull = v >= 53
  const bear = v <= 47
  const barColor = bull ? 'bg-emerald-500/70' : bear ? 'bg-rose-500/70' : 'bg-neutral-500/70'
  const numColor = bull ? 'text-emerald-400' : bear ? 'text-rose-400' : 'text-neutral-400'
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] text-neutral-500 w-20 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${barColor}`} style={{ width: `${Math.min(100, Math.max(0, v))}%` }} />
      </div>
      <span className={`text-[10px] font-mono w-8 text-right ${numColor}`}>{Math.round(v)}</span>
    </div>
  )
}

export default function SystemDecision({ decision, geoRisk }) {
  if (!decision || !decision.action) return null
  const meta = ACTION_META[decision.action] || ACTION_META.UNRATED
  const conviction = Number(decision.conviction) || 0
  const components = decision.components || {}
  const geoLevel = (geoRisk?.risk_level || '').toUpperCase()

  return (
    <div className={`${meta.bg} border ${meta.border} rounded-xl p-6`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-[0.2em] text-neutral-500 mb-1">
            System Decision · Autonomous
          </p>
          <div className="flex items-center gap-4">
            <span className={`text-4xl font-black ${meta.text}`}>{decision.action}</span>
            <div className="h-8 w-px bg-neutral-800" />
            <div>
              <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Conviction</p>
              <div className="flex items-center gap-2">
                <span className="text-white font-mono font-bold text-lg">{conviction}</span>
                <div className="w-24 h-2 bg-neutral-800 rounded-full overflow-hidden">
                  <div className={`h-full rounded-full ${meta.bar}`} style={{ width: `${conviction}%` }} />
                </div>
              </div>
            </div>
            {decision.score != null && (
              <div>
                <p className="text-neutral-500 text-[10px] uppercase tracking-wider">Composite</p>
                <p className="text-white font-mono font-bold text-lg">{decision.score}</p>
              </div>
            )}
          </div>
          <p className="text-neutral-300 text-sm mt-2 max-w-xl">{decision.instruction}</p>
        </div>

        <div className="min-w-[220px] max-w-[320px] flex-1 space-y-1.5">
          {Object.entries(COMPONENT_LABELS).map(([key, label]) => (
            components[key] != null
              ? <ComponentBar key={key} label={label} value={components[key]} />
              : null
          ))}
          {decision.earnings_blackout && (
            <div className="mt-2 text-[10px] font-bold px-2 py-1 rounded border inline-block bg-rose-500/15 border-rose-500/40 text-rose-300">
              EARNINGS BLACKOUT
              {Number.isFinite(Number(decision.earnings_days)) && (
                <span className="text-neutral-400 font-medium"> · report in {decision.earnings_days}d</span>
              )}
            </div>
          )}
          {(geoLevel === 'CRITICAL' || geoLevel === 'ELEVATED') && (
            <div className={`mt-2 text-[10px] font-bold px-2 py-1 rounded border inline-block ${
              geoLevel === 'CRITICAL'
                ? 'bg-rose-500/15 border-rose-500/40 text-rose-300'
                : 'bg-rose-500/10 border-rose-500/25 text-rose-400'
            }`}>
              GEO RISK {geoLevel}
              {geoRisk?.active_hotspots?.length > 0 && (
                <span className="text-neutral-400 font-medium"> · {geoRisk.active_hotspots.slice(0, 3).join(', ')}</span>
              )}
            </div>
          )}
        </div>
      </div>

      {(decision.drivers?.length > 0 || decision.risks?.length > 0) && (
        <div className="mt-4 pt-4 border-t border-neutral-800 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5">
          <div className="space-y-1.5">
            {(decision.drivers || []).map((d, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
                <span className="text-neutral-300 text-sm">{d}</span>
              </div>
            ))}
          </div>
          <div className="space-y-1.5">
            {(decision.risks || []).map((r, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-rose-500 shrink-0" />
                <span className="text-neutral-300 text-sm">{r}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
