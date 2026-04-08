import { useState, useEffect } from 'react'
import {
  XAxis, YAxis, CartesianGrid, Tooltip,
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
  const [autoStatus, setAutoStatus] = useState(null)
  const [queuedTrades, setQueuedTrades] = useState(null)

  useEffect(() => {
    fetchQuantPicks()
    fetchPortfolio()
    fetchIntelligence()
    fetchAutoStatus()
    fetchQueuedTrades()
    // Refresh auto-trading status every 30s
    const interval = setInterval(() => { fetchAutoStatus(); fetchQueuedTrades() }, 30000)
    return () => clearInterval(interval)
  }, [])

  const fetchQuantPicks = async () => {
    setLoading(p => ({ ...p, picks: true }))
    try {
      const controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), 120000)
      const res = await fetch('/api/quant-picks', { signal: controller.signal })
      clearTimeout(timeout)
      if (res.ok) {
        const data = await res.json()
        setQuantPicks(data)
      }
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
      if (pRes.ok) setPortfolio(await pRes.json())
      if (perfRes.ok) setPerformance(await perfRes.json())
    } catch (e) { console.debug('Portfolio fetch error:', e) }
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

  const fetchAutoStatus = async () => {
    try {
      const res = await fetch('/api/auto-trading-status')
      setAutoStatus(await res.json())
    } catch { }
  }

  const fetchQueuedTrades = async () => {
    try {
      const res = await fetch('/api/queued-trades')
      setQueuedTrades(await res.json())
    } catch { }
  }

  const RegimeBadge = ({ regime }) => {
    const colors = {
      BULL: 'bg-green-500/20 text-green-400 border-green-500/30',
      BEAR: 'bg-red-500/20 text-red-400 border-red-500/30',
      SIDEWAYS: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
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
          14-factor quant engine • Autonomous trading • Self-learning system
        </p>

        {/* Portfolio Value — inline with header */}
        {portfolio && (
          <div className="mt-3 flex items-end justify-between flex-wrap gap-3">
            <div className="flex gap-4 text-xs text-neutral-500">
              <span>14 Quant Factors</span>
              <span>500+ Stocks Analyzed</span>
              <span>Event-Driven Trading</span>
              <span>Self-Learning Weekly</span>
            </div>
            <div className="text-right">
              <p className="text-3xl font-black text-white">${(portfolio.total_value || 109000).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</p>
              <p className={`text-sm font-bold ${(portfolio.total_return_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {(portfolio.total_return_pct || 0) >= 0 ? '▲ ' : '▼ '}{Math.abs(portfolio.total_return_pct || 0).toFixed(2)}% all-time
              </p>
            </div>
          </div>
        )}
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
          autoStatus={autoStatus}
          queuedTrades={queuedTrades}
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
  if (loading) return <LoadingSpinner text="Analyzing 500+ stocks across 14 quant factors... this takes ~90 seconds" />
  if (!picks) return <EmptyState text="Quant picks are being computed. Refresh in 60 seconds." />

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
                p.confidence >= 55 ? 'bg-neutral-500/20 text-neutral-400' :
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
function PaperPortfolioTab({ portfolio, performance, loading, autoStatus, queuedTrades }) {
  if (loading) return <LoadingSpinner text="Loading portfolio..." />

  const exp = portfolio?.exposure || {}
  const perf = performance?.overall || {}
  // Stats come from performance.overall, not portfolio.stats
  const stats = {
    total_trades: perf.total_trades || 0,
    total_open: portfolio?.num_positions || 0,
    win_rate: perf.win_rate || 0,
    profit_factor: perf.profit_factor || 0,
    avg_win_pct: perf.avg_win || 0,
    avg_loss_pct: perf.avg_loss || 0,
    trades_per_day: perf.trades_per_day || 0,
  }
  return (
    <div className="space-y-6">

      {/* ====== FUND PERFORMANCE HEADER ====== */}
      {portfolio && (
        <div className="bg-black border-2 border-green-500/40 rounded-xl p-6 relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-green-500 via-green-400 to-green-600"></div>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-2xl font-black text-white">Epic Fury Fund</h2>
              <p className="text-neutral-500 text-xs mt-1">Since March 30, 2026 | Initial Capital: $100,000</p>
            </div>
            <div className="text-right">
              <p className="text-4xl font-black text-white tracking-tight">
                ${(portfolio.total_value || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
              </p>
              <p className={`text-lg font-bold ${(portfolio.total_return_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {(portfolio.total_return_pct || 0) >= 0 ? '+' : ''}{(portfolio.total_return_pct || 0).toFixed(2)}% all-time return
              </p>
            </div>
          </div>

          {/* Key metrics row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-5">
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Cash</div>
              <div className="text-white font-bold font-mono text-lg">
                ${(portfolio.cash || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
              </div>
              <div className="text-neutral-600 text-xs">{exp.cash_pct || 0}% of portfolio</div>
            </div>
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Positions Value</div>
              <div className="text-white font-bold font-mono text-lg">
                ${(portfolio.positions_value || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
              </div>
              <div className="text-neutral-600 text-xs">{portfolio.num_positions || 0} open positions</div>
            </div>
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-green-500/20">
              <div className="text-neutral-500 text-xs">Long Positions</div>
              <div className="text-green-400 font-bold font-mono text-lg">{portfolio.num_longs || 0}</div>
              <div className="text-neutral-600 text-xs">{exp.long_pct || 0}% exposure</div>
            </div>
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-red-500/20">
              <div className="text-neutral-500 text-xs">Short Positions</div>
              <div className="text-red-400 font-bold font-mono text-lg">{portfolio.num_shorts || 0}</div>
              <div className="text-neutral-600 text-xs">{exp.short_pct || 0}% exposure</div>
            </div>
          </div>
        </div>
      )}

      {/* ====== ADVANCED ANALYTICS PANEL ====== */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h3 className="text-white font-bold text-lg mb-4">Advanced Analytics</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3">
          {/* Sharpe Ratio */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Sharpe Ratio</div>
            <div className={`text-xl font-black font-mono ${
              (perf.sharpe_ratio || 0) >= 1 ? 'text-green-400' :
              (perf.sharpe_ratio || 0) >= 0 ? 'text-neutral-300' : 'text-red-400'
            }`}>
              {(perf.sharpe_ratio || 0).toFixed(2)}
            </div>
            <div className="text-neutral-600 text-xs">
              {(perf.sharpe_ratio || 0) >= 2 ? 'Excellent' :
               (perf.sharpe_ratio || 0) >= 1 ? 'Good' :
               (perf.sharpe_ratio || 0) >= 0 ? 'Fair' : 'Poor'}
            </div>
          </div>

          {/* Sortino Ratio */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Sortino Ratio</div>
            <div className={`text-xl font-black font-mono ${
              (perf.sortino_ratio || 0) >= 1.5 ? 'text-green-400' :
              (perf.sortino_ratio || 0) >= 0 ? 'text-neutral-300' : 'text-red-400'
            }`}>
              {(perf.sortino_ratio || 0) >= 99 ? '99+' : (perf.sortino_ratio || 0).toFixed(2)}
            </div>
            <div className="text-neutral-600 text-xs">Downside risk-adjusted</div>
          </div>

          {/* Gross Exposure */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Gross Exposure</div>
            <div className="text-white font-black font-mono text-xl">
              {exp.gross_exposure_pct || 0}%
            </div>
            <div className="text-neutral-600 text-xs">
              ${((exp.gross_exposure || 0) / 1000).toFixed(1)}K deployed
            </div>
          </div>

          {/* Net Exposure */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Net Exposure</div>
            <div className={`text-xl font-black font-mono ${
              (exp.net_exposure_pct || 0) > 0 ? 'text-green-400' :
              (exp.net_exposure_pct || 0) < 0 ? 'text-red-400' : 'text-neutral-300'
            }`}>
              {(exp.net_exposure_pct || 0) > 0 ? '+' : ''}{exp.net_exposure_pct || 0}%
            </div>
            <div className="text-neutral-600 text-xs">
              {(exp.net_exposure_pct || 0) > 50 ? 'Net Long' :
               (exp.net_exposure_pct || 0) < -10 ? 'Net Short' : 'Balanced'}
            </div>
          </div>

          {/* Trades Per Day */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Trades / Day</div>
            <div className="text-white font-black font-mono text-xl">
              {stats.trades_per_day || perf.trades_per_day || 0}
            </div>
            <div className="text-neutral-600 text-xs">
              {perf.trading_days_active || 0} trading days
            </div>
          </div>

          {/* Total Trades */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Total Trades</div>
            <div className="text-white font-black font-mono text-xl">
              {(stats.total_trades || 0) + (stats.total_open || 0)}
            </div>
            <div className="text-neutral-600 text-xs">
              {stats.total_trades || 0} closed, {stats.total_open || 0} open
            </div>
          </div>

          {/* Avg Hold Days */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Avg Hold</div>
            <div className="text-white font-black font-mono text-xl">
              {perf.avg_hold_days || 0}d
            </div>
            <div className="text-neutral-600 text-xs">Per trade</div>
          </div>

          {/* Max Drawdown */}
          <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <div className="text-neutral-500 text-xs">Max Drawdown</div>
            <div className="text-red-400 font-black font-mono text-xl">
              {performance?.max_drawdown_pct != null ? `${performance.max_drawdown_pct}%` : 'N/A'}
            </div>
            <div className="text-neutral-600 text-xs">Peak to trough</div>
          </div>
        </div>
      </div>

      {/* ====== BENCHMARKING vs S&P 500 ====== */}
      {performance?.benchmark && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold text-lg mb-4">Benchmarking vs S&P 500</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <div className="bg-neutral-900 rounded-lg p-3 border border-green-500/20">
              <div className="text-neutral-500 text-xs">Fund Return</div>
              <div className={`text-xl font-black font-mono ${
                (performance.benchmark.fund_return_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'
              }`}>
                {(performance.benchmark.fund_return_pct || 0) >= 0 ? '+' : ''}{(performance.benchmark.fund_return_pct || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">Since inception</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">S&P 500 Return</div>
              <div className={`text-xl font-black font-mono ${
                (performance.benchmark.sp500_return_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'
              }`}>
                {(performance.benchmark.sp500_return_pct || 0) >= 0 ? '+' : ''}{(performance.benchmark.sp500_return_pct || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">Same period</div>
            </div>
            <div className={`bg-neutral-900 rounded-lg p-3 border ${
              (performance.benchmark.alpha_pct || 0) > 0 ? 'border-green-500/30' : 'border-red-500/30'
            }`}>
              <div className="text-neutral-500 text-xs">Alpha</div>
              <div className={`text-xl font-black font-mono ${
                (performance.benchmark.alpha_pct || 0) > 0 ? 'text-green-400' : 'text-red-400'
              }`}>
                {(performance.benchmark.alpha_pct || 0) > 0 ? '+' : ''}{(performance.benchmark.alpha_pct || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">
                {(performance.benchmark.alpha_pct || 0) > 0 ? 'Beating market' : 'Trailing market'}
              </div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Fund Sharpe</div>
              <div className={`text-xl font-black font-mono ${
                (performance.benchmark.fund_sharpe || 0) >= 1 ? 'text-green-400' : 'text-neutral-300'
              }`}>
                {(performance.benchmark.fund_sharpe || 0).toFixed(2)}
              </div>
              <div className="text-neutral-600 text-xs">Risk-adjusted</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">S&P Sharpe</div>
              <div className="text-neutral-300 font-black font-mono text-xl">
                {(performance.benchmark.sp500_sharpe || 0).toFixed(2)}
              </div>
              <div className="text-neutral-600 text-xs">Benchmark</div>
            </div>
            <div className={`bg-neutral-900 rounded-lg p-3 border ${
              (performance.benchmark.sharpe_edge || 0) > 0 ? 'border-green-500/30' : 'border-red-500/30'
            }`}>
              <div className="text-neutral-500 text-xs">Sharpe Edge</div>
              <div className={`text-xl font-black font-mono ${
                (performance.benchmark.sharpe_edge || 0) > 0 ? 'text-green-400' : 'text-red-400'
              }`}>
                {(performance.benchmark.sharpe_edge || 0) > 0 ? '+' : ''}{(performance.benchmark.sharpe_edge || 0).toFixed(2)}
              </div>
              <div className="text-neutral-600 text-xs">vs S&P 500</div>
            </div>
          </div>
        </div>
      )}

      {/* ====== TRADE STATISTICS ====== */}
      {stats.total_trades > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold text-lg mb-4">Trade Statistics</h3>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Win Rate</div>
              <div className={`text-xl font-black font-mono ${
                (stats.win_rate || 0) > 50 ? 'text-green-400' : 'text-red-400'
              }`}>
                {stats.win_rate || 0}%
              </div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Profit Factor</div>
              <div className={`text-xl font-black font-mono ${
                (stats.profit_factor || 0) > 1 ? 'text-green-400' : 'text-red-400'
              }`}>
                {stats.profit_factor || 0}
              </div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Avg Win</div>
              <div className="text-green-400 font-bold font-mono text-xl">+{stats.avg_win_pct || 0}%</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Avg Loss</div>
              <div className="text-red-400 font-bold font-mono text-xl">{stats.avg_loss_pct || 0}%</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Max Drawdown</div>
              <div className="text-red-400 font-bold font-mono text-xl">
                {performance?.max_drawdown_pct != null ? `${performance.max_drawdown_pct}%` : 'N/A'}
              </div>
            </div>
          </div>

          {/* Long vs Short breakdown */}
          {(performance?.long_stats || performance?.short_stats) && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4">
              {performance.long_stats && (
                <div className="bg-green-500/5 border border-green-500/20 rounded-lg p-4">
                  <div className="text-green-400 font-bold text-sm mb-2">Long Performance</div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Trades</span>
                    <span className="text-white font-mono">{performance.long_stats.total}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Win Rate</span>
                    <span className={`font-mono font-bold ${performance.long_stats.win_rate > 50 ? 'text-green-400' : 'text-red-400'}`}>
                      {performance.long_stats.win_rate}%
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Avg Return</span>
                    <span className={`font-mono font-bold ${performance.long_stats.avg_return >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {performance.long_stats.avg_return >= 0 ? '+' : ''}{performance.long_stats.avg_return}%
                    </span>
                  </div>
                </div>
              )}
              {performance.short_stats && (
                <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-4">
                  <div className="text-red-400 font-bold text-sm mb-2">Short Performance</div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Trades</span>
                    <span className="text-white font-mono">{performance.short_stats.total}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Win Rate</span>
                    <span className={`font-mono font-bold ${performance.short_stats.win_rate > 50 ? 'text-green-400' : 'text-red-400'}`}>
                      {performance.short_stats.win_rate}%
                    </span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-neutral-400">Avg Return</span>
                    <span className={`font-mono font-bold ${performance.short_stats.avg_return >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {performance.short_stats.avg_return >= 0 ? '+' : ''}{performance.short_stats.avg_return}%
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Autonomous Trading Status */}
      {autoStatus && (
        <div className="bg-black border border-green-500/30 rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className={`w-3 h-3 rounded-full ${
              autoStatus.status === 'running' || autoStatus.status === 'idle' ? 'bg-green-500 animate-pulse' :
              autoStatus.status === 'trading' ? 'bg-green-500 animate-pulse' :
              'bg-red-500'
            }`} />
            <h3 className="text-white font-bold text-lg">Autonomous Trading</h3>
            <span className={`px-3 py-1 rounded-full text-xs font-bold ${
              autoStatus.status === 'trading' ? 'bg-green-500/20 text-green-400 border border-green-500/30' :
              autoStatus.status === 'running' || autoStatus.status === 'idle' ? 'bg-green-500/20 text-green-400 border border-green-500/30' :
              'bg-red-500/20 text-red-400 border border-red-500/30'
            }`}>
              {autoStatus.status === 'idle' ? 'ACTIVE' : autoStatus.status?.toUpperCase()}
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-sm">
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Trade Cycles</div>
              <div className="text-white font-bold font-mono text-lg">{autoStatus.total_cycles}</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Trades Opened</div>
              <div className="text-green-400 font-bold font-mono text-lg">{autoStatus.total_trades_opened}</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Trades Closed</div>
              <div className="text-red-400 font-bold font-mono text-lg">{autoStatus.total_trades_closed}</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Schedule</div>
              <div className="text-green-400 font-bold text-sm">Event-Driven</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Next Run</div>
              <div className="text-white font-mono text-sm">
                {autoStatus.next_scheduled_run
                  ? new Date(autoStatus.next_scheduled_run).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                  : 'Soon'}
              </div>
            </div>
          </div>
          {autoStatus.last_result && (
            <div className="mt-3 text-neutral-500 text-xs">
              Last cycle: {autoStatus.last_result.opened} opened, {autoStatus.last_result.closed} closed
              {autoStatus.last_result.regime && ` | ${autoStatus.last_result.regime} regime`}
              {autoStatus.last_run && ` | ${new Date(autoStatus.last_run).toLocaleString()}`}
            </div>
          )}
          <p className="text-neutral-600 text-xs mt-2 italic">
            The computer autonomously analyzes 500+ stocks and executes trades event-driven during market hours. No human intervention required.
          </p>
        </div>
      )}

      {/* Queued Trades — what the AI wants to trade next */}
      {queuedTrades && queuedTrades.total_queued > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="w-3 h-3 rounded-full bg-green-500 animate-pulse" />
            <h3 className="text-white font-bold text-lg">Queued Trades</h3>
            <span className="px-3 py-1 rounded-full text-xs font-bold bg-green-500/20 text-green-400 border border-green-500/30">
              {queuedTrades.total_queued} PENDING
            </span>
            <span className="text-neutral-500 text-xs ml-auto">
              {queuedTrades.regime} regime
            </span>
          </div>
          <p className="text-neutral-500 text-xs mb-3">
            The AI is waiting to execute these trades on the next cycle.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {queuedTrades.queued_longs?.filter(t => t.status === 'queued').length > 0 && (
              <div>
                <h4 className="text-green-400 text-sm font-bold mb-2">
                  LONG Queue ({queuedTrades.queued_longs.filter(t => t.status === 'queued').length})
                </h4>
                <div className="space-y-1">
                  {queuedTrades.queued_longs.filter(t => t.status === 'queued').slice(0, 10).map(t => (
                    <div key={t.symbol} className="flex items-center justify-between bg-neutral-900 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-white font-bold text-sm">{t.symbol}</span>
                        <span className="text-neutral-600 text-xs">{t.sector}</span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-neutral-400 font-mono text-xs">${t.price}</span>
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          t.confidence >= 70 ? 'bg-green-500/20 text-green-400' : 'bg-neutral-500/20 text-neutral-400'
                        }`}>{t.confidence}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {queuedTrades.queued_shorts?.filter(t => t.status === 'queued').length > 0 && (
              <div>
                <h4 className="text-red-400 text-sm font-bold mb-2">
                  SHORT Queue ({queuedTrades.queued_shorts.filter(t => t.status === 'queued').length})
                </h4>
                <div className="space-y-1">
                  {queuedTrades.queued_shorts.filter(t => t.status === 'queued').slice(0, 10).map(t => (
                    <div key={t.symbol} className="flex items-center justify-between bg-neutral-900 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-white font-bold text-sm">{t.symbol}</span>
                        <span className="text-neutral-600 text-xs">{t.sector}</span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-neutral-400 font-mono text-xs">${t.price}</span>
                        <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                          t.confidence >= 70 ? 'bg-green-500/20 text-green-400' : 'bg-neutral-500/20 text-neutral-400'
                        }`}>{t.confidence}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ====== OPEN POSITIONS TABLE ====== */}
      {portfolio?.positions?.length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6 relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-green-500/50 via-green-500/20 to-red-500/50"></div>
          <h3 className="text-white font-bold text-lg mb-3">Open Positions ({portfolio.positions.length})</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-neutral-500 text-xs border-b border-neutral-800">
                  <th className="text-left py-2 px-2">Ticker</th>
                  <th className="text-left py-2 px-2">Dir</th>
                  <th className="text-right py-2 px-2">Entry</th>
                  <th className="text-right py-2 px-2">Current</th>
                  <th className="text-right py-2 px-2">P&L</th>
                  <th className="text-right py-2 px-2">Value</th>
                  <th className="text-right py-2 px-2">Days</th>
                  <th className="text-left py-2 px-2">Sector</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.positions.map(p => (
                  <tr key={p.trade_id} className={`border-b border-neutral-800/50 ${(p.unrealized_pct || 0) >= 0 ? 'bg-green-500/[0.02]' : 'bg-red-500/[0.02]'}`}>
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
                    <td className="py-2 px-2 text-right font-mono text-neutral-400">
                      ${(p.position_value || 0).toLocaleString(undefined, {maximumFractionDigits: 0})}
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
            intelligence.system_status === 'learning' ? 'bg-green-500/20 text-green-400' :
            intelligence.system_status === 'adapting' ? 'bg-neutral-500/20 text-neutral-400' :
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
                      entry.weight > 20 ? '#22c55e' :
                      entry.weight > 15 ? '#4ade80' : '#737373'
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
  const textColor = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' : 'text-white'
  const borderColor = color === 'green' ? 'border-green-500/30' : color === 'red' ? 'border-red-500/30' : 'border-neutral-800'
  const glowBg = color === 'green' ? 'bg-gradient-to-b from-green-500/5 to-transparent' : color === 'red' ? 'bg-gradient-to-b from-red-500/5 to-transparent' : ''
  return (
    <div className={`bg-neutral-900 rounded-lg p-3 border ${borderColor} ${glowBg}`}>
      <div className="text-neutral-500 text-xs">{label}</div>
      <div className={`text-lg font-bold font-mono ${textColor}`}>{value}</div>
    </div>
  )
}

function GradientDivider() {
  return <div className="h-px bg-gradient-to-r from-transparent via-green-500/30 to-transparent my-4" />
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
