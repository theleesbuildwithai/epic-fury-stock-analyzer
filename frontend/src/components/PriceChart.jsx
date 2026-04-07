import {
  ResponsiveContainer, ComposedChart, Area, Line, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
} from 'recharts'

export default function PriceChart({ chartData }) {
  if (!chartData || chartData.length === 0) return null

  // Show every Nth label so x-axis isn't crowded
  const interval = Math.floor(chartData.length / 8)

  return (
    <div className="bg-neutral-800 rounded-xl p-6 border border-neutral-700">
      <h2 className="text-xl font-semibold text-white mb-4">Price Chart</h2>

      <ResponsiveContainer width="100%" height={400}>
        <ComposedChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#404040" />
          <XAxis
            dataKey="date"
            stroke="#737373"
            tick={{ fontSize: 12 }}
            interval={interval}
            tickFormatter={(d) => d.slice(5)} // Show MM-DD
          />
          <YAxis stroke="#737373" tick={{ fontSize: 12 }} domain={['auto', 'auto']} />
          <Tooltip
            contentStyle={{ backgroundColor: '#171717', border: '1px solid #525252', borderRadius: '8px' }}
            labelStyle={{ color: '#e5e5e5' }}
            itemStyle={{ color: '#e5e5e5' }}
          />
          <Legend />

          {/* Bollinger Bands */}
          <Area
            type="monotone" dataKey="bb_upper" stroke="none" fill="#a3a3a3" fillOpacity={0.05}
            name="BB Upper" dot={false}
          />
          <Area
            type="monotone" dataKey="bb_lower" stroke="none" fill="#a3a3a3" fillOpacity={0.05}
            name="BB Lower" dot={false}
          />

          {/* Price */}
          <Line
            type="monotone" dataKey="close" stroke="#ffffff" strokeWidth={2}
            name="Price" dot={false}
          />

          {/* Moving Averages */}
          <Line
            type="monotone" dataKey="sma_20" stroke="#22c55e" strokeWidth={1}
            strokeDasharray="4 2" name="SMA 20" dot={false}
          />
          <Line
            type="monotone" dataKey="sma_50" stroke="#a3a3a3" strokeWidth={1}
            strokeDasharray="4 2" name="SMA 50" dot={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
