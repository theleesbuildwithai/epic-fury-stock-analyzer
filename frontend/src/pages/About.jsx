// ABOUT — who we are, how the system thinks, and where it's going.
//
// Every graphic on this page is a STATIC illustration of how the engine works.
// They render fixed values (the engine's actual published weights, thresholds
// and rules), fetch nothing, track nothing, hold no state, and never change at
// runtime. Nothing here can break when data is unavailable — there is no data.

function Section({ title, children }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-6">
      <h2 className="text-lg font-bold text-white mb-3">{title}</h2>
      {children}
    </div>
  )
}

// ── By the numbers — static shipped facts, not live metrics ──────────────────
const SHIPPED_FACTS = [
  ['5', 'weighted components per verdict'],
  ['2', 'independent price sources'],
  ['6', 'quote fallback layers'],
  ['7d', 'earnings blackout window'],
  ['11', 'test suites gate every deploy'],
  ['1', 'verdict per page — never two'],
]

function ByTheNumbers() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
      {SHIPPED_FACTS.map(([n, label]) => (
        <div key={label} className="bg-neutral-900/40 border border-neutral-800/60 rounded-lg px-3 py-3 text-center">
          <div className="text-xl font-black text-white font-mono">{n}</div>
          <div className="text-[9px] text-neutral-500 uppercase tracking-wider leading-tight mt-1">{label}</div>
        </div>
      ))}
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

// ── Graphic 5: how a verdict is born — the full path, end to end ──
const BIRTH_STEPS = [
  ['MARKET DATA', 'price · news · macro'],
  ['INTEGRITY GATE', 'verify · cross-check'],
  ['FIVE SCANNERS', 'scored 0–100 each'],
  ['COMPOSITE', 'weighted blend'],
  ['VETOES', 'can only tighten'],
  ['VERDICT', 'one word'],
  ['LEDGER', 'recorded · scored'],
]

function VerdictBornGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <svg viewBox="0 0 700 86" className="w-full" role="img"
           aria-label="Market data passes an integrity gate, is scored by five scanners, blended into a composite, checked against vetoes, issued as one verdict, and recorded in the ledger">
        {BIRTH_STEPS.map(([name, sub], i) => {
          const x = 4 + i * 100
          const isVerdict = name === 'VERDICT'
          return (
            <g key={name}>
              <rect x={x} y="22" width="88" height="42" rx="7" fill={isVerdict ? '#0a0a0a' : '#171717'}
                    stroke={isVerdict ? '#a3a3a3' : '#404040'} />
              <text x={x + 44} y="41" textAnchor="middle" fontSize="8.5" fontWeight="bold"
                    fill={isVerdict ? '#ffffff' : '#d4d4d4'} fontFamily="monospace">{name}</text>
              <text x={x + 44} y="54" textAnchor="middle" fontSize="7" fill="#737373" fontFamily="monospace">{sub}</text>
              {i < BIRTH_STEPS.length - 1 && (
                <polygon points={`${x + 90},40 ${x + 97},43 ${x + 90},46`} fill="#525252" />
              )}
            </g>
          )
        })}
      </svg>
      <p className="text-[10px] text-neutral-600 mt-1">
        Nothing skips a stage. Data that fails the gate never reaches a scanner; a verdict that clears every stage is still recorded and scored against the market.
      </p>
    </div>
  )
}

