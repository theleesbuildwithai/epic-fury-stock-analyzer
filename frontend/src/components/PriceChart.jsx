import {
  ResponsiveContainer, ComposedChart, Area, Line, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
} from 'recharts'

export default function PriceChart({ chartData }) {
  if (!chartData || chartData.length === 0) return null

  // Show every Nth label so x-axis isn't crowded
  const interval = Math.floor(chartData.length / 8)

  return (
    <div className="bg-slate-800 rounded-xl p-6 border border-slate-700">
      <h2 className="text-xl font-semibold text-white mb-4">Price Chart</h2>

      <ResponsiveContainer width="100%" height={400}>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis
            dataKey="date"
            stroke="#64748b"
            tick={{ fontSize: 12 }}
            interval={interval}
            tickFormatter={(d) => d.slice(5)} // Show MM-DD
          />
          <YAxis stroke="#64748b" tick={{ fontSize: 12 }} domain={['auto', 'auto']} />
          <Tooltip
            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '8px' }}
            labelStyle={{ color: '#e2e8f0' }}
            itemStyle={{ color: '#e2e8f0' }}
          />
          <Legend />

          {/* Bollinger Bands */}
          <Area
            type="monotone" dataKey="bb_upper" stroke="none" fill="#3b82f6" fillOpacity={0.05}
            name="BB Upper" dot={false}
          />
          <Area
            type="monotone" dataKey="bb_lower" stroke="none" fill="#3b82f6" fillOpacity={0.05}
            name="BB Lower" dot={false}
          />

          {/* Price */}
          <Line
            type="monotone" dataKey="close" stroke="#f97316" strokeWidth={2}
            name="Price" dot={false}
          />

          {/* Moving Averages */}
          <Line
            type="monotone" dataKey="sma_20" stroke="#22d3ee" strokeWidth={1}
            strokeDasharray="4 2" name="SMA 20" dot={false}
          />
          <Line
            type="monotone" dataKey="sma_50" stroke="#a78bfa" strokeWidth={1}
            strokeDasharray="4 2" name="SMA 50" dot={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
