// ABOUT — who we are, how the system thinks, and where it's going.
//
// The graphics below are STATIC illustrations of how the engine works.
// They render fixed values (the engine's actual published weights and
// thresholds), fetch nothing, track nothing, and never change at runtime.

function Section({ title, children }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-6">
      <h2 className="text-lg font-bold text-white mb-3">{title}</h2>
      {children}
    </div>
  )
}

// ── Graphic 1: the decision pipeline — five weighted inputs, one verdict ──
const PIPELINE_INPUTS = [
  ['Technicals', '0.34'],
  ['Forecast', '0.18'],
  ['News', '0.16'],
  ['Geopolitics', '0.16'],
  ['Fundamentals', '0.16'],
]

function PipelineGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <svg viewBox="0 0 640 200" className="w-full" role="img"
           aria-label="Five weighted inputs combine into one composite score, which becomes one verdict">
        {PIPELINE_INPUTS.map(([name, w], i) => {
          const y = 26 + i * 38
          const mx = (152 + 300) / 2
          const my = (y + 100) / 2
          return (
            <g key={name}>
              <rect x="8" y={y - 14} width="144" height="27" rx="6" fill="#171717" stroke="#404040" />
              <text x="80" y={y + 4} textAnchor="middle" fontSize="11" fill="#d4d4d4" fontFamily="monospace">{name}</text>
              <line x1="152" y1={y} x2="300" y2="100" stroke="#404040" strokeWidth="1" />
              <text x={mx} y={my - 5} textAnchor="middle" fontSize="9" fill="#737373" fontFamily="monospace">×{w}</text>
            </g>
          )
        })}
        <rect x="300" y="78" width="112" height="44" rx="8" fill="#171717" stroke="#525252" />
        <text x="356" y="97" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#e5e5e5" fontFamily="monospace">COMPOSITE</text>
        <text x="356" y="112" textAnchor="middle" fontSize="9" fill="#737373" fontFamily="monospace">0–100</text>
        <line x1="412" y1="100" x2="468" y2="100" stroke="#525252" strokeWidth="1" />
        <polygon points="468,96 476,100 468,104" fill="#525252" />
        <rect x="478" y="78" width="154" height="44" rx="8" fill="#0a0a0a" stroke="#a3a3a3" />
        <text x="555" y="97" textAnchor="middle" fontSize="11" fontWeight="bold" fill="#ffffff" fontFamily="monospace">ONE VERDICT</text>
        <text x="555" y="112" textAnchor="middle" fontSize="8" fill="#737373" fontFamily="monospace">BUY · HOLD · AVOID</text>
      </svg>
      <p className="text-[10px] text-neutral-600 mt-1">
        Fixed weights, shown as shipped. No input can speak for itself — only the weighted composite decides.
      </p>
    </div>
  )
}

// ── Graphic 2: the verdict scale — the actual thresholds, drawn ──
const VERDICT_BANDS = [
  ['AVOID', '< 32', 'bg-rose-500/25 border-rose-500/40 text-rose-300'],
  ['REDUCE', '32–42', 'bg-rose-500/10 border-rose-500/25 text-rose-300/80'],
  ['HOLD', '42–58', 'bg-neutral-800/80 border-neutral-700 text-neutral-300'],
  ['ACCUMULATE', '58–68', 'bg-emerald-500/10 border-emerald-500/25 text-emerald-300/80'],
  ['BUY', '≥ 68', 'bg-emerald-500/25 border-emerald-500/40 text-emerald-300'],
]

function VerdictScaleGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <div className="grid grid-cols-5 gap-1" role="img"
           aria-label="Composite score thresholds: below 32 avoid, 32 to 42 reduce, 42 to 58 hold, 58 to 68 accumulate, 68 and above buy">
        {VERDICT_BANDS.map(([name, range, cls]) => (
          <div key={name} className={`border rounded-md px-1 py-2 text-center ${cls}`}>
            <div className="text-[9px] sm:text-[10px] font-bold tracking-wider">{name}</div>
            <div className="text-[9px] font-mono opacity-70">{range}</div>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-neutral-600 mt-2">
        The rulebook, not a dial: the composite score maps to exactly one action. Vetoes can only make it stricter.
      </p>
    </div>
  )
}

// ── Graphic 3: fail-closed gate — no trusted data, no verdict ──
function FailClosedGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <svg viewBox="0 0 640 150" className="w-full" role="img"
           aria-label="If data is trusted a verdict is issued; if not, the verdict is withheld as unrated">
        <rect x="8" y="55" width="160" height="40" rx="8" fill="#171717" stroke="#525252" />
        <text x="88" y="79" textAnchor="middle" fontSize="11" fontWeight="bold" fill="#e5e5e5" fontFamily="monospace">DATA TRUSTED?</text>
        <line x1="168" y1="68" x2="300" y2="42" stroke="#34d399" strokeWidth="1" />
        <text x="234" y="46" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#34d399" fontFamily="monospace">YES</text>
        <rect x="300" y="22" width="330" height="40" rx="8" fill="#0a0a0a" stroke="#34d399" strokeOpacity="0.5" />
        <text x="465" y="46" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#6ee7b7" fontFamily="monospace">VERDICT ISSUED — EVERY SCORE SHOWN</text>
        <line x1="168" y1="82" x2="300" y2="108" stroke="#fb7185" strokeWidth="1" />
        <text x="234" y="104" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#fb7185" fontFamily="monospace">NO</text>
        <rect x="300" y="88" width="330" height="40" rx="8" fill="#0a0a0a" stroke="#fb7185" strokeOpacity="0.5" />
        <text x="465" y="112" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#fda4af" fontFamily="monospace">UNRATED — VERDICT WITHHELD</text>
      </svg>
      <p className="text-[10px] text-neutral-600 mt-1">
        Fails closed. A wrong confident answer is worse than no answer, so bad data ends the conversation.
      </p>
    </div>
  )
}

