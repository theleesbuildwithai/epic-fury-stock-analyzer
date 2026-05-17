import { useState, useEffect, useCallback } from 'react'

export default function BacktestDashboard() {
  const [days, setDays] = useState(90)
  const [topN, setTopN] = useState(10)
  const [stopPct, setStopPct] = useState(0.04)
  const [takePct, setTakePct] = useState(0.10)
  const [holdDays, setHoldDays] = useState(5)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const runBacktest = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({
        days, top_n: topN,
        stop_pct: stopPct, take_pct: takePct, hold_days: holdDays,
      })
      const res = await fetch(`/api/backtest/detail?${params}`)
      const json = await res.json()
      if (!json.ok) throw new Error(json.reason || 'Backtest failed')
      setData(json)
    } catch (e) {
      setError(e.message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [days, topN, stopPct, takePct, holdDays])

  useEffect(() => { runBacktest() }, [])  // initial load

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <h1 className="text-3xl font-bold text-white mb-2">Backtest Lab</h1>
      <p className="text-gray-400 mb-6">Replays historical data with chosen parameters — see what the strategy WOULD have done.</p>

      {/* Controls */}
      <div className="bg-slate-800 rounded-lg p-4 mb-6 grid grid-cols-2 md:grid-cols-6 gap-3">
        <Field label="Days back" value={days} onChange={setDays} type="number" />
        <Field label="Top N" value={topN} onChange={setTopN} type="number" />
        <Field label="Stop %" value={stopPct} onChange={setStopPct} type="number" step="0.01" />
        <Field label="Take %" value={takePct} onChange={setTakePct} type="number" step="0.01" />
        <Field label="Hold days" value={holdDays} onChange={setHoldDays} type="number" />
        <button onClick={runBacktest} disabled={loading}
          className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white font-semibold rounded px-4 py-2">
          {loading ? 'Running…' : 'Run Backtest'}
        </button>
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
        <div className="text-gray-400 p-6 bg-slate-800 rounded-lg">Adjust parameters above and click "Run Backtest".</div>
      )}
    </div>
  )
}

function Field({ label, value, onChange, type = 'text', step }) {
  return (
    <div>
      <label className="block text-xs text-gray-400 mb-1">{label}</label>
      <input
        type={type}
        step={step}
        value={value}
        onChange={e => onChange(type === 'number' ? Number(e.target.value) : e.target.value)}
        className="w-full bg-slate-900 text-white rounded px-2 py-1.5 border border-slate-700 focus:border-blue-500 outline-none"
      />
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
