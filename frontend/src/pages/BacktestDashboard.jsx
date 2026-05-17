import { useState, useEffect, useCallback } from 'react'

const PERIODS = [
  { label: '30 days',  days: 30 },
  { label: '90 days',  days: 90 },
  { label: '180 days', days: 180 },
  { label: '1 year',   days: 365 },
]

export default function BacktestDashboard() {
  const [days, setDays] = useState(90)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Pro analysis states
  const [proLoading, setProLoading] = useState({ wf: false, mc: false, rg: false, st: false })
  const [proData, setProData] = useState({ wf: null, mc: null, rg: null, st: null })
  const [proError, setProError] = useState({ wf: null, mc: null, rg: null, st: null })

  const runBacktest = useCallback(async (selectedDays) => {
    const useDays = selectedDays ?? days
    setLoading(true)
    setError(null)
    try {
      // Fixed sensible defaults — user only changes time period
      const params = new URLSearchParams({
        days: useDays, top_n: 10,
        stop_pct: 0.04, take_pct: 0.10, hold_days: 5,
        cost_bps: 5, slippage_bps: 5,
      })
      // Generous timeout: 365-day first call can take 60-90s
      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), 180000)
      const res = await fetch(`/api/backtest/detail?${params}`, { signal: controller.signal })
      clearTimeout(timeoutId)
      const json = await res.json()
      if (!json.ok) throw new Error(json.reason || 'Backtest failed')
      setData(json)
    } catch (e) {
      setError(e.name === 'AbortError' ? 'Backtest timed out (>3min). Try a shorter period or wait & retry.' : e.message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [days])

  const runProAnalysis = useCallback(async (kind) => {
    const endpoints = {
      wf: '/api/backtest-pro/walk-forward?train_months=4&test_months=1',
      mc: '/api/backtest-pro/monte-carlo?n_simulations=300&days=180',
      rg: '/api/backtest-pro/regimes?days=540',
      st: '/api/backtest-pro/stress-tests',
    }
    setProLoading(s => ({ ...s, [kind]: true }))
    setProError(s => ({ ...s, [kind]: null }))
    // 4-minute timeout so a hung backend can't leave UI spinning forever
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 240000)
    try {
      const res = await fetch(endpoints[kind], { signal: controller.signal })
      clearTimeout(timeoutId)
      const json = await res.json()
      if (!json.ok) throw new Error(json.reason || 'Analysis failed')
      setProData(s => ({ ...s, [kind]: json }))
    } catch (e) {
      clearTimeout(timeoutId)
      const msg = e.name === 'AbortError'
        ? 'Timed out (>4 min). Backend may be busy — try again in a minute.'
        : e.message
      setProError(s => ({ ...s, [kind]: msg }))
    } finally {
      setProLoading(s => ({ ...s, [kind]: false }))
    }
  }, [])

  useEffect(() => { runBacktest() }, [])  // initial load

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <h1 className="text-3xl font-bold text-white mb-2">Backtest Lab</h1>
      <p className="text-gray-400 mb-6">Replays historical data with chosen parameters — see what the strategy WOULD have done.</p>

      {/* Time period buttons — like analyze page */}
      <div className="bg-slate-800 rounded-lg p-4 mb-6 flex flex-wrap items-center gap-3">
        <span className="text-gray-400 text-sm mr-2">Time period:</span>
        {PERIODS.map(p => (
          <button
            key={p.days}
            onClick={() => { setDays(p.days); runBacktest(p.days) }}
            disabled={loading}
            className={`px-4 py-2 rounded font-medium transition-colors ${
              days === p.days
                ? 'bg-blue-600 text-white'
                : 'bg-slate-700 text-gray-300 hover:bg-slate-600'
            } disabled:opacity-50`}
          >
            {p.label}
          </button>
        ))}
        {loading && <span className="text-gray-400 text-sm ml-2">Running… (can take up to 3 min on 1-year)</span>}
      </div>

      {error && <div className="bg-red-900 text-red-200 p-3 rounded mb-4">Error: {error}</div>}

      {data && data.headline && (
        <>
          {/* Headline metrics */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
            <Metric label="Total Return" value={`${data.headline.total_return_pct?.toFixed(2) ?? '—'}%`}
              positive={data.headline.total_return_pct > 0} big />
            <Metric label="vs S&P" value={`${data.headline.alpha_vs_sp500_pct?.toFixed(2) ?? '—'}%`}
              positive={data.headline.alpha_vs_sp500_pct > 0} />
            <Metric label="Sharpe" value={data.headline.sharpe_ratio?.toFixed(2) ?? '—'}
              positive={data.headline.sharpe_ratio > 0} />
            <Metric label="Win Rate" value={`${data.headline.win_rate_pct?.toFixed(1) ?? '—'}%`}
              positive={data.headline.win_rate_pct > 50} />
            <Metric label="Profit Factor" value={data.headline.profit_factor?.toFixed(2) ?? '—'}
              positive={data.headline.profit_factor > 1} />
            <Metric label="Max Drawdown" value={`${data.headline.max_drawdown_pct?.toFixed(2) ?? '—'}%`}
              positive={false} />
            <Metric label="Total Trades" value={data.headline.total_trades ?? '—'} />
            <Metric label="Avg Win" value={`${data.headline.avg_win_pct?.toFixed(2) ?? '—'}%`} positive />
            <Metric label="Avg Loss" value={`${data.headline.avg_loss_pct?.toFixed(2) ?? '—'}%`} positive={false} />
            <Metric label="Final Equity" value={`$${data.headline.final_equity?.toLocaleString() ?? '—'}`} />
          </div>

          {/* Equity curve (simple ASCII-style) */}
          {data.equity_curve?.length > 0 && (
            <div className="bg-slate-800 rounded-lg p-4 mb-6">
              <h2 className="text-xl font-semibold text-white mb-2">Equity Curve ({data.equity_curve.length} days)</h2>
              <EquityChart points={data.equity_curve} sp={data.sp500_series} />
            </div>
          )}

          {/* Best / Worst trades */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <TradeCard title="Best Trade" trade={data.best_trade} positive />
            <TradeCard title="Worst Trade" trade={data.worst_trade} />
          </div>

          {/* Per-ticker breakdown */}
          {data.per_ticker?.length > 0 && (
            <div className="bg-slate-800 rounded-lg p-4 mb-6">
              <h2 className="text-xl font-semibold text-white mb-2">Per-Ticker Performance (top 30)</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-400 border-b border-slate-700">
                      <th className="text-left py-2 px-2">Ticker</th>
                      <th className="text-right py-2 px-2">Trades</th>
                      <th className="text-right py-2 px-2">Wins</th>
                      <th className="text-right py-2 px-2">Win Rate</th>
                      <th className="text-right py-2 px-2">Total PnL %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.per_ticker.map((t, i) => (
                      <tr key={i} className="border-b border-slate-700/50 hover:bg-slate-700/30">
                        <td className="py-1.5 px-2 text-white font-mono">{t.ticker}</td>
                        <td className="text-right py-1.5 px-2">{t.trades}</td>
                        <td className="text-right py-1.5 px-2">{t.wins}</td>
                        <td className="text-right py-1.5 px-2">{t.win_rate_pct?.toFixed(1) ?? '—'}%</td>
                        <td className={`text-right py-1.5 px-2 font-semibold ${(t.total_pnl_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {t.total_pnl_pct?.toFixed(2) ?? '—'}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Trade sample */}
          {data.trades_sample?.length > 0 && (
            <div className="bg-slate-800 rounded-lg p-4">
              <h2 className="text-xl font-semibold text-white mb-2">Trade Sample (first 50)</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-400 border-b border-slate-700">
                      <th className="text-left py-2 px-2">Ticker</th>
                      <th className="text-left py-2 px-2">Entry</th>
                      <th className="text-left py-2 px-2">Exit</th>
                      <th className="text-right py-2 px-2">Days</th>
                      <th className="text-right py-2 px-2">PnL %</th>
                      <th className="text-left py-2 px-2">Exit reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.trades_sample.map((t, i) => (
                      <tr key={i} className="border-b border-slate-700/50">
                        <td className="py-1.5 px-2 text-white font-mono">{t.ticker}</td>
                        <td className="py-1.5 px-2 text-gray-400">{t.entry_date}</td>
                        <td className="py-1.5 px-2 text-gray-400">{t.exit_date}</td>
                        <td className="text-right py-1.5 px-2">{t.days_held}</td>
                        <td className={`text-right py-1.5 px-2 font-semibold ${(t.pnl_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {t.pnl_pct?.toFixed(2) ?? '—'}%
                        </td>
                        <td className="py-1.5 px-2 text-gray-400">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      {!data && !loading && !error && (
        <div className="text-gray-400 p-6 bg-slate-800 rounded-lg">Pick a time period above to run a backtest.</div>
      )}

      {data?._stale && (
        <div className="bg-yellow-900/40 border border-yellow-700 text-yellow-200 p-3 rounded mb-4 text-sm">
          ⚠ Serving cached result ({data._stale_age_seconds}s old) — live data unavailable. Click again later for fresh.
        </div>
      )}

      {/* ── PRO ANALYSES — separate section, each loads independently ── */}
      <div className="mt-10 mb-3">
        <h2 className="text-2xl font-bold text-white">Pro Analyses</h2>
        <p className="text-gray-400 text-sm">Hedge-fund-grade validation. Each takes 30-120s.</p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProAnalysisCard
          title="Walk-Forward Validation"
          desc="Train on first half, test on second. If OOS Sharpe ≈ in-sample Sharpe → strategy is real. If OOS drops >50% → overfit."
          loading={proLoading.wf} error={proError.wf} data={proData.wf}
          onRun={() => runProAnalysis('wf')}
          render={(d) => (
            <div className="text-sm">
              <div>Windows: {d.windows?.length || d.n_windows || '—'}</div>
              <div>In-sample Sharpe: <span className="text-white font-bold">{d.avg_in_sample_sharpe?.toFixed(2) ?? '—'}</span></div>
              <div>OOS Sharpe: <span className="text-white font-bold">{d.avg_oos_sharpe?.toFixed(2) ?? '—'}</span></div>
              <div>Overfit ratio: <span className={d.overfit_ratio > 0.7 ? 'text-green-400' : 'text-yellow-400'}>{d.overfit_ratio?.toFixed(2) ?? '—'}</span></div>
            </div>
          )}
        />
        <ProAnalysisCard
          title="Monte Carlo (300 sims)"
          desc="Bootstrap-resample trades 300 times. Shows the distribution of possible outcomes — was result skill or luck?"
          loading={proLoading.mc} error={proError.mc} data={proData.mc}
          onRun={() => runProAnalysis('mc')}
          render={(d) => {
            const r = d.return_distribution || {}
            return (
              <div className="text-sm">
                <div>Mean return: <span className="text-white font-bold">{r.mean?.toFixed(2) ?? '—'}%</span></div>
                <div>Median: {r.median?.toFixed(2) ?? '—'}%</div>
                <div className="text-red-400">5% worst: {r.p5?.toFixed(2) ?? '—'}%</div>
                <div className="text-green-400">95% best: {r.p95?.toFixed(2) ?? '—'}%</div>
              </div>
            )
          }}
        />
        <ProAnalysisCard
          title="Regime-Conditional Stats"
          desc="Separate Sharpe / win rate for BULL / BEAR / SIDEWAYS market regimes. Tells you when strategy works."
          loading={proLoading.rg} error={proError.rg} data={proData.rg}
          onRun={() => runProAnalysis('rg')}
          render={(d) => (
            <div className="text-sm space-y-1">
              {Object.entries(d.regime_stats || {}).map(([reg, s]) => (
                <div key={reg} className="flex justify-between border-b border-slate-700 py-1">
                  <span className="text-gray-300">{reg}</span>
                  <span>{s.trades} trades | {s.win_rate_pct?.toFixed(1)}% wr | Sharpe {s.sharpe?.toFixed(2)}</span>
                </div>
              ))}
            </div>
          )}
        />
        <ProAnalysisCard
          title="Stress Tests"
          desc="Replay strategy across COVID 2020, 2022 inflation, SVB, Aug 2024 vol spike. Did it survive?"
          loading={proLoading.st} error={proError.st} data={proData.st}
          onRun={() => runProAnalysis('st')}
          render={(d) => (
            <div className="text-sm space-y-1">
              {(d.crises || []).map((sc, i) => (
                <div key={i} className="flex justify-between border-b border-slate-700 py-1">
                  <span className="text-gray-300" title={sc.description}>{sc.label}</span>
                  {sc.ok ? (
                    <span className={(sc.total_return_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                      {sc.total_return_pct?.toFixed(2) ?? '—'}% ({sc.total_trades ?? '—'} trades)
                    </span>
                  ) : (
                    <span className="text-gray-500">no data ({sc.reason?.slice(0, 30) ?? 'fail'})</span>
                  )}
                </div>
              ))}
              {d.crises?.length === 0 && <div className="text-gray-500">No scenarios returned.</div>}
            </div>
          )}
        />
      </div>
    </div>
  )
}

function ProAnalysisCard({ title, desc, loading, error, data, onRun, render }) {
  return (
    <div className="bg-slate-800 rounded-lg p-4">
      <div className="flex justify-between items-start mb-2">
        <div>
          <h3 className="text-lg font-semibold text-white">{title}</h3>
          <p className="text-xs text-gray-400 mt-1">{desc}</p>
        </div>
        <button onClick={onRun} disabled={loading}
          className="bg-purple-700 hover:bg-purple-600 disabled:bg-gray-600 text-white text-sm font-medium rounded px-3 py-1">
          {loading ? '…' : 'Run'}
        </button>
      </div>
      {error && <div className="text-red-400 text-sm mt-2">Error: {error}</div>}
      {data && !error && <div className="mt-3 text-gray-200">{render(data)}</div>}
      {!data && !error && !loading && <div className="text-gray-500 text-sm mt-2">Click Run to load.</div>}
    </div>
  )
}

function Metric({ label, value, positive, big }) {
  const color = positive === true ? 'text-green-400' : positive === false ? 'text-red-400' : 'text-white'
  return (
    <div className="bg-slate-800 rounded-lg p-3">
      <div className="text-xs text-gray-400 mb-1">{label}</div>
      <div className={`font-bold ${big ? 'text-3xl' : 'text-xl'} ${color}`}>{value}</div>
    </div>
  )
}

function TradeCard({ title, trade, positive }) {
  if (!trade) return null
  const color = positive ? 'text-green-400' : 'text-red-400'
  return (
    <div className="bg-slate-800 rounded-lg p-4">
      <div className="text-gray-400 text-sm mb-2">{title}</div>
      <div className="text-2xl font-bold text-white mb-1">{trade.ticker}</div>
      <div className={`text-3xl font-bold ${color} mb-2`}>{trade.pnl_pct?.toFixed(2)}%</div>
      <div className="text-sm text-gray-400 space-y-1">
        <div>Entry: ${trade.entry_price?.toFixed(2)} → Exit: ${trade.exit_price?.toFixed(2)}</div>
        <div>Held {trade.days_held} days | Exit reason: {trade.exit_reason}</div>
      </div>
    </div>
  )
}

function EquityChart({ points, sp }) {
  if (!points?.length) return null
  const w = 800, h = 200
  const equities = points.map(p => p.equity)
  const minE = Math.min(...equities), maxE = Math.max(...equities)
  const range = maxE - minE || 1
  const pathD = points.map((p, i) => {
    const x = (i / (points.length - 1)) * w
    const y = h - ((p.equity - minE) / range) * h
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  // SP500 normalized to same scale
  let spPath = ''
  if (sp?.length) {
    const spCloses = sp.map(p => p.close)
    const spMin = Math.min(...spCloses), spMax = Math.max(...spCloses)
    const spRange = spMax - spMin || 1
    spPath = sp.map((p, i) => {
      const x = (i / (sp.length - 1)) * w
      const y = h - ((p.close - spMin) / spRange) * h
      return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
    }).join(' ')
  }
  return (
    <div className="w-full overflow-x-auto">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{maxHeight: '300px'}}>
        {spPath && <path d={spPath} stroke="#666" strokeWidth="1.5" fill="none" strokeDasharray="3,3" />}
        <path d={pathD} stroke="#3b82f6" strokeWidth="2" fill="none" />
      </svg>
      <div className="text-xs text-gray-400 mt-2 flex gap-4">
        <span><span className="inline-block w-3 h-0.5 bg-blue-500 mr-1"></span>Strategy</span>
        {spPath && <span><span className="inline-block w-3 h-0.5 bg-gray-500 mr-1"></span>S&P 500</span>}
        <span className="ml-auto">{points[0]?.date} → {points[points.length-1]?.date}</span>
      </div>
    </div>
  )
}