// ── Graphic 6: cross-source price verification — the second opinion ──
function CrossSourceGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <svg viewBox="0 0 640 170" className="w-full" role="img"
           aria-label="The live price and an independent reference price are compared. Under 3 percent apart is verified, 3 to 10 percent cuts conviction, over 10 percent withholds the verdict">
        <rect x="8" y="18" width="150" height="38" rx="7" fill="#171717" stroke="#404040" />
        <text x="83" y="35" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#d4d4d4" fontFamily="monospace">LIVE PRICE</text>
        <text x="83" y="48" textAnchor="middle" fontSize="8" fill="#737373" fontFamily="monospace">primary source</text>
        <rect x="8" y="112" width="150" height="38" rx="7" fill="#171717" stroke="#404040" />
        <text x="83" y="129" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#d4d4d4" fontFamily="monospace">REFERENCE PRICE</text>
        <text x="83" y="142" textAnchor="middle" fontSize="8" fill="#737373" fontFamily="monospace">independent source</text>
        <line x1="158" y1="37" x2="250" y2="78" stroke="#404040" strokeWidth="1" />
        <line x1="158" y1="131" x2="250" y2="90" stroke="#404040" strokeWidth="1" />
        <rect x="250" y="64" width="110" height="40" rx="7" fill="#171717" stroke="#525252" />
        <text x="305" y="88" textAnchor="middle" fontSize="10" fontWeight="bold" fill="#e5e5e5" fontFamily="monospace">COMPARE</text>
        <line x1="360" y1="84" x2="408" y2="34" stroke="#34d399" strokeWidth="1" strokeOpacity="0.6" />
        <line x1="360" y1="84" x2="408" y2="84" stroke="#737373" strokeWidth="1" />
        <line x1="360" y1="84" x2="408" y2="134" stroke="#fb7185" strokeWidth="1" strokeOpacity="0.6" />
        <rect x="410" y="16" width="222" height="34" rx="7" fill="#0a0a0a" stroke="#34d399" strokeOpacity="0.5" />
        <text x="521" y="31" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#6ee7b7" fontFamily="monospace">&lt; 3% APART — VERIFIED</text>
        <text x="521" y="43" textAnchor="middle" fontSize="7.5" fill="#737373" fontFamily="monospace">normal intraday noise</text>
        <rect x="410" y="66" width="222" height="34" rx="7" fill="#0a0a0a" stroke="#525252" />
        <text x="521" y="81" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#d4d4d4" fontFamily="monospace">3–10% — CONVICTION ×0.8</text>
        <text x="521" y="93" textAnchor="middle" fontSize="7.5" fill="#737373" fontFamily="monospace">verdict stands, labeled</text>
        <rect x="410" y="116" width="222" height="34" rx="7" fill="#0a0a0a" stroke="#fb7185" strokeOpacity="0.5" />
        <text x="521" y="131" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#fda4af" fontFamily="monospace">&gt; 10% — VERDICT WITHHELD</text>
        <text x="521" y="143" textAnchor="middle" fontSize="7.5" fill="#737373" fontFamily="monospace">one source is wrong</text>
      </svg>
      <p className="text-[10px] text-neutral-600 mt-1">
        The one number every verdict depends on gets a second, independent opinion. Only a measured disagreement can
        withhold a verdict — verification infrastructure being down never blocks the analysis, it just labels it.
      </p>
    </div>
  )
}

// ── Graphic 7: earnings blackout — stand aside from binary events ──
function EarningsBlackoutGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4">
      <svg viewBox="0 0 640 110" className="w-full" role="img"
           aria-label="On a timeline toward an earnings print, new entries are allowed until seven days before the report, then withheld until the print clears">
        <line x1="20" y1="62" x2="620" y2="62" stroke="#404040" strokeWidth="1" />
        <rect x="20" y="40" width="340" height="44" rx="6" fill="#171717" fillOpacity="0.5" />
        <text x="190" y="30" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#a3a3a3" fontFamily="monospace">NORMAL — VERDICTS AS SCORED</text>
        <rect x="360" y="40" width="180" height="44" rx="6" fill="#fb7185" fillOpacity="0.1" stroke="#fb7185" strokeOpacity="0.4" />
        <text x="450" y="30" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#fda4af" fontFamily="monospace">BLACKOUT — NO NEW ENTRIES</text>
        <line x1="540" y1="34" x2="540" y2="90" stroke="#fb7185" strokeWidth="2" strokeOpacity="0.8" />
        <text x="540" y="104" textAnchor="middle" fontSize="9" fontWeight="bold" fill="#fda4af" fontFamily="monospace">EARNINGS PRINT</text>
        <text x="360" y="100" textAnchor="middle" fontSize="8" fill="#737373" fontFamily="monospace">T−7 days</text>
        <text x="595" y="52" textAnchor="middle" fontSize="8" fill="#a3a3a3" fontFamily="monospace">re-evaluate</text>
        <text x="595" y="64" textAnchor="middle" fontSize="8" fill="#a3a3a3" fontFamily="monospace">after the</text>
        <text x="595" y="76" textAnchor="middle" fontSize="8" fill="#a3a3a3" fontFamily="monospace">print clears</text>
      </svg>
      <p className="text-[10px] text-neutral-600 mt-1">
        Inside seven days of a scheduled report, every bullish verdict is gated to HOLD and labeled. An earnings print
        is a coin flip with a date on it — the system stands aside rather than bet on it.
      </p>
    </div>
  )
}