// ── Graphic 4: evidence-split meter — disagreement cuts conviction ──
function ConvictionBars({ heights, convictionPct, convictionCls, label }) {
  return (
    <div>
      <div className="flex items-end gap-1.5 h-14 mb-2">
        {heights.map((h, i) => (
          <div key={i} className="w-2.5 rounded-sm bg-neutral-500/80" style={{ height: `${h}%` }} />
        ))}
      </div>
      <div className="h-2 rounded-full bg-neutral-800/80 overflow-hidden">
        <div className={`h-full rounded-full ${convictionCls}`} style={{ width: `${convictionPct}%` }} />
      </div>
      <div className="text-[9px] font-mono text-neutral-500 mt-1">{label}</div>
    </div>
  )
}

function EvidenceSplitGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4" role="img"
         aria-label="When component scores agree conviction stays full; when they split, conviction is cut">
      <div className="grid grid-cols-2 gap-6">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 mb-2">Evidence aligned</div>
          <ConvictionBars heights={[80, 72, 85, 70, 78]} convictionPct={85}
                          convictionCls="bg-emerald-500/70" label="CONVICTION — FULL" />
        </div>
        <div>
          <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-400 mb-2">Evidence split</div>
          <ConvictionBars heights={[90, 18, 75, 12, 50]} convictionPct={33}
                          convictionCls="bg-neutral-400" label="CONVICTION ×0.65 — REDUCED" />
        </div>
      </div>
      <p className="text-[10px] text-neutral-600 mt-3">
        The five bars are component scores. When they span too wide, the system cuts its own conviction and says there is no edge.
      </p>
    </div>
  )
}

export default function About() {
  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <h1 className="text-3xl font-black text-white tracking-tight">About Sentinel Quant</h1>
        <p className="text-sm text-neutral-400 mt-1">An autonomous research system built to beat the market — and to prove it.</p>
      </div>

      <div className="space-y-6">
        <Section title="The Founding">
          <p className="text-sm text-neutral-300 leading-relaxed">
            Sentinel Quant was founded by <span className="text-white font-semibold">Jackson Lee</span> at
            fourteen years old, with one goal and no interest in excuses: beat the market. Not with tips,
            not with narratives — with a system. What started as a stock analyzer has been rebuilt, audited
            and hardened into an autonomous decision engine that scans price action, news, geopolitics and
            fundamentals around the clock and says, in one word, what to do.
          </p>
        </Section>

        <Section title="Philosophy">
          <div className="space-y-3 text-sm text-neutral-300 leading-relaxed">
            <p>
              <span className="text-white font-semibold">Capital preservation first.</span> The engine fails
              closed: if the data can't be trusted, it withholds the verdict rather than guess. A wrong
              confident answer is worse than no answer.
            </p>
            <FailClosedGraphic />
            <p>
              <span className="text-white font-semibold">Numbers over narratives.</span> Every verdict is a
              composite of five measured components — technicals, probabilistic forecast, news tape,
              geopolitical backdrop, fundamentals — each with its score shown, never a vibe.
            </p>
            <PipelineGraphic />
            <p>
              <span className="text-white font-semibold">One page, one verdict.</span> The system never shows
              two contradictory calls. When the chart and the world disagree, it says so, cuts its own
              conviction, and tells you there is no edge — because accuracy means never being confidently wrong.
            </p>
            <EvidenceSplitGraphic />
          </div>
        </Section>

        <Section title="Strategy">
          <p className="text-sm text-neutral-300 leading-relaxed">
            Multi-factor and evidence-weighted. Technical structure carries the most weight because it is
            computed from full price history; news sentiment tilts the forecast only within strict bounds;
            geopolitical risk can veto a buy but never manufacture one. Entry timing gates every bullish
            verdict, and known binary events — earnings prints, policy escalations — are treated as risk to
            stand aside from, not noise to trade through. Every decision is recorded in a ledger and scored
            against the market, so the weights answer to evidence, not opinion.
          </p>
          <VerdictScaleGraphic />
        </Section>

        <Section title="The Goal">
          <p className="text-sm text-neutral-300 leading-relaxed">
            We look up to the firms that turned discipline into dynasties — <span className="text-white font-semibold">Two
            Sigma</span> and <span className="text-white font-semibold">Citadel</span> — operations where process
            beats prediction and the research never stops. Our ambition is unapologetic: build Sentinel Quant
            into a trillion-dollar company. The path there is not louder claims; it is a longer ledger —
            thousands of scored verdicts, measured hit rates, and a system that earns trust one decision at a time.
          </p>
        </Section>

        <p className="text-[10px] text-neutral-600 leading-snug">
          Sentinel Quant is a research and analysis system. Market data from Yahoo Finance; news from CNBC, CNN
          and Bloomberg. All verdicts, probabilities and scores are model outputs, not guarantees.
          Not financial advice.
        </p>
      </div>
    </div>
  )
}
