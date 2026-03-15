import { useState, useEffect } from 'react'
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend, Cell,
} from 'recharts'

export default function Performance() {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/performance')
      .then((res) => res.json())
      .then((data) => { setStats(data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="text-center py-20">
        <div className="inline-block animate-spin rounded-full h-12 w-12 border-4 border-slate-600 border-t-orange-500"></div>
        <p className="text-slate-400 mt-4">Loading performance data...</p>
      </div>
    )
  }

  if (!stats || stats.total_predictions === 0) {
    return (
      <div className="max-w-2xl mx-auto px-4 py-20 text-center">
        <h1 className="text-3xl font-bold text-white mb-4">📊 Performance Tracker</h1>
        <p className="text-slate-400 text-lg mb-4">{stats?.message || 'No predictions yet.'}</p>
        <p className="text-slate-500">
          Go to the home page, analyze a stock, and click "Save This Prediction" to start tracking!
        </p>
      </div>
    )
  }

  const benchmarkData = stats.benchmarks ? [
    { name: 'Our Picks', value: stats.avg_return_pct, fill: '#f97316' },
    { name: 'S&P 500', value: stats.benchmarks.sp500?.total_return_pct || 0, fill: '#3b82f6' },
    { name: 'Nasdaq', value: stats.benchmarks.nasdaq?.total_return_pct || 0, fill: '#22c55e' },
    { name: 'Dow Jones', value: stats.benchmarks.dow_jones?.total_return_pct || 0, fill: '#a78bfa' },
  ] : []

  return (
    <div className="max-w-6xl mx-auto px-4 py-8 space-y-6">
      <h1 className="text-3xl font-bold text-white">📊 Performance Tracker</h1>
      <p className="text-slate-400">How accurate are our predictions? Let's find out.</p>

      {/* Overview Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Total Predictions" value={stats.total_predictions} />
        <StatCard label="Win Rate" value={`${stats.win_rate}%`}
          color={stats.win_rate >= 50 ? 'text-green-400' : 'text-red-400'} />
        <StatCard label="Avg Return" value={`${stats.avg_return_pct >= 0 ? '+' : ''}${stats.avg_return_pct}%`}
          color={stats.avg_return_pct >= 0 ? 'text-green-400' : 'text-red-400'} />
        <StatCard label="Pending" value={stats.pending} />
      </div>

      {/* Win/Loss */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-slate-800 rounded-xl p-6 border border-slate-700">
          <h2 className="text-xl font-semibold text-white mb-4">Win / Loss Record</h2>
          <div className="flex items-center justify-center gap-8">
            <div className="text-center">
              <p className="text-4xl font-bold text-green-400">{stats.wins}</p>
              <p className="text-slate-400">Wins</p>
            </div>
            <div className="text-4xl text-slate-600">—</div>
            <div className="text-center">
              <p className="text-4xl font-bold text-red-400">{stats.losses}</p>
              <p className="text-slate-400">Losses</p>
            </div>
          </div>
          {stats.current_streak && (
            <p className="text-center text-slate-400 mt-4">
              Current Streak: <span className={stats.current_streak.type === 'hit' ? 'text-green-400' : 'text-red-400'}>
                {stats.current_streak.count} {stats.current_streak.type === 'hit' ? 'wins' : 'losses'}
              </span>
            </p>
          )}
        </div>

        {/* Benchmark Comparison */}
        {benchmarkData.length > 0 && (
          <div className="bg-slate-800 rounded-xl p-6 border border-slate-700">
            <h2 className="text-xl font-semibold text-white mb-4">vs Market Indices (1Y)</h2>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={benchmarkData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="name" stroke="#64748b" tick={{ fontSize: 12 }} />
                <YAxis stroke="#64748b" tick={{ fontSize: 12 }} tickFormatter={(v) => `${v}%`} />
                <Tooltip
                  contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '8px' }}
                  formatter={(v) => [`${v.toFixed(2)}%`, 'Return']}
                />
                <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                  {benchmarkData.map((entry, i) => (
                    <Cell key={i} fill={entry.fill} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* Best & Worst */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {stats.best_prediction && (
          <div className="bg-green-900/20 rounded-xl p-6 border border-green-800/30">
            <h3 className="text-green-400 font-medium mb-2">Best Prediction</h3>
            <p className="text-2xl font-bold text-white">{stats.best_prediction.ticker}</p>
            <p className="text-green-400 text-lg">+{stats.best_prediction.return_pct}%</p>
          </div>
        )}
        {stats.worst_prediction && (
          <div className="bg-red-900/20 rounded-xl p-6 border border-red-800/30">
            <h3 className="text-red-400 font-medium mb-2">Worst Prediction</h3>
            <p className="text-2xl font-bold text-white">{stats.worst_prediction.ticker}</p>
            <p className="text-red-400 text-lg">{stats.worst_prediction.return_pct}%</p>
          </div>
        )}
      </div>

      {/* Predictions Table */}
      <div className="bg-slate-800 rounded-xl p-6 border border-slate-700 overflow-x-auto">
        <h2 className="text-xl font-semibold text-white mb-4">All Predictions</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-slate-400 border-b border-slate-700">
              <th className="text-left py-2">Date</th>
              <th className="text-left py-2">Ticker</th>
              <th className="text-left py-2">Signal</th>
              <th className="text-right py-2">Entry</th>
              <th className="text-right py-2">Current/Final</th>
              <th className="text-right py-2">Return</th>
              <th className="text-center py-2">Outcome</th>
            </tr>
          </thead>
          <tbody>
            {stats.predictions.map((p) => (
              <tr key={p.id} className="border-b border-slate-700/50">
                <td className="py-2 text-slate-400">{new Date(p.predicted_at).toLocaleDateString()}</td>
                <td className="py-2 text-white font-medium">{p.ticker}</td>
                <td className={`py-2 ${p.predicted_direction.includes('Buy') ? 'text-green-400' : p.predicted_direction.includes('Sell') ? 'text-red-400' : 'text-yellow-400'}`}>
                  {p.predicted_direction}
                </td>
                <td className="py-2 text-right text-slate-300">${p.entry_price.toFixed(2)}</td>
                <td className="py-2 text-right text-slate-300">
                  {p.actual_price ? `$${p.actual_price.toFixed(2)}` : '—'}
                </td>
                <td className={`py-2 text-right ${p.actual_return_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {p.actual_return_pct != null ? `${p.actual_return_pct >= 0 ? '+' : ''}${p.actual_return_pct}%` : '—'}
                </td>
                <td className="py-2 text-center">
                  <span className={`px-2 py-0.5 rounded-full text-xs ${
                    p.actual_outcome === 'hit' ? 'bg-green-900/50 text-green-400' :
                    p.actual_outcome === 'miss' ? 'bg-red-900/50 text-red-400' :
                    'bg-slate-700 text-slate-400'
                  }`}>
                    {p.actual_outcome}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StatCard({ label, value, color = 'text-white' }) {
  return (
    <div className="bg-slate-800 rounded-xl p-4 border border-slate-700">
      <p className="text-slate-400 text-xs">{label}</p>
      <p className={`text-2xl font-bold ${color}`}>{value}</p>
    </div>
  )
}