// ── Graphic 8: the veto ladder — every way the system says no ──
const VETOES = [
  ['Geopolitical risk CRITICAL', 'BUY', 'ACCUMULATE', 'active-conflict tape caps aggression'],
  ['Entry timing reads AVOID', 'BUY / ACCUMULATE', 'HOLD', 'right stock, wrong day'],
  ['Earnings within 7 days', 'BUY / ACCUMULATE', 'HOLD', 'binary event ahead — stand aside'],
  ['Price sources disagree >10%', 'ANY VERDICT', 'UNRATED', 'the price itself is in dispute'],
]

function VetoLadderGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4" role="img"
         aria-label="Four vetoes: critical geopolitical risk caps buy to accumulate, timing avoid gates to hold, earnings within seven days gates to hold, and a price mismatch withholds the verdict entirely">
      <div className="space-y-2">
        {VETOES.map(([trigger, from, to, why]) => (
          <div key={trigger} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
            <span className="w-full sm:w-56 font-semibold text-neutral-300">{trigger}</span>
            <span className="font-mono text-emerald-400/80 line-through decoration-neutral-600">{from}</span>
            <span className="text-neutral-600">→</span>
            <span className={`font-mono font-bold ${to === 'UNRATED' ? 'text-rose-300' : 'text-neutral-200'}`}>{to}</span>
            <span className="text-neutral-600 text-[10px]">{why}</span>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-neutral-600 mt-3">
        Vetoes run one direction only: they can downgrade a verdict, never upgrade one. The system has more ways to say
        no than yes — by design.
      </p>
    </div>
  )
}

// ── Graphic 9: the accuracy loop — verdicts answer to the market ──
const LOOP_STEPS = [
  ['VERDICT ISSUED', 'action · conviction · probability'],
  ['RECORDED', 'written to the ledger, same day'],
  ['MATURED', 'scored at 7 and 30 days'],
  ['MEASURED', 'hit rates and calibration, with sample sizes'],
]

function LedgerLoopGraphic() {
  return (
    <div className="mt-4 bg-neutral-950/60 border border-neutral-800/60 rounded-lg p-4" role="img"
         aria-label="Each verdict is recorded, matured for 7 and 30 days, measured for accuracy, and the evidence feeds back into the system">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        {LOOP_STEPS.map(([name, sub], i) => (
          <div key={name} className="relative bg-neutral-900/60 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-[9px] font-mono text-neutral-600 mb-1">{String(i + 1).padStart(2, '0')}</div>
            <div className="text-[10px] font-bold text-neutral-200 font-mono tracking-wide">{name}</div>
            <div className="text-[9px] text-neutral-500 mt-1 leading-snug">{sub}</div>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-2 mt-3">
        <div className="flex-1 h-px bg-neutral-800" />
        <span className="text-[9px] font-mono text-neutral-500">EVIDENCE FEEDS BACK — WEIGHTS ANSWER TO RESULTS, NOT OPINION</span>
        <div className="flex-1 h-px bg-neutral-800" />
      </div>
      <p className="text-[10px] text-neutral-600 mt-2">
        No verdict escapes the scoreboard. First call of the day stands — no retroactive edits, no survivorship tricks.
      </p>
    </div>
  )
}

// ── Data sources — approved providers and their roles ──
const SOURCES = [
  ['Yahoo Finance', 'Primary market data', 'Price history, quotes, fundamentals — the base layer every indicator is computed from.'],
  ['CNBC', 'Independent verification + news', 'Second opinion on every verdict price, and first-line quote backup. Different infrastructure, on purpose.'],
  ['Bloomberg', 'News tape', 'Market, macro and policy headlines feeding the sentiment and geopolitical scanners.'],
  ['CNN', 'News tape', 'Breaking geopolitical and political coverage for the event-risk read.'],
]

function DataSourcesSection() {
  return (
    <Section title="Data Sources">
      <p className="text-sm text-neutral-300 leading-relaxed mb-4">
        Four approved providers, each with a defined job. Quotes fall through six independent layers before the system
        ever reports a price as unavailable — and a cached last-known price is always labeled as such, never passed off
        as live.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {SOURCES.map(([name, role, desc]) => (
          <div key={name} className="bg-neutral-950/60 border border-neutral-800/60 rounded-lg px-4 py-3">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-bold text-white">{name}</span>
              <span className="text-[9px] font-mono uppercase tracking-wider text-neutral-500">{role}</span>
            </div>
            <p className="text-[11px] text-neutral-400 mt-1 leading-snug">{desc}</p>
          </div>
        ))}
      </div>
    </Section>
  )
}

// ── Milestones — the road so far, in order ──
const MILESTONES = [
  ['Founding', 'A stock analyzer built at fourteen, with one goal: beat the market with a system, not opinions.'],
  ['Multi-source data shield', 'Six quote layers and a persistent price cache so no single provider outage can blind the system.'],
  ['News + geopolitical overlay', 'Entries and exits learn to read the tape: headlines, sectors, conflicts, trade policy.'],
  ['Entry timing engine', 'Right stock is not enough — the system grades whether today is the day.'],
  ['Autonomous decision engine', 'Five scanners, fixed weights, one verdict per page. Vetoes that only tighten.'],
  ['Verdict ledger', 'Every call recorded the day it is made and scored against the market at 7 and 30 days.'],
  ['Data integrity layer', 'Cross-source price verification, earnings blackout gating, and failure labels on screen instead of silent fallbacks.'],
]

function MilestonesSection() {
  return (
    <Section title="Milestones">
      <div className="space-y-0">
        {MILESTONES.map(([name, desc], i) => (
          <div key={name} className="flex gap-4">
            <div className="flex flex-col items-center">
              <div className={`w-2.5 h-2.5 rounded-full mt-1 shrink-0 ${i === MILESTONES.length - 1 ? 'bg-white' : 'bg-neutral-600'}`} />
              {i < MILESTONES.length - 1 && <div className="w-px flex-1 bg-neutral-800" />}
            </div>
            <div className="pb-5">
              <div className="text-sm font-bold text-white leading-tight">{name}</div>
              <p className="text-[12px] text-neutral-400 mt-0.5 leading-snug">{desc}</p>
            </div>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-neutral-600">The research never stops. Each layer was built because the previous one exposed what was still missing.</p>
    </Section>
  )
}

// ── Glossary — the words on every analyze page, defined once ──
const GLOSSARY = [
  ['Composite', 'The 0–100 weighted blend of all five component scores. The only number that picks the action.'],
  ['Conviction', 'How strongly the evidence agrees with itself, 5–97. Disagreement and price variance cut it; nothing inflates it.'],
  ['Stance', 'Directional read behind the action: BULLISH, NEUTRAL or BEARISH.'],
  ['UNRATED', 'The system refused to grade. Bad or disputed price data withholds the verdict rather than guess.'],
  ['Earnings blackout', 'A scheduled report within 7 days. Bullish verdicts are gated to HOLD until the print clears.'],
  ['Evidence split', 'Component scores span 45+ points — the chart and the world disagree. Conviction is cut and it says so.'],
  ['Verdict ledger', 'The permanent record. Every verdict scored against actual 7- and 30-day market outcomes.'],
]

function GlossarySection() {
  return (
    <Section title="Glossary">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
        {GLOSSARY.map(([term, def]) => (
          <div key={term}>
            <div className="text-[11px] font-bold font-mono uppercase tracking-wider text-neutral-200">{term}</div>
            <p className="text-[11px] text-neutral-400 mt-0.5 leading-snug">{def}</p>
          </div>
        ))}
      </div>
    </Section>
  )
}

export default function About() {
  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Hero */}
      <div className="mb-6">
        <p className="text-[10px] font-bold uppercase tracking-[0.25em] text-neutral-500 mb-2">Sentinel Quant · Autonomous Research</p>
        <h1 className="text-3xl sm:text-4xl font-black text-white tracking-tight">Built to beat the market — and to prove it.</h1>
        <p className="text-sm text-neutral-400 mt-2 max-w-2xl leading-relaxed">
          An autonomous decision engine that scans price action, news, geopolitics and fundamentals around the clock,
          verifies its own data, and says — in one word — what to do.
        </p>
      </div>

      <div className="space-y-6">
        <ByTheNumbers />

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

        <Section title="How a Verdict Is Born">
          <p className="text-sm text-neutral-300 leading-relaxed">
            Every verdict walks the same path, every time. Data is fetched and checked before any scanner sees it;
            five scanners score it independently; the weighted composite picks an action; vetoes get the final word;
            and the result is recorded in a ledger that will grade it against the market.
          </p>
          <VerdictBornGraphic />
        </Section>

        <Section title="Data Integrity">
          <div className="space-y-3 text-sm text-neutral-300 leading-relaxed">
            <p>
              <span className="text-white font-semibold">Every price gets a second opinion.</span> Before the
              engine grades a stock, the live price is checked against an independent source on separate
              infrastructure. Agreement is verified; small variance cuts conviction and says so; real
              disagreement withholds the verdict entirely.
            </p>
            <CrossSourceGraphic />
            <p>
              <span className="text-white font-semibold">Known binary events are not noise to trade through.</span>{' '}
              When a company is about to report, no model edge survives the coin flip of the print. The system
              gates new entries and waits.
            </p>
            <EarningsBlackoutGraphic />
            <p>
              <span className="text-white font-semibold">Failures are shown, never hidden.</span> Stale data,
              cached reports and source disagreements are labeled on screen, on the page where the verdict
              lives — a fallback the user can't see is a lie of omission.
            </p>
          </div>
        </Section>

        <Section title="The Veto Ladder">
          <p className="text-sm text-neutral-300 leading-relaxed">
            The composite proposes; the vetoes dispose. Four conditions can override any score the moment they
            trigger — and every one of them makes the verdict stricter, never bolder.
          </p>
          <VetoLadderGraphic />
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

        <Section title="The Accuracy Loop">
          <p className="text-sm text-neutral-300 leading-relaxed">
            Claims are cheap; ledgers are not. Every verdict the engine issues enters a permanent record the day
            it is made and is graded against what the market actually did.
          </p>
          <LedgerLoopGraphic />
        </Section>

        <DataSourcesSection />

        <MilestonesSection />

        <GlossarySection />

        <Section title="The Goal">
          <p className="text-sm text-neutral-300 leading-relaxed">
            We look up to the firms that turned discipline into dynasties — <span className="text-white font-semibold">Two
            Sigma</span> and <span className="text-white font-semibold">Citadel</span> — operations where process
            beats prediction and the research never stops. Our ambition is unapologetic: build Sentinel Quant
            into a trillion-dollar company. The path there is not louder claims; it is a longer ledger —
            thousands of scored verdicts, measured hit rates, and a system that earns trust one decision at a time.
          </p>
        </Section>

        {/* Footer disclaimer */}
        <div className="border-t border-neutral-800/60 pt-4">
          <p className="text-[10px] text-neutral-600 leading-snug">
            Sentinel Quant is a research and analysis system. Market data from Yahoo Finance; news from CNBC, CNN
            and Bloomberg. All verdicts, probabilities and scores are model outputs, not guarantees. The graphics on
            this page are static illustrations of published rules — they display nothing live and track nothing.
            Not financial advice.
          </p>
        </div>
      </div>
    </div>
  )
}
