import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'

// ABOUT — who we are, how the system thinks, and the scoreboard.
//
// The accuracy panel is the part that matters: it reads /api/decision-accuracy,
// the measured record of every verdict the engine has issued, scored against
// what the market actually did. A firm that claims accuracy without a ledger
// is marketing. The ledger is the product.

function Section({ title, children }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-6">
      <h2 className="text-lg font-bold text-white mb-3">{title}</h2>
      {children}
    </div>
  )
}

function AccuracyPanel() {
  const [acc, setAcc] = useState(null)
  useEffect(() => {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), 15000)
    fetch('/api/decision-accuracy', { signal: ctrl.signal })
      .then(r => (r.ok ? r.json() : null))
      .then(j => { if (j && j.ok) setAcc(j) })
      .catch(() => {})
      .finally(() => clearTimeout(timer))
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [])

  const actions = acc?.by_action ? Object.entries(acc.by_action) : []

  return (
    <Section title="The Scoreboard — Measured, Not Claimed">
      <p className="text-sm text-neutral-400 mb-4">
        Every System Decision the engine issues is written to a permanent ledger and scored against
        the real market 7 and 30 days later. Nothing is edited after the fact. This table is that
        record — including the sample sizes, because a hit rate without its sample size is a lie.
      </p>
      {!acc ? (
        <p className="text-xs text-neutral-500">Ledger loading — or no verdicts have matured yet. The scoreboard fills in as decisions age past their horizons.</p>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-6">
            <div>
              <div className="text-2xl font-bold font-mono text-white">{acc.total_verdicts}</div>
              <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Verdicts recorded</div>
            </div>
            <div>
              <div className="text-2xl font-bold font-mono text-white">{acc.scored_30d}</div>
              <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Scored at 30 days</div>
            </div>
          </div>
          {actions.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-[10px] font-bold uppercase tracking-wider text-neutral-500 border-b border-neutral-800/60">
                    <th className="py-2 pr-4">Action</th>
                    <th className="py-2 pr-4 text-right">Verdicts</th>
                    <th className="py-2 pr-4 text-right">Avg 7d</th>
                    <th className="py-2 pr-4 text-right">Avg 30d</th>
                    <th className="py-2 text-right">Hit rate (n)</th>
                  </tr>
                </thead>
                <tbody>
                  {actions.map(([a, v]) => (
                    <tr key={a} className="border-b border-neutral-800/40 last:border-b-0">
                      <td className={`py-2 pr-4 font-bold ${
                        a === 'BUY' || a === 'ACCUMULATE' ? 'text-emerald-400'
                          : a === 'REDUCE' || a === 'AVOID' ? 'text-rose-400' : 'text-neutral-200'
                      }`}>{a}</td>
                      <td className="py-2 pr-4 text-right font-mono text-neutral-300">{v.verdicts}</td>
                      <td className="py-2 pr-4 text-right font-mono text-neutral-300">{v.avg_return_7d_pct != null ? `${v.avg_return_7d_pct > 0 ? '+' : ''}${v.avg_return_7d_pct}%` : '—'}</td>
                      <td className="py-2 pr-4 text-right font-mono text-neutral-300">{v.avg_return_30d_pct != null ? `${v.avg_return_30d_pct > 0 ? '+' : ''}${v.avg_return_30d_pct}%` : '—'}</td>
                      <td className="py-2 text-right font-mono text-neutral-300">{v.hit_rate_pct != null ? `${v.hit_rate_pct}% (${v.hit_sample})` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {Array.isArray(acc.calibration) && acc.calibration.some(c => c.n > 0) && (
            <div>
              <p className="text-[10px] font-bold uppercase tracking-wider text-neutral-500 mb-2">Forecast calibration — stated 30-day upside probability vs what happened</p>
              <div className="flex flex-wrap gap-3">
                {acc.calibration.map(c => (
                  <div key={c.stated_prob_up_30d} className="bg-neutral-900/60 border border-neutral-800/60 rounded-lg px-4 py-2">
                    <div className="text-xs font-mono text-neutral-400">{c.stated_prob_up_30d}</div>
                    <div className="text-sm font-bold font-mono text-white">
                      {c.realized_up_rate_pct != null ? `${c.realized_up_rate_pct}% up` : '—'}
                      <span className="text-neutral-600 font-normal"> · n={c.n}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Section>
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

        <AccuracyPanel />

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
