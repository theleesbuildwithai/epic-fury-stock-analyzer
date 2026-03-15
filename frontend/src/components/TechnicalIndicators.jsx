import {
  ResponsiveContainer, LineChart, Line, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
} from 'recharts'

function RSIChart({ chartData }) {
  const interval = Math.floor(chartData.length / 6)
  return (
    <div>
      <h3 className="text-lg font-medium text-white mb-2">RSI (Relative Strength Index)</h3>
      <p className="text-slate-400 text-sm mb-3">
        Above 70 = overbought (might drop). Below 30 = oversold (might bounce).
      </p>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="date" stroke="#64748b" tick={{ fontSize: 10 }} interval={interval} tickFormatter={(d) => d.slice(5)} />
          <YAxis stroke="#64748b" domain={[0, 100]} tick={{ fontSize: 10 }} />
          <Tooltip contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '8px' }} />
          <ReferenceLine y={70} stroke="#ef4444" strokeDasharray="3 3" label={{ value: '70', fill: '#ef4444', fontSize: 10 }} />
          <ReferenceLine y={30} stroke="#22c55e" strokeDasharray="3 3" label={{ value: '30', fill: '#22c55e', fontSize: 10 }} />
          <Line type="monotone" dataKey="rsi" stroke="#eab308" strokeWidth={2} dot={false} name="RSI" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function MACDChart({ chartData }) {
  const interval = Math.floor(chartData.length / 6)
  return (
    <div>
      <h3 className="text-lg font-medium text-white mb-2">MACD</h3>
      <p className="text-slate-400 text-sm mb-3">
        When MACD crosses above signal = bullish. Below = bearish.
      </p>
      <ResponsiveContainer width="100%" height={200}>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="date" stroke="#64748b" tick={{ fontSize: 10 }} interval={interval} tickFormatter={(d) => d.slice(5)} />
          <YAxis stroke="#64748b" tick={{ fontSize: 10 }} />
          <Tooltip contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '8px' }} />
          <ReferenceLine y={0} stroke="#475569" />
          <Bar dataKey="macd_histogram" name="Histogram" fill="#64748b" />
          <Line type="monotone" dataKey="macd" stroke="#3b82f6" strokeWidth={2} dot={false} name="MACD" />
          <Line type="monotone" dataKey="macd_signal" stroke="#ef4444" strokeWidth={1} dot={false} name="Signal" />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

import { ComposedChart } from 'recharts'

export default function TechnicalIndicators({ chartData }) {
  if (!chartData || chartData.length === 0) return null

  return (
    <div className="bg-slate-800 rounded-xl p-6 border border-slate-700 space-y-8">
      <h2 className="text-xl font-semibold text-white">Technical Indicators</h2>
      <RSIChart chartData={chartData} />
      <MACDChart chartData={chartData} />
    </div>
  )
}
