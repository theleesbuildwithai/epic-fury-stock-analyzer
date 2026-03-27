import { useState, useEffect } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, BarChart, Bar, Cell
} from 'recharts'

const TABS = ['Quant Picks', 'Paper Portfolio', 'System Intelligence']

export default function QuantDashboard() {
  const [activeTab, setActiveTab] = useState(0)
  const [quantPicks, setQuantPicks] = useState(null)
  const [portfolio, setPortfolio] = useState(null)
  const [performance, setPerformance] = useState(null)
  const [intelligence, setIntelligence] = useState(null)
  const [loading, setLoading] = useState({})
  const [rebalancing, setRebalancing] = useState(false)
  const [backtesting, setBacktesting] = useState(false)
  const [rebalanceResult, setRebalanceResult] = useState(null)
  const [backtestResult, setBacktestResult] = useState(null)

  useEffect(() => {
    fetchQuantPicks()
    fetchPortfolio()
    fetchIntelligence()
  }, [])

  const fetchQuantPicks = async () => {
    setLoading(p => ({ ...p, picks: true }))
    try {
      const res = await fetch('/api/quant-picks')
      const data = await res.json()
      setQuantPicks(data)
    } catch { }
    setLoading(p => ({ ...p, picks: false }))
  }

  const fetchPortfolio = async () => {
    setLoading(p => ({ ...p, portfolio: true }))
    try {
      const [pRes, perfRes] = await Promise.all([
        fetch('/api/paper-portfolio'),
        fetch('/api/paper-performance'),
      ])
      setPortfolio(await pRes.json())
      setPerformance(await perfRes.json())
    } catch { }
    setLoading(p => ({ ...p, portfolio: false }))
  }

  const fetchIntelligence = async () => {
    setLoading(p => ({ ...p, intel: true }))
    try {
      const res = await fetch('/api/system-intelligence')
      setIntelligence(await res.json())
    } catch { }
    setLoading(p => ({ ...p, intel: false }))
  }

  const handleRebalance = async () => {
    setRebalancing(true)
    setRebalanceResult(null)
    try {
      const res = await fetch('/api/paper-trade/rebalance', { method: 'POST' })
      const data = await res.json()
      setRebalanceResult(data)
      fetchPortfolio()
      fetchIntelligence()
    } catch { }
    setRebalancing(false)
  }

  const handleBacktest = async () => {
    setBacktesting(true)
    setBacktestResult(null)
    try {
      const res = await fetch('/api/paper-trade/backtest', { method: 'POST' })
      const data = await res.json()
      setBacktestResult(data)
      fetchIntelligence()
    } catch { }
    setBacktesting(false)
  }

  const RegimeBadge = ({ regime }) => {
    const colors = {
      BULL: 'bg-green-500/20 text-green-400 border-green-500/30',
      BEAR: 'bg-red-500/20 text-red-400 border-red-500/30',
      SIDEWAYS: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
    }
    return (
      <span className={`px-3 py-1 rounded-full text-sm font-bold border ${colors[regime] || colors.SIDEWAYS}`}>
        {regime} Market
      </span>
    )
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      <div className="mb-6">
        <h1 className="text-3xl font-black text-white tracking-tight">
          Quant Hedge Fund
        </h1>
        <p className="text-neutral-500 mt-1">
          Quantitative analysis, paper trading, and self-learning system
        </p>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 mb-6 bg-neutral-900 rounded-xl p-1 w-fit">
        {TABS.map((tab, i) => (
          <button
            key={tab}
            onClick={() => setActiveTab(i)}
            className={`px-5 py-2 rounded-lg text-sm font-medium transition-all ${
              activeTab === i
                ? 'bg-white text-black shadow-lg'
                : 'text-neutral-400 hover:text-white'
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {activeTab === 0 && (
        <QuantPicksTab picks={quantPicks} loading={loading.picks} RegimeBadge={RegimeBadge} />
      )}
      {activeTab === 1 && (
        <PaperPortfolioTab
          portfolio={portfolio} performance={performance}
          loading={loading.portfolio}
          onRebalance={handleRebalance} rebalancing={rebalancing}
          rebalanceResult={rebalanceResult}
          onBacktest={handleBacktest} backtesting={backtesting}
          backtestResult={backtestResult}
        />
      )}
      {activeTab === 2 && (
        <IntelligenceTab intelligence={intelligence} loading={loading.intel} />
      )}
    </div>
  )
}

// ============================================================
// TAB 1: QUANT PICKS
// ============================================================
function QuantPicksTab({ picks, loading, RegimeBadge }) {
  if (loading) return <LoadingSpinner text="Analyzing 100+ stocks..." />
  if (!picks) return <EmptyState text="Failed to load quant picks" />

  const regime = picks.regime || {}
  const macro = picks.macro || {}

  return (
    <div className="space-y-6">
      {/* Regime & Macro Panel */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <div className="flex flex-wrap items-center gap-4 mb-4">
          <RegimeBadge regime={regime.regime || 'SIDEWAYS'} />
          <span className="text-neutral-500 text-sm">
            Confidence: {regime.confidence}% | VIX: {regime.vix_level}
          </span>
          <span className="text-neutral-600 text-xs">
            {picks.total_analyzed} stocks analyzed in {picks.computation_time_seconds}s
          </span>
        </div>

        {regime.details && (
          <div className="space-y-1 mb-4">
            {regime.details.map((d, i) => (
              <p key={i} className="text-neutral-400 text-sm">{d}</p>
            ))}
          </div>
        )}

        {/* Macro indicators */}
        {macro.treasury_10y && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
            <MacroCard label="10Y Treasury" value={`${macro.treasury_10y?.value}%`}
              signal={macro.treasury_10y?.signal} />
            <MacroCard label="Crude Oil" value={`$${macro.crude_oil?.value}`}
              signal={macro.crude_oil?.signal} />
            <MacroCard label="Gold" value={`$${macro.gold?.value}`}
              signal={macro.gold?.signal} />
            <MacroCard label="VIX" value={macro.vix?.value}
              signal={macro.vix?.signal} />
          </div>
        )}
      </div>

      {/* Long Picks */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h2 className="text-xl font-bold text-green-400 mb-4">
          LONG Picks ({picks.long_picks?.length || 0})
        </h2>
        <div className="overflow-x-auto">
          <PicksTable picks={picks.long_picks || []} direction="LONG" />
        </div>
      </div>

      {/* Short Picks */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h2 className="text-xl font-bold text-red-400 mb-4">
          SHORT Picks ({picks.short_picks?.length || 0})
        </h2>
        <div className="overflow-x-auto">
          <PicksTable picks={picks.short_picks || []} direction="SHORT" />
        </div>
      </div>

      <p className="text-neutral-600 text-xs text-center italic">
        {picks.disclaimer}
      </p>
    </div>
  )
}

function MacroCard({ label, value, signal }) {
  const colors = {
    rising: 'text-green-400',
    falling: 'text-red-400',
    flat: 'text-neutral-400',
  }
  return (
    <div className="bg-neutral-900 rounded-lg p-3">
      <div className="text-neutral-500 text-xs">{label}</div>
      <div className={`text-lg font-bold font-mono ${colors[signal] || 'text-white'}`}>
        {value}
      </div>
      <div className="text-neutral-600 text-xs capitalize">{signal}</div>
    </div>
  )
}

function PicksTable({ picks, direction }) {
  if (!picks.length) return <p className="text-neutral-500 text-sm">No picks in this direction</p>

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-neutral-500 text-xs border-b border-neutral-800">
          <th className="text-left py-2 px-2">#</th>
          <th className="text-left py-2 px-2">Symbol</th>
          <th className="text-right py-2 px-2">Price</th>
          <th className="text-right py-2 px-2">Score</th>
          <th className="text-right py-2 px-2">Confidence</th>
          <th className="text-right py-2 px-2">RSI(14)</th>
          <th className="text-right py-2 px-2">Vol</th>
          <th className="text-left py-2 px-2">Sector</th>
          <th className="text-left py-2 px-2">Top Reason</th>
        </tr>
      </thead>
      <tbody>
        {picks.map((p, i) => (
          <tr key={p.symbol} className="border-b border-neutral-800/50 hover:bg-neutral-900/50">
            <td className="py-2 px-2 text-neutral-600">{p.rank || i + 1}</td>
            <td className="py-2 px-2 font-bold text-white">{p.symbol}</td>
            <td className="py-2 px-2 text-right font-mono text-white">${p.price}</td>
            <td className={`py-2 px-2 text-right font-mono font-bold ${
              p.composite_score >= 0 ? 'text-green-400' : 'text-red-400'
            }`}>
              {p.composite_score > 0 ? '+' : ''}{p.composite_score}
            </td>
            <td className="py-2 px-2 text-right">
              <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                p.confidence >= 70 ? 'bg-green-500/20 text-green-400' :
                p.confidence >= 55 ? 'bg-yellow-500/20 text-yellow-400' :
                'bg-neutral-500/20 text-neutral-400'
              }`}>
                {p.confidence}%
              </span>
            </td>
            <td className="py-2 px-2 text-right font-mono text-neutral-400">{p.rsi14}</td>
            <td className="py-2 px-2 text-right font-mono text-neutral-400">{p.volatility_60d}%</td>
            <td className="py-2 px-2 text-neutral-500 text-xs">{p.sector}</td>
            <td className="py-2 px-2 text-neutral-400 text-xs">{p.reasons?.[0] || ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ============================================================
// TAB 2: PAPER PORTFOLIO
// ============================================================
function PaperPortfolioTab({ portfolio, performance, loading, onRebalance, rebalancing,
  rebalanceResult, onBacktest, backtesting, backtestResult }) {
  if (loading) return <LoadingSpinner text="Loading portfolio..." />

  return (
    <div className="space-y-6">
      {/* Actions */}
      <div className="flex gap-3">
        <button
          onClick={onRebalance}
          disabled={rebalancing}
          className="px-5 py-2 bg-white text-black rounded-lg font-bold text-sm hover:bg-neutral-200 transition-all disabled:opacity-50"
        >
          {rebalancing ? 'Executing Trades...' : 'Rebalance Portfolio'}
        </button>
        <button
          onClick={onBacktest}
          disabled={backtesting}
          className="px-5 py-2 bg-neutral-800 text-white rounded-lg font-bold text-sm hover:bg-neutral-700 transition-all disabled:opacity-50"
        >
          {backtesting ? 'Running Backtest...' : 'Run Backtest (500 trades)'}
        </button>
      </div>

      {/* Rebalance Result */}
      {rebalanceResult && (
        <div className="bg-black border border-neutral-700 rounded-xl p-4">
          <h3 className="text-white font-bold mb-2">Trade Execution Results</h3>
          <div className="grid grid-cols-3 gap-3 text-sm">
            <div className="bg-green-500/10 rounded-lg p-3">
              <div className="text-green-400 font-bold text-lg">{rebalanceResult.opened?.length || 0}</div>
              <div className="text-neutral-500">Opened</div>
            </div>
            <div className="bg-red-500/10 rounded-lg p-3">
              <div className="text-red-400 font-bold text-lg">{rebalanceResult.closed?.length || 0}</div>
              <div className="text-neutral-500">Closed</div>
            </div>
            <div className="bg-neutral-500/10 rounded-lg p-3">
              <div className="text-neutral-400 font-bold text-lg">{rebalanceResult.skipped?.length || 0}</div>
              <div className="text-neutral-500">Skipped</div>
            </div>
          </div>
        </div>
      )}

      {/* Backtest Result */}
      {backtestResult && (
        <div className="bg-black border border-neutral-700 rounded-xl p-4">
          <h3 className="text-white font-bold mb-2">Backtest Results</h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
            <StatCard label="Total Trades" value={backtestResult.total_trades} />
            <StatCard label="Win Rate" value={`${backtestResult.win_rate}%`}
              color={backtestResult.win_rate > 50 ? 'green' : 'red'} />
            <StatCard label="Avg Return" value={`${backtestResult.avg_return_pct}%`}
              color={backtestResult.avg_return_pct > 0 ? 'green' : 'red'} />
            <StatCard label="Sharpe" value={backtestResult.sharpe_ratio}
              color={backtestResult.sharpe_ratio > 1 ? 'green' : 'yellow'} />
          </div>
          {backtestResult.factor_results && (
            <div className="mt-4">
              <h4 className="text-neutral-400 text-xs font-bold uppercase mb-2">Strategy Breakdown</h4>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {Object.entries(backtestResult.factor_results).map(([name, stats]) => (
                  <div key={name} className="bg-neutral-900 rounded-lg p-3">
                    <div className="text-white font-bold text-sm">{name.replace(/_/g, ' ')}</div>
                    <div className="text-neutral-400 text-xs mt-1">
                      {stats.total_trades} trades | {stats.win_rate}% win | {stats.avg_return}% avg
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Portfolio Overview */}
      {portfolio && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h2 className="text-xl font-bold text-white mb-4">Portfolio Overview</h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
            <StatCard label="Total Value" value={`$${(portfolio.total_value || 0).toLocaleString()}`} />
            <StatCard label="Cash" value={`$${(portfolio.cash || 0).toLocaleString()}`} />
            <StatCard label="Return" value={`${portfolio.total_return_pct || 0}%`}
              color={(portfolio.total_return_pct || 0) >= 0 ? 'green' : 'red'} />
            <StatCard label="Positions" value={`${portfolio.num_positions || 0} / ${portfolio.max_positions}`} />
          </div>

          {/* Win/Loss Stats */}
          {portfolio.stats && portfolio.stats.total_trades > 0 && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
              <StatCard label="Total Trades" value={portfolio.stats.total_trades} />
              <StatCard label="Win Rate" value={`${portfolio.stats.win_rate}%`}
                color={portfolio.stats.win_rate > 50 ? 'green' : 'red'} />
              <StatCard label="Profit Factor" value={portfolio.stats.profit_factor}
                color={portfolio.stats.profit_factor > 1 ? 'green' : 'red'} />
              <StatCard label="Avg Win" value={`${portfolio.stats.avg_win_pct}%`} color="green" />
            </div>
          )}

          {/* Open Positions */}
          {portfolio.positions?.length > 0 && (
            <div className="mt-4">
              <h3 className="text-neutral-400 text-xs font-bold uppercase mb-2">Open Positions</h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-neutral-500 text-xs border-b border-neutral-800">
                      <th className="text-left py-2 px-2">Ticker</th>
                      <th className="text-left py-2 px-2">Dir</th>
                      <th className="text-right py-2 px-2">Entry</th>
                      <th className="text-right py-2 px-2">Current</th>
                      <th className="text-right py-2 px-2">P&L</th>
                      <th className="text-right py-2 px-2">Days</th>
                      <th className="text-left py-2 px-2">Sector</th>
                    </tr>
                  </thead>
                  <tbody>
                    {portfolio.positions.map(p => (
                      <tr key={p.trade_id} className="border-b border-neutral-800/50">
                        <td className="py-2 px-2 font-bold text-white">{p.ticker}</td>
                        <td className={`py-2 px-2 text-xs font-bold ${
                          p.direction === 'long' ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {p.direction.toUpperCase()}
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-neutral-400">${p.entry_price}</td>
                        <td className="py-2 px-2 text-right font-mono text-white">${p.current_price}</td>
                        <td className={`py-2 px-2 text-right font-mono font-bold ${
                          p.unrealized_pct >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {p.unrealized_pct >= 0 ? '+' : ''}{p.unrealized_pct}%
                        </td>
                        <td className="py-2 px-2 text-right text-neutral-400">{p.days_held}d</td>
                        <td className="py-2 px-2 text-neutral-500 text-xs">{p.sector}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Equity Curve */}
      {performance?.equity_curve?.length > 1 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h2 className="text-xl font-bold text-white mb-4">Equity Curve</h2>
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={performance.equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                <XAxis dataKey="date" tick={{ fill: '#737373', fontSize: 10 }} />
                <YAxis tick={{ fill: '#737373', fontSize: 10 }} tickFormatter={v => `${v}%`} />
                <Tooltip
                  contentStyle={{ background: '#171717', border: '1px solid #404040', borderRadius: 8 }}
                  labelStyle={{ color: '#a3a3a3' }}
                />
                <Line dataKey="portfolio_return" stroke="#3b82f6" strokeWidth={2}
                  dot={false} name="Portfolio" />
                <Line dataKey="sp500_return" stroke="#737373" strokeWidth={1}
                  strokeDasharray="4 2" dot={false} name="S&P 500" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  )
}

// ============================================================
// TAB 3: SYSTEM INTELLIGENCE
// ============================================================
function IntelligenceTab({ intelligence, loading }) {
  if (loading) return <LoadingSpinner text="Analyzing system performance..." />
  if (!intelligence) return <EmptyState text="No intelligence data yet" />

  const weights = intelligence.current_weights || {}
  const factorPerf = intelligence.factor_performance || {}

  const weightData = Object.entries(weights).map(([name, weight]) => ({
    name: name.replace('_', ' '),
    weight: Math.round(weight * 100),
    win_rate: factorPerf[name]?.win_rate || 0,
    sharpe: factorPerf[name]?.sharpe || 0,
  }))

  return (
    <div className="space-y-6">
      {/* System Status */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <div className="flex items-center gap-3 mb-4">
          <h2 className="text-xl font-bold text-white">System Status</h2>
          <span className={`px-3 py-1 rounded-full text-xs font-bold ${
            intelligence.system_status === 'confident' ? 'bg-green-500/20 text-green-400' :
            intelligence.system_status === 'learning' ? 'bg-blue-500/20 text-blue-400' :
            intelligence.system_status === 'adapting' ? 'bg-yellow-500/20 text-yellow-400' :
            'bg-neutral-500/20 text-neutral-400'
          }`}>
            {intelligence.system_status?.toUpperCase()}
          </span>
          <span className="text-neutral-500 text-sm">
            {intelligence.total_closed_trades || 0} trades analyzed
          </span>
        </div>

        {intelligence.insights?.map((insight, i) => (
          <p key={i} className="text-neutral-400 text-sm mb-1">{insight}</p>
        ))}
      </div>

      {/* Strengths & Weaknesses */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-black border border-green-500/30 rounded-xl p-6">
          <h3 className="text-green-400 font-bold mb-3">Strengths</h3>
          {intelligence.strengths?.length > 0 ? (
            intelligence.strengths.map((s, i) => (
              <p key={i} className="text-neutral-300 text-sm mb-2">+ {s}</p>
            ))
          ) : (
            <p className="text-neutral-500 text-sm">Still learning...</p>
          )}
        </div>
        <div className="bg-black border border-red-500/30 rounded-xl p-6">
          <h3 className="text-red-400 font-bold mb-3">Weaknesses</h3>
          {intelligence.weaknesses?.length > 0 ? (
            intelligence.weaknesses.map((w, i) => (
              <p key={i} className="text-neutral-300 text-sm mb-2">- {w}</p>
            ))
          ) : (
            <p className="text-neutral-500 text-sm">No weaknesses identified yet</p>
          )}
        </div>
      </div>

      {/* Factor Weights Chart */}
      {weightData.length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Factor Weights (Adaptive)</h3>
          <div className="h-[200px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={weightData} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                <XAxis type="number" tick={{ fill: '#737373', fontSize: 10 }}
                  tickFormatter={v => `${v}%`} />
                <YAxis dataKey="name" type="category" width={80}
                  tick={{ fill: '#a3a3a3', fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: '#171717', border: '1px solid #404040', borderRadius: 8 }}
                  formatter={(v, name) => [`${v}%`, name]}
                />
                <Bar dataKey="weight" radius={[0, 4, 4, 0]}>
                  {weightData.map((entry, i) => (
                    <Cell key={i} fill={
                      entry.weight > 20 ? '#3b82f6' :
                      entry.weight > 15 ? '#6366f1' : '#737373'
                    } />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Sector Performance */}
      {intelligence.sector_performance?.sectors && Object.keys(intelligence.sector_performance.sectors).length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Performance by Sector</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {Object.entries(intelligence.sector_performance.sectors).map(([sector, stats]) => (
              <div key={sector} className="bg-neutral-900 rounded-lg p-3">
                <div className="text-white text-sm font-bold">{sector}</div>
                <div className={`text-lg font-bold font-mono ${
                  stats.win_rate > 55 ? 'text-green-400' : stats.win_rate < 45 ? 'text-red-400' : 'text-neutral-400'
                }`}>
                  {stats.win_rate}%
                </div>
                <div className="text-neutral-500 text-xs">{stats.total_trades} trades</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Confidence Calibration */}
      {intelligence.confidence_calibration && Object.keys(intelligence.confidence_calibration).length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Confidence Calibration</h3>
          <p className="text-neutral-500 text-sm mb-3">
            How well our predicted confidence matches actual outcomes
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            {Object.entries(intelligence.confidence_calibration).map(([bucket, data]) => (
              <div key={bucket} className="bg-neutral-900 rounded-lg p-3 text-center">
                <div className="text-neutral-400 text-xs">{bucket}% predicted</div>
                <div className={`text-xl font-bold font-mono ${
                  data.actual_win_rate > data.avg_predicted_confidence * 0.9
                    ? 'text-green-400' : 'text-red-400'
                }`}>
                  {data.actual_win_rate}%
                </div>
                <div className="text-neutral-600 text-xs">actual ({data.total_trades} trades)</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ============================================================
// SHARED COMPONENTS
// ============================================================
function StatCard({ label, value, color }) {
  const textColor = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' :
    color === 'yellow' ? 'text-yellow-400' : 'text-white'
  return (
    <div className="bg-neutral-900 rounded-lg p-3">
      <div className="text-neutral-500 text-xs">{label}</div>
      <div className={`text-lg font-bold font-mono ${textColor}`}>{value}</div>
    </div>
  )
}

function LoadingSpinner({ text }) {
  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
      <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin mb-3"></div>
      <p className="text-neutral-500">{text}</p>
    </div>
  )
}

function EmptyState({ text }) {
  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-12 text-center">
      <p className="text-neutral-500">{text}</p>
    </div>
  )
}
