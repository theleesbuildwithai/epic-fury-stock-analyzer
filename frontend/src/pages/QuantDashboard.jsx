import { useState, useEffect, Component } from 'react'
import {
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, BarChart, Bar, Cell
} from 'recharts'

// Error boundary to prevent black screen crashes
class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="bg-red-900/20 border border-red-800 rounded-xl p-8 text-center m-4">
          <p className="text-red-400 font-bold text-lg mb-2">Something went wrong</p>
          <p className="text-neutral-400 text-sm mb-4">{this.state.error?.message}</p>
          <button onClick={() => { this.setState({ hasError: false }); window.location.reload() }}
            className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-500 text-sm">
            Reload Page
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

// Safe number helper — prevents crashes from non-number API values
const num = (v, fallback = 0) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

const TABS = ['Quant Picks', 'Paper Portfolio', 'System Intelligence']

export default function QuantDashboard() {
  const [activeTab, setActiveTab] = useState(1)
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
      if (res.ok) setIntelligence(await res.json())
    } catch { }
    setLoading(p => ({ ...p, intel: false }))
  }

  const fetchAutoStatus = async () => {
    try {
      const res = await fetch('/api/auto-trading-status')
      if (res.ok) setAutoStatus(await res.json())
    } catch { }
  }

  const fetchQueuedTrades = async () => {
    try {
      const res = await fetch('/api/queued-trades')
      if (res.ok) setQueuedTrades(await res.json())
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
          Sentinel Quant
        </h1>
        <p className="text-neutral-500 mt-1">
          22-factor quant engine • Autonomous trading • Self-learning system
        </p>

        {/* Portfolio Value — inline with header */}
        {portfolio && (
          <div className="mt-3 flex items-end justify-between flex-wrap gap-3">
            <div className="flex gap-4 text-xs text-neutral-500">
              <span>22 Quant Factors</span>
              <span>520+ Stocks Analyzed</span>
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
      <ErrorBoundary>
      {activeTab === 0 && (
        <QuantPicksTab picks={quantPicks} loading={loading.picks} RegimeBadge={RegimeBadge} />
      )}
      {activeTab === 1 && (
        <PaperPortfolioTab
          portfolio={portfolio} performance={performance}
          loading={loading.portfolio}
          autoStatus={autoStatus}
          queuedTrades={queuedTrades}
          quantPicks={quantPicks}
        />
      )}
      {activeTab === 2 && (
        <IntelligenceTab intelligence={intelligence} loading={loading.intel} quantPicks={quantPicks} />
      )}
      </ErrorBoundary>
    </div>
  )
}

// ============================================================
// TAB 1: QUANT PICKS
// ============================================================
function QuantPicksTab({ picks, loading, RegimeBadge }) {
  if (loading) return <LoadingSpinner text="Analyzing 520+ stocks across 14 quant factors... this takes ~90 seconds" />
  if (!picks) return <EmptyState text="Quant picks are being computed. Refresh in 60 seconds." />
  if (picks.cache_status === 'cold') return <EmptyState text={picks.message || "Analyzing 520+ stocks... data will appear after the next trade cycle."} />

  const regime = picks.regime || {}
  const macro = picks.macro || {}

  return (
    <div className="space-y-6">
      {/* Regime & Macro Panel */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <div className="flex flex-wrap items-center gap-4 mb-4">
          <RegimeBadge regime={regime.regime || 'SIDEWAYS'} />
          <span className="text-neutral-500 text-sm">
            Confidence: {regime.confidence ?? 'N/A'}% | VIX: {regime.vix_level ?? 'N/A'}
          </span>
          <span className="text-neutral-600 text-xs">
            {picks.total_analyzed ?? 0} stocks analyzed in {picks.computation_time_seconds ?? 0}s
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
            <MacroCard label="10Y Treasury" value={`${macro.treasury_10y?.value ?? 'N/A'}%`}
              signal={macro.treasury_10y?.signal} />
            <MacroCard label="Crude Oil" value={`$${macro.crude_oil?.value ?? 'N/A'}`}
              signal={macro.crude_oil?.signal} />
            <MacroCard label="Gold" value={`$${macro.gold?.value ?? 'N/A'}`}
              signal={macro.gold?.signal} />
            <MacroCard label="VIX" value={macro.vix?.value ?? 'N/A'}
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
          <th className="text-right py-2 px-2">ADX</th>
          <th className="text-left py-2 px-2">Sector</th>
          <th className="text-left py-2 px-2">Top Reason</th>
        </tr>
      </thead>
      <tbody>
        {picks.map((p, i) => (
          <tr key={p.symbol} className="border-b border-neutral-800/50 hover:bg-neutral-900/50">
            <td className="py-2 px-2 text-neutral-600">{p.rank || i + 1}</td>
            <td className="py-2 px-2 font-bold text-white">{p.symbol}</td>
            <td className="py-2 px-2 text-right font-mono text-white">${p.price ?? 0}</td>
            <td className={`py-2 px-2 text-right font-mono font-bold ${
              (p.composite_score || 0) >= 0 ? 'text-green-400' : 'text-red-400'
            }`}>
              {(p.composite_score || 0) > 0 ? '+' : ''}{(p.composite_score || 0).toFixed(2)}
            </td>
            <td className="py-2 px-2 text-right">
              <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                (p.confidence || 0) >= 70 ? 'bg-green-500/20 text-green-400' :
                (p.confidence || 0) >= 55 ? 'bg-neutral-500/20 text-neutral-400' :
                'bg-neutral-500/20 text-neutral-400'
              }`}>
                {p.confidence ?? 0}%
              </span>
            </td>
            <td className="py-2 px-2 text-right font-mono text-neutral-400">{p.rsi14 ?? 'N/A'}</td>
            <td className="py-2 px-2 text-right font-mono text-neutral-400">{p.volatility_60d ?? 0}%</td>
            <td className={`py-2 px-2 text-right font-mono text-xs font-bold ${
              (p.adx || 25) > 30 ? 'text-green-400' :
              (p.adx || 25) > 20 ? 'text-neutral-400' :
              'text-yellow-400'
            }`}>{(p.adx || 25).toFixed(0)}</td>
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
function PaperPortfolioTab({ portfolio, performance, loading, autoStatus, queuedTrades, quantPicks }) {
  if (loading) return <LoadingSpinner text="Loading portfolio..." />
  if (!portfolio) return <LoadingSpinner text="Connecting to trading engine... please wait" />

  const exp = portfolio?.exposure || {}
  const stats = portfolio?.stats || {}
  const perf = performance?.overall || {}
  return (
    <div className="space-y-6">

      {/* ====== FUND PERFORMANCE HEADER ====== */}
      {portfolio && (
        <div className="bg-black border-2 border-green-500/40 rounded-xl p-6 relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-green-500 via-green-400 to-green-600"></div>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-2xl font-black text-white">Sentinel Quant Fund</h2>
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
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mt-5">
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
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-cyan-500/30">
              <div className="text-neutral-500 text-xs">Calls</div>
              <div className="text-cyan-400 font-bold font-mono text-lg">{portfolio.num_calls || 0}</div>
              <div className="text-neutral-600 text-xs">
                ${((exp.calls_value || 0)).toLocaleString(undefined, {maximumFractionDigits: 0})}
              </div>
            </div>
            <div className="bg-neutral-900/80 rounded-lg p-3 border border-violet-500/30">
              <div className="text-neutral-500 text-xs">Puts</div>
              <div className="text-violet-400 font-bold font-mono text-lg">{portfolio.num_puts || 0}</div>
              <div className="text-neutral-600 text-xs">
                ${((exp.puts_value || 0)).toLocaleString(undefined, {maximumFractionDigits: 0})}
              </div>
            </div>
            {(portfolio.num_options || 0) > 0 && (
              <div className="bg-neutral-900/80 rounded-lg p-3 border border-amber-500/30">
                <div className="text-neutral-500 text-xs">Options %</div>
                <div className="text-amber-400 font-bold font-mono text-lg">{exp.options_pct || 0}%</div>
                <div className="text-neutral-600 text-xs">of portfolio</div>
              </div>
            )}
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

      {/* ====== RISK INTELLIGENCE ====== */}
      {quantPicks && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold text-lg mb-4">🛡️ Risk Intelligence</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            {/* Portfolio VaR */}
            <div className={`bg-neutral-900 rounded-lg p-3 border ${
              (quantPicks.portfolio_var?.var_pct || 0) > 2 ? 'border-red-500/40' :
              (quantPicks.portfolio_var?.var_pct || 0) > 1.5 ? 'border-yellow-500/40' :
              'border-green-500/20'
            }`}>
              <div className="text-neutral-500 text-xs">Daily VaR (95%)</div>
              <div className={`text-xl font-black font-mono ${
                (quantPicks.portfolio_var?.var_pct || 0) > 2 ? 'text-red-400' :
                (quantPicks.portfolio_var?.var_pct || 0) > 1.5 ? 'text-yellow-400' :
                'text-green-400'
              }`}>
                {(quantPicks.portfolio_var?.var_pct || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">
                {quantPicks.portfolio_var?.status === 'VAR_EXCEEDED' ? '🚫 BLOCKED' :
                 quantPicks.portfolio_var?.status === 'VAR_WARNING' ? '⚠️ Half-size' :
                 quantPicks.portfolio_var?.status === 'VAR_ELEVATED' ? '⚡ Elevated' : 'OK'}
              </div>
            </div>

            {/* CVaR */}
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">CVaR (Tail Risk)</div>
              <div className="text-xl font-black font-mono text-neutral-300">
                {(quantPicks.portfolio_var?.cvar_pct || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">Expected shortfall</div>
            </div>

            {/* Portfolio Correlation */}
            <div className={`bg-neutral-900 rounded-lg p-3 border ${
              (quantPicks.portfolio_var?.avg_correlation || 0) > 0.6 ? 'border-red-500/40' :
              (quantPicks.portfolio_var?.avg_correlation || 0) > 0.4 ? 'border-yellow-500/40' :
              'border-neutral-800'
            }`}>
              <div className="text-neutral-500 text-xs">Avg Correlation</div>
              <div className={`text-xl font-black font-mono ${
                (quantPicks.portfolio_var?.avg_correlation || 0) > 0.6 ? 'text-red-400' :
                (quantPicks.portfolio_var?.avg_correlation || 0) > 0.4 ? 'text-yellow-400' :
                'text-green-400'
              }`}>
                {(quantPicks.portfolio_var?.avg_correlation || 0).toFixed(2)}
              </div>
              <div className="text-neutral-600 text-xs">
                {(quantPicks.portfolio_var?.avg_correlation || 0) > 0.6 ? 'High risk' :
                 (quantPicks.portfolio_var?.avg_correlation || 0) > 0.4 ? 'Moderate' : 'Diversified'}
              </div>
            </div>

            {/* VaR Multiplier */}
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Size Multiplier</div>
              <div className={`text-xl font-black font-mono ${
                (quantPicks.portfolio_var?.var_multiplier || 1) < 0.5 ? 'text-red-400' :
                (quantPicks.portfolio_var?.var_multiplier || 1) < 1 ? 'text-yellow-400' :
                'text-green-400'
              }`}>
                {(quantPicks.portfolio_var?.var_multiplier || 1).toFixed(1)}x
              </div>
              <div className="text-neutral-600 text-xs">VaR budget</div>
            </div>

            {/* Daily Vol */}
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Portfolio Vol</div>
              <div className="text-xl font-black font-mono text-neutral-300">
                {(quantPicks.portfolio_var?.portfolio_vol_daily || 0).toFixed(2)}%
              </div>
              <div className="text-neutral-600 text-xs">Daily volatility</div>
            </div>

            {/* Positions Analyzed */}
            <div className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-neutral-500 text-xs">Scoring Engine</div>
              <div className="text-xl font-black font-mono text-blue-400">
                15
              </div>
              <div className="text-neutral-600 text-xs">Active factors</div>
            </div>
          </div>
        </div>
      )}

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
            The computer autonomously analyzes 520+ stocks and executes trades event-driven during market hours. No human intervention required.
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
                  <th className="text-left py-2 px-2">Type</th>
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
                {portfolio.positions.map(p => {
                  const isOption = p.instrument_type === 'call' || p.instrument_type === 'put';
                  return (
                  <tr key={p.trade_id} className={`border-b border-neutral-800/50 ${(p.unrealized_pct || 0) >= 0 ? 'bg-green-500/[0.02]' : 'bg-red-500/[0.02]'}`}>
                    <td className="py-2 px-2 font-bold text-white">
                      {p.ticker}
                      {isOption && p.strike_price && (
                        <span className="text-neutral-500 text-xs ml-1">${p.strike_price}</span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-xs font-bold">
                      {isOption ? (
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                          p.instrument_type === 'call' ? 'bg-cyan-500/20 text-cyan-400' : 'bg-violet-500/20 text-violet-400'
                        }`}>
                          {p.instrument_type.toUpperCase()}
                        </span>
                      ) : (
                        <span className="text-neutral-600 text-[10px]">STK</span>
                      )}
                    </td>
                    <td className={`py-2 px-2 text-xs font-bold ${
                      isOption
                        ? (p.instrument_type === 'call' ? 'text-cyan-400' : 'text-violet-400')
                        : (p.direction === 'long' ? 'text-green-400' : 'text-red-400')
                    }`}>
                      {isOption
                        ? p.instrument_type.toUpperCase()
                        : (p.direction || 'long').toUpperCase()}
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-neutral-400">
                      {isOption ? `$${p.premium ?? p.entry_price ?? 0}` : `$${p.entry_price ?? 0}`}
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-white">${p.current_price ?? 0}</td>
                    <td className={`py-2 px-2 text-right font-mono font-bold ${
                      (p.unrealized_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}>
                      {(p.unrealized_pct || 0) >= 0 ? '+' : ''}{(p.unrealized_pct || 0).toFixed(2)}%
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-neutral-400">
                      ${(p.position_value || 0).toLocaleString(undefined, {maximumFractionDigits: 0})}
                    </td>
                    <td className="py-2 px-2 text-right text-neutral-400">
                      {isOption && p.dte != null ? (
                        <span>{p.dte}d <span className="text-neutral-600 text-[10px]">DTE</span></span>
                      ) : (
                        <span>{p.days_held ?? 0}d</span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-neutral-500 text-xs">
                      {isOption && p.contracts ? `${p.contracts}x ` : ''}{p.sector}
                    </td>
                  </tr>
                  );
                })}
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
function IntelligenceTab({ intelligence, loading, quantPicks }) {
  const intel = intelligence || {}
  const weights = intel.current_weights || {}
  const factorPerf = intel.factor_performance || {}
  const hedge = quantPicks?.dynamic_hedge || {}
  const sectorRankings = quantPicks?.sector_rankings || []

  // All 22 factors for display
  const ALL_FACTORS = [
    { key: 'momentum', name: 'Momentum (12-1M)', desc: 'Jegadeesh-Titman trend continuation' },
    { key: 'value', name: 'Value (P/E + P/B)', desc: 'Real fundamentals — earnings yield + book-to-price' },
    { key: 'quality', name: 'Quality', desc: 'Return consistency (Sharpe ratio)' },
    { key: 'low_vol', name: 'Low Volatility', desc: 'GARCH-enhanced vol anomaly' },
    { key: 'rsi2', name: 'RSI(2) Mean Reversion', desc: 'Connors strategy (75-91% win rate)' },
    { key: 'volume', name: 'Volume Confirmation', desc: 'OBV trend — smart money flow' },
    { key: 'smart_money', name: 'Smart Money Divergence', desc: 'Price-volume divergence detection' },
    { key: 'relative_strength', name: 'Relative Strength', desc: 'Outperformance vs sector peers' },
    { key: 'bb_squeeze', name: 'Bollinger Squeeze', desc: 'Volatility compression breakout' },
    { key: 'vwap', name: 'VWAP Proximity', desc: 'Institutional execution quality' },
    { key: 'hurst', name: 'Hurst Exponent', desc: 'Trend vs mean-reversion classifier' },
    { key: 'autocorr', name: 'Autocorrelation', desc: 'Serial return pattern exploitation' },
    { key: 'stat_arb', name: 'Stat Arb Z-Score', desc: 'Distance from statistical fair value' },
    { key: 'kurtosis', name: 'Kurtosis', desc: 'Fat tail risk detection' },
    { key: 'vol_compression', name: 'Vol Compression', desc: 'GARCH breakout predictor' },
    { key: 'mtf_alignment', name: 'Multi-Timeframe', desc: 'Daily+weekly+monthly trend alignment' },
    { key: 'earnings_drift', name: 'Post-Earnings Drift', desc: 'Earnings beat/miss continuation' },
    { key: 'vpoc', name: 'Volume Profile (VPOC)', desc: 'Point of control support/resistance' },
    { key: 'ichimoku', name: 'Ichimoku Cloud', desc: 'Japanese trend confirmation system' },
    { key: 'sector_rotation', name: 'Sector Rotation', desc: 'Rotate into hot sectors, avoid cold' },
    { key: 'candlestick', name: 'Candlestick Patterns', desc: 'Hammer, engulfing, doji detection' },
    { key: 'beta', name: 'Stock Beta', desc: 'Low-beta anomaly — defensive stocks outperform risk-adjusted' },
  ]

  const weightData = Object.entries(weights).map(([name, weight]) => ({
    name: name.replaceAll('_', ' '),
    weight: Math.round(weight * 100),
    win_rate: factorPerf[name]?.win_rate || 0,
    sharpe: factorPerf[name]?.sharpe || 0,
  }))

  const hedgeColor = hedge.risk_level === 'LOW' ? 'green' : hedge.risk_level === 'MODERATE' ? 'yellow' : hedge.risk_level === 'HIGH' ? 'orange' : 'red'
  const hedgeBorder = hedgeColor === 'green' ? 'border-green-500/30' : hedgeColor === 'yellow' ? 'border-yellow-500/30' : hedgeColor === 'orange' ? 'border-orange-500/30' : 'border-red-500/30'
  const hedgeText = hedgeColor === 'green' ? 'text-green-400' : hedgeColor === 'yellow' ? 'text-yellow-400' : hedgeColor === 'orange' ? 'text-orange-400' : 'text-red-400'
  const hedgeBg = hedgeColor === 'green' ? 'bg-green-500/10' : hedgeColor === 'yellow' ? 'bg-yellow-500/10' : hedgeColor === 'orange' ? 'bg-orange-500/10' : 'bg-red-500/10'

  return (
    <div className="space-y-6">
      {/* Dynamic Hedge / Risk Shield */}
      {hedge.risk_level && (
        <div className={`bg-black border ${hedgeBorder} rounded-xl p-6`}>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-bold text-white">Risk Shield</h2>
            <span className={`px-4 py-1.5 rounded-full text-sm font-bold ${hedgeBg} ${hedgeText}`}>
              {hedge.risk_level}
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Risk Score</div>
              <div className={`text-2xl font-bold font-mono ${hedgeText}`}>{hedge.risk_score}/10</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3">
              <div className="text-neutral-500 text-xs">Exposure</div>
              <div className={`text-2xl font-bold font-mono ${hedgeText}`}>{hedge.exposure_pct}%</div>
            </div>
            <div className="bg-neutral-900 rounded-lg p-3 col-span-2">
              <div className="text-neutral-500 text-xs">Action</div>
              <div className="text-white text-sm font-medium mt-1">{hedge.action}</div>
            </div>
          </div>
          {hedge.risk_factors?.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {hedge.risk_factors.map((f, i) => (
                <span key={i} className={`px-2 py-1 rounded text-xs ${hedgeBg} ${hedgeText}`}>{f}</span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* System Status */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <div className="flex items-center gap-3 mb-4">
          <h2 className="text-xl font-bold text-white">System Status</h2>
          <span className={`px-3 py-1 rounded-full text-xs font-bold ${
            intel.system_status === 'confident' ? 'bg-green-500/20 text-green-400' :
            intel.system_status === 'learning' ? 'bg-green-500/20 text-green-400' :
            intel.system_status === 'adapting' ? 'bg-neutral-500/20 text-neutral-400' :
            'bg-neutral-500/20 text-neutral-400'
          }`}>
            {intel.system_status?.toUpperCase() || 'ACTIVE'}
          </span>
          <span className="text-neutral-500 text-sm">
            {intel.total_closed_trades || 0} trades analyzed
          </span>
          <span className="text-neutral-500 text-sm ml-auto">
            {quantPicks?.total_factors || 22} factors active
          </span>
        </div>

        {intel.insights?.map((insight, i) => (
          <p key={i} className="text-neutral-400 text-sm mb-1">{insight}</p>
        ))}
      </div>

      {/* All 20 Factors */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h3 className="text-white font-bold mb-4">All {ALL_FACTORS.length} Scoring Factors</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {ALL_FACTORS.map((f, i) => {
            const isNew = ['earnings_drift', 'vpoc', 'ichimoku', 'sector_rotation', 'candlestick'].includes(f.key)
            return (
              <div key={f.key} className={`rounded-lg p-3 border ${isNew ? 'bg-blue-950/30 border-blue-500/30' : 'bg-neutral-900 border-neutral-800'}`}>
                <div className="flex items-center gap-2">
                  <span className="text-neutral-500 text-xs font-mono">#{i + 1}</span>
                  <span className="text-white text-sm font-semibold">{f.name}</span>
                  {isNew && <span className="px-1.5 py-0.5 text-[10px] font-bold bg-blue-500/20 text-blue-400 rounded">NEW</span>}
                </div>
                <div className="text-neutral-500 text-xs mt-1">{f.desc}</div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Sector Rotation Heatmap */}
      {sectorRankings.length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Sector Rotation Heatmap</h3>
          <p className="text-neutral-500 text-sm mb-3">Sectors ranked by momentum — rotate into HOT, avoid COLD</p>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {sectorRankings.map(s => (
              <div key={s.sector} className={`rounded-lg p-3 border ${
                s.zone === 'HOT' ? 'bg-green-950/30 border-green-500/30' :
                s.zone === 'COLD' ? 'bg-red-950/30 border-red-500/30' :
                'bg-neutral-900 border-neutral-800'
              }`}>
                <div className="flex items-center justify-between">
                  <span className="text-white text-sm font-semibold">{s.sector}</span>
                  <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                    s.zone === 'HOT' ? 'bg-green-500/20 text-green-400' :
                    s.zone === 'COLD' ? 'bg-red-500/20 text-red-400' :
                    'bg-neutral-500/20 text-neutral-400'
                  }`}>{s.zone}</span>
                </div>
                <div className={`text-lg font-bold font-mono mt-1 ${
                  s.momentum_pct > 0 ? 'text-green-400' : 'text-red-400'
                }`}>
                  {s.momentum_pct > 0 ? '+' : ''}{s.momentum_pct}%
                </div>
                <div className="text-neutral-600 text-xs">Rank #{s.rank}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Strengths & Weaknesses */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-black border border-green-500/30 rounded-xl p-6">
          <h3 className="text-green-400 font-bold mb-3">Strengths</h3>
          {intel.strengths?.length > 0 ? (
            intel.strengths.map((s, i) => (
              <p key={i} className="text-neutral-300 text-sm mb-2">+ {s}</p>
            ))
          ) : (
            <p className="text-neutral-500 text-sm">Still learning...</p>
          )}
        </div>
        <div className="bg-black border border-red-500/30 rounded-xl p-6">
          <h3 className="text-red-400 font-bold mb-3">Weaknesses</h3>
          {intel.weaknesses?.length > 0 ? (
            intel.weaknesses.map((w, i) => (
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
      {intel.sector_performance?.sectors && Object.keys(intel.sector_performance.sectors).length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Performance by Sector</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {Object.entries(intel.sector_performance.sectors).map(([sector, stats]) => (
              <div key={sector} className="bg-neutral-900 rounded-lg p-3">
                <div className="text-white text-sm font-bold">{sector}</div>
                <div className={`text-lg font-bold font-mono ${
                  stats.win_rate > 55 ? 'text-green-400' : stats.win_rate < 45 ? 'text-red-400' : 'text-neutral-400'
                }`}>
                  {stats.win_rate ?? 0}%
                </div>
                <div className="text-neutral-500 text-xs">{stats.total_trades ?? 0} trades</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Confidence Calibration */}
      {intel.confidence_calibration && Object.keys(intel.confidence_calibration).length > 0 && (
        <div className="bg-black border border-neutral-700 rounded-xl p-6">
          <h3 className="text-white font-bold mb-4">Confidence Calibration</h3>
          <p className="text-neutral-500 text-sm mb-3">
            How well our predicted confidence matches actual outcomes
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            {Object.entries(intel.confidence_calibration).map(([bucket, data]) => (
              <div key={bucket} className="bg-neutral-900 rounded-lg p-3 text-center">
                <div className="text-neutral-400 text-xs">{bucket}% predicted</div>
                <div className={`text-xl font-bold font-mono ${
                  (data.actual_win_rate || 0) > (data.avg_predicted_confidence || 0) * 0.9
                    ? 'text-green-400' : 'text-red-400'
                }`}>
                  {data.actual_win_rate ?? 0}%
                </div>
                <div className="text-neutral-600 text-xs">actual ({data.total_trades ?? 0} trades)</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Market Overlays Active */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h3 className="text-white font-bold mb-4">Active Market Overlays</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {[
            { name: 'Market Regime Detection', desc: 'S&P 500 trend + VIX + breadth → BULL/BEAR/SIDEWAYS', icon: 'G' },
            { name: 'Global Macro Overlay', desc: 'Treasury yields, crude oil, gold, VIX, yield curve, dollar strength', icon: 'M' },
            { name: 'Overnight Intelligence', desc: 'Futures, global markets, Bitcoin, gold/bonds gap detection', icon: 'N' },
            { name: 'Cross-Asset Signals', desc: 'Dollar, Bitcoin, Copper, Bonds momentum → sector adjustments', icon: 'X' },
            { name: 'Geopolitical Risk Layer', desc: 'CNN/Yahoo/CNBC scanning for wars, sanctions, conflicts', icon: 'P' },
            { name: 'Ceasefire/De-escalation', desc: 'Peace dividend: boost tech/consumer, penalize defense/energy', icon: 'C' },
            { name: 'Earnings Proximity Shield', desc: 'Reduce confidence 10-50% near earnings dates', icon: 'E' },
            { name: 'Ensemble Voting (3 Models)', desc: 'Momentum, mean-reversion, ML must agree on direction', icon: 'V' },
            { name: 'Dynamic Hedging Engine', desc: 'Auto-scale exposure based on composite risk score', icon: 'H' },
          ].map(o => (
            <div key={o.name} className="bg-neutral-900 rounded-lg p-3 border border-neutral-800 flex gap-3">
              <div className="w-8 h-8 rounded-lg bg-green-500/10 text-green-400 flex items-center justify-center text-sm font-bold flex-shrink-0">{o.icon}</div>
              <div>
                <div className="text-white text-sm font-semibold">{o.name}</div>
                <div className="text-neutral-500 text-xs">{o.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Risk Management Arsenal */}
      <div className="bg-black border border-neutral-700 rounded-xl p-6">
        <h3 className="text-white font-bold mb-4">Risk Management Arsenal</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {[
            { name: 'Kelly Criterion Sizing', desc: 'Optimal bet sizing from win rate + payoff ratio (half-Kelly for safety)' },
            { name: 'VIX-Scaled Positions', desc: 'VIX<15: 1.3x, VIX 20-25: 0.85x, VIX>35: 0.45x sizing' },
            { name: 'ATR Trailing Stops', desc: 'Per-stock volatility-based stops that ratchet up, never down' },
            { name: 'Win-Lock System', desc: 'Auto-lock all positions at daily gain threshold (3-8% depending on regime)' },
            { name: 'Drawdown Protection', desc: 'Halt at -10% from peak, halve sizes at -5%' },
            { name: 'Smart Order Timing', desc: 'Avoid first 15min, favor power hour (3-4 PM)' },
            { name: 'Streak Calibration', desc: '3+ losses → 50% size, 5+ wins → 130% size (press/protect)' },
            { name: 'Portfolio VaR Budget', desc: '95% VaR from position covariance — block trades when risk is full' },
            { name: 'Correlation Limits', desc: 'Block trades with >0.75 correlation to 3+ existing positions' },
            { name: 'Momentum Crash Filter', desc: 'Detect unwinding momentum and dead cat bounces' },
          ].map(r => (
            <div key={r.name} className="bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <div className="text-white text-sm font-semibold">{r.name}</div>
              <div className="text-neutral-500 text-xs mt-1">{r.desc}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Trading Philosophy */}
      <div className="bg-black border border-purple-500/20 rounded-xl p-6">
        <h3 className="text-white font-bold mb-4">Trading Philosophy</h3>
        <div className="space-y-3 text-neutral-400 text-sm">
          <p><span className="text-white font-semibold">Universe:</span> 520+ stocks across all 11 S&P 500 sectors + 16 ETFs</p>
          <p><span className="text-white font-semibold">Scoring:</span> 22-factor composite with z-score normalization + adaptive regime-aware weights</p>
          <p><span className="text-white font-semibold">Edge Sources:</span> Momentum + mean-reversion hybrid, statistical arbitrage, post-earnings drift, Ichimoku trends, volume profile, sector rotation</p>
          <p><span className="text-white font-semibold">Risk First:</span> Never risk more than Kelly half-fraction. VIX scales all sizes. ATR-based stops per stock. Portfolio VaR budget caps total risk.</p>
          <p><span className="text-white font-semibold">Regime Adaptive:</span> BEAR → favor defensives + shorts, reduce momentum weight. BULL → favor longs, reduce short exposure. SIDEWAYS → boost mean-reversion.</p>
          <p><span className="text-white font-semibold">Multi-Layer Confirmation:</span> 3-model ensemble must agree. ADX validates trend strength. Multi-timeframe alignment required. Overnight gaps and cross-asset signals confirm macro direction.</p>
          <p><span className="text-white font-semibold">Macro Aware:</span> Treasury yields, oil, gold, dollar strength, yield curve inversion, VIX term structure, geopolitical risk, and ceasefire/de-escalation signals all feed into sector-level adjustments.</p>
        </div>
      </div>
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
