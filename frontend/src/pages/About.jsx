// ABOUT — who we are, how the system thinks, and where it's going.

function Section({ title, children }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-6">
      <h2 className="text-lg font-bold text-white mb-3">{title}</h2>
      {children}
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
            <p>
              <span className="text-white font-semibold">Numbers over narratives.</span> Every verdict is a
              composite of five measured components — technicals, probabilistic forecast, news tape,
              geopolitical backdrop, fundamentals — each with its score shown, never a vibe.
            </p>
            <p>
              <span className="text-white font-semibold">One page, one verdict.</span> The system never shows
              two contradictory calls. When the chart and the world disagree, it says so, cuts its own
              conviction, and tells you there is no edge — because accuracy means never being confidently wrong.
            </p>
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
