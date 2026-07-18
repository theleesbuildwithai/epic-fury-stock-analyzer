import { useState, useCallback } from 'react'

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

  // Pro analysis states (wf/mc/rg). Stress tests are handled per-scenario below.
  const [proLoading, setProLoading] = useState({ wf: false, mc: false, rg: false })
  const [proData, setProData] = useState({ wf: null, mc: null, rg: null })
  const [proError, setProError] = useState({ wf: null, mc: null, rg: null })

  // Per-scenario stress test state (one Run button per crisis)
  const [scenarios, setScenarios] = useState(null)         // list from backend
  const [scenariosError, setScenariosError] = useState(null)
  const [scenariosLoading, setScenariosLoading] = useState(false)
  const [scResults, setScResults] = useState({})           // {label: {ok, total_return_pct, ...}}
  const [scLoading, setScLoading] = useState({})           // {label: bool}
  const [scError, setScError] = useState({})               // {label: string|null}

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

  // NOTE: no auto-run on mount. User must explicitly click a time-period
  // button to start a backtest (so we don't burn yfinance budget on idle visits).

  // Fetch the list of stress-test scenarios on demand (only when user opens the section)
  const loadScenarios = useCallback(async () => {
    if (scenarios || scenariosLoading) return
    setScenariosLoading(true)
    setScenariosError(null)
    try {
      const res = await fetch('/api/backtest-pro/stress-test-scenarios')
      const json = await res.json()
      if (!json.ok) throw new Error(json.reason || 'failed to load scenarios')
      setScenarios(json.scenarios || [])
    } catch (e) {
      setScenariosError(e.message)
    } finally {
      setScenariosLoading(false)
    }
  }, [scenarios, scenariosLoading])

  const runSingleStress = useCallback(async (label) => {
    setScLoading(s => ({ ...s, [label]: true }))
    setScError(s => ({ ...s, [label]: null }))
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 240000)
    try {
      const res = await fetch(`/api/backtest-pro/stress-test-one?label=${encodeURIComponent(label)}`,
                              { signal: controller.signal })
      clearTimeout(timeoutId)
      const json = await res.json()
      if (!json.ok) throw new Error(json.reason || 'scenario failed')
      setScResults(s => ({ ...s, [label]: json }))
    } catch (e) {
      clearTimeout(timeoutId)
      const msg = e.name === 'AbortError'
        ? 'Timed out (>4 min). Try again in a minute.'
        : e.message
      setScError(s => ({ ...s, [label]: msg }))
    } finally {
      setScLoading(s => ({ ...s, [label]: false }))
    }
  }, [])

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <h1 className="text-4xl font-bold mb-2">
        <span className="text-gradient">Backtest</span> <span className="text-white">Lab</span>
      </h1>
      <p className="text-neutral-400 mb-6">Replays historical data with chosen parameters — see what the strategy WOULD have done.</p>
      <div className="section-divider mb-6" />

      {/* Time period buttons — like analyze page */}
      <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4 mb-6 flex flex-wrap items-center gap-3">
        <span className="text-neutral-400 text-sm mr-2">Time period:</span>
        {PERIODS.map(p => (
          <button
            key={p.days}
            onClick={() => { setDays(p.days); runBacktest(p.days) }}
            disabled={loading}
            className={`px-4 py-2 rounded font-medium transition-colors ${
              days === p.days
                ? 'bg-white text-black border border-white'
                : 'bg-neutral-900 border border-neutral-700 text-neutral-300 hover:border-neutral-500 hover:text-white'
            } disabled:opacity-50`}
          >
            {p.label}
          </button>
        ))}
        {loading && <span className="text-neutral-400 text-sm ml-2">Running… (can take up to 3 min on 1-year)</span>}
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
            <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4 mb-6">
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
            <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4 mb-6">
              <h2 className="text-xl font-semibold text-white mb-2">Per-Ticker Performance (top 30)</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-neutral-400 border-b border-neutral-800">
                      <th className="text-left py-2 px-2">Ticker</th>
                      <th className="text-right py-2 px-2">Trades</th>
                      <th className="text-right py-2 px-2">Wins</th>
                      <th className="text-right py-2 px-2">Win Rate</th>
                      <th className="text-right py-2 px-2">Total PnL %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.per_ticker.map((t, i) => (
                      <tr key={i} className="border-b border-neutral-800/50 hover:bg-neutral-900/40">
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
            <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4">
              <h2 className="text-xl font-semibold text-white mb-2">Trade Sample (first 50)</h2>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-neutral-400 border-b border-neutral-800">
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
                      <tr key={i} className="border-b border-neutral-800/50">
                        <td className="py-1.5 px-2 text-white font-mono">{t.ticker}</td>
                        <td className="py-1.5 px-2 text-neutral-400">{t.entry_date}</td>
                        <td className="py-1.5 px-2 text-neutral-400">{t.exit_date}</td>
                        <td className="text-right py-1.5 px-2">{t.days_held}</td>
                        <td className={`text-right py-1.5 px-2 font-semibold ${(t.pnl_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {t.pnl_pct?.toFixed(2) ?? '—'}%
                        </td>
                        <td className="py-1.5 px-2 text-neutral-400">{t.exit_reason}</td>
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
        <div className="text-neutral-400 p-6 bg-black border border-neutral-700 card-accent-top rounded-lg">Pick a time period above to run a backtest.</div>
      )}

      {data?._stale && (
        <div className="bg-red-900/40 border border-red-700 text-red-200 p-3 rounded mb-4 text-sm">
          Serving cached result ({data._stale_age_seconds}s old) — live data unavailable. Click again later for fresh.
        </div>
      )}

      {/* ── PRO ANALYSES — separate section, each loads independently ── */}
      <div className="mt-12 mb-4">
        <div className="section-divider mb-6" />
        <h2 className="text-3xl font-bold">
          <span className="text-gradient">Pro</span> <span className="text-white">Analyses</span>
        </h2>
        <p className="text-neutral-400 text-sm mt-1">Hedge-fund-grade validation. Each takes 30-120s.</p>
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
              <div>Overfit ratio: <span className={d.overfit_ratio > 0.7 ? 'text-green-400' : 'text-red-400'}>{d.overfit_ratio?.toFixed(2) ?? '—'}</span></div>
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
                <div key={reg} className="flex justify-between border-b border-neutral-800 py-1">
                  <span className="text-neutral-300">{reg}</span>
                  <span>{s.trades} trades | {s.win_rate_pct?.toFixed(1)}% wr | Sharpe {s.sharpe?.toFixed(2)}</span>
                </div>
              ))}
            </div>
          )}
        />
        <StressTestCard
          scenarios={scenarios}
          scenariosLoading={scenariosLoading}
          scenariosError={scenariosError}
          onLoad={loadScenarios}
          onRunOne={runSingleStress}
          results={scResults}
          loadingMap={scLoading}
          errorMap={scError}
        />
      </div>
    </div>
  )
}

function ProAnalysisCard({ title, desc, loading, error, data, onRun, render }) {
  return (
    <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4">
      <div className="flex justify-between items-start mb-2">
        <div>
          <h3 className="text-lg font-semibold text-white">{title}</h3>
          <p className="text-xs text-neutral-400 mt-1">{desc}</p>
        </div>
        <button onClick={onRun} disabled={loading}
          className="bg-white hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 text-black text-sm font-medium rounded px-3 py-1">
          {loading ? '…' : 'Run'}
        </button>
      </div>
      {error && <div className="text-red-400 text-sm mt-2">Error: {error}</div>}
      {data && !error && <div className="mt-3 text-gray-200">{render(data)}</div>}
      {!data && !error && !loading && <div className="text-neutral-500 text-sm mt-2">Click Run to load.</div>}
    </div>
  )
}

function StressTestCard({ scenarios, scenariosLoading, scenariosError, onLoad,
                         onRunOne, results, loadingMap, errorMap }) {
  return (
    <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4">
      <div className="flex justify-between items-start mb-2">
        <div>
          <h3 className="text-lg font-semibold text-white">Stress Tests</h3>
          <p className="text-xs text-neutral-400 mt-1">
            Replay strategy across major historical crises. Each scenario runs
            independently — click Run on the one you want to test.
          </p>
        </div>
        {!scenarios && (
          <button onClick={onLoad} disabled={scenariosLoading}
            className="bg-white hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 text-black text-sm font-medium rounded px-3 py-1">
            {scenariosLoading ? '…' : 'Load'}
          </button>
        )}
      </div>
      {scenariosError && <div className="text-red-400 text-sm mt-2">Error: {scenariosError}</div>}
      {!scenarios && !scenariosLoading && !scenariosError && (
        <div className="text-neutral-500 text-sm mt-2">Click Load to fetch scenarios.</div>
      )}
      {scenarios && scenarios.length === 0 && (
        <div className="text-neutral-500 text-sm mt-2">No scenarios available.</div>
      )}
      {scenarios && scenarios.length > 0 && (
        <div className="mt-3 space-y-2">
          {scenarios.map((sc) => {
            const r = results[sc.label]
            const isLoading = !!loadingMap[sc.label]
            const err = errorMap[sc.label]
            return (
              <div key={sc.label} className="border-b border-neutral-800 pb-2">
                <div className="flex justify-between items-start gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="text-gray-200 text-sm font-medium truncate" title={sc.description}>
                      {sc.label}
                    </div>
                    <div className="text-xs text-neutral-500">
                      {sc.start} → {sc.end} · {sc.description}
                    </div>
                  </div>
                  <button onClick={() => onRunOne(sc.label)} disabled={isLoading}
                    className="bg-white hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 text-black text-xs font-medium rounded px-3 py-1 whitespace-nowrap">
                    {isLoading ? '…' : (r ? 'Re-run' : 'Run')}
                  </button>
                </div>
                {err && <div className="text-red-400 text-xs mt-1">Error: {err}</div>}
                {r && r.ok && (
                  <div className="text-xs text-neutral-300 mt-1 flex flex-wrap gap-x-4 gap-y-0.5">
                    <span className={(r.total_return_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                      Return: {r.total_return_pct?.toFixed(2) ?? '—'}%
                    </span>
                    <span>vs S&P: {r.alpha_vs_sp500_pct?.toFixed(2) ?? '—'}%</span>
                    <span>Sharpe: {r.sharpe_ratio?.toFixed(2) ?? '—'}</span>
                    <span>WinRate: {r.win_rate_pct?.toFixed(1) ?? '—'}%</span>
                    <span>MaxDD: {r.max_drawdown_pct?.toFixed(2) ?? '—'}%</span>
                    <span>Trades: {r.total_trades ?? '—'}</span>
                  </div>
                )}
                {r && !r.ok && (
                  <div className="text-xs text-neutral-500 mt-1">no data ({r.reason?.slice(0, 60) ?? 'fail'})</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Metric({ label, value, positive, big }) {
  const color = positive === true ? 'text-green-400' : positive === false ? 'text-red-400' : 'text-white'
  return (
    <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-3">
      <div className="text-xs text-neutral-400 mb-1">{label}</div>
      <div className={`font-bold ${big ? 'text-3xl' : 'text-xl'} ${color}`}>{value}</div>
    </div>
  )
}

function TradeCard({ title, trade, positive }) {
  if (!trade) return null
  const color = positive ? 'text-green-400' : 'text-red-400'
  return (
    <div className="bg-black border border-neutral-700 card-accent-top rounded-lg p-4">
      <div className="text-neutral-400 text-sm mb-2">{title}</div>
      <div className="text-2xl font-bold text-white mb-1">{trade.ticker}</div>
      <div className={`text-3xl font-bold ${color} mb-2`}>{trade.pnl_pct?.toFixed(2) ?? '—'}%</div>
      <div className="text-sm text-neutral-400 space-y-1">
        <div>Entry: ${trade.entry_price?.toFixed(2) ?? '—'} → Exit: ${trade.exit_price?.toFixed(2) ?? '—'}</div>
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
        <path d={pathD} stroke="#22c55e" strokeWidth="2" fill="none" />
      </svg>
      <div className="text-xs text-neutral-400 mt-2 flex gap-4">
        <span><span className="inline-block w-3 h-0.5 bg-green-500 mr-1"></span>Strategy</span>
        {spPath && <span><span className="inline-block w-3 h-0.5 bg-neutral-500 mr-1"></span>S&P 500</span>}
        <span className="ml-auto">{points[0]?.date} → {points[points.length-1]?.date}</span>
      </div>
    </div>
  )
}
