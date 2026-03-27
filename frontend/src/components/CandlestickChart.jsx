import { useState, useEffect, useMemo } from 'react'
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Area
} from 'recharts'

const PERIODS = [
  { label: '1M', value: '1mo' },
  { label: '3M', value: '3mo' },
  { label: '6M', value: '6mo' },
  { label: '1Y', value: '1y' },
  { label: '2Y', value: '2y' },
  { label: '5Y', value: '5y' },
]

const CHART_TYPES = [
  { label: 'Candle', value: 'candle' },
  { label: 'Line', value: 'line' },
  { label: 'Area', value: 'area' },
  { label: 'OHLC', value: 'ohlc' },
]

const OVERLAYS = [
  { label: 'SMA 20', key: 'sma_20' },
  { label: 'SMA 50', key: 'sma_50' },
  { label: 'SMA 200', key: 'sma_200' },
  { label: 'EMA 12', key: 'ema_12' },
  { label: 'Bollinger', key: 'bollinger' },
]

export default function CandlestickChart({ ticker, chartData: initialData }) {
  const [period, setPeriod] = useState('1y')
  const [chartType, setChartType] = useState('candle')
  const [activeOverlays, setActiveOverlays] = useState(['sma_20'])
  const [chartData, setChartData] = useState(initialData || [])
  const [loading, setLoading] = useState(false)
  const [hoveredData, setHoveredData] = useState(null)

  // Fetch chart data when period changes
  useEffect(() => {
    if (!ticker) return
    const fetchData = async () => {
      setLoading(true)
      try {
        const res = await fetch(`/api/chart-data/${ticker}?period=${period}`)
        const data = await res.json()
        if (data.chart_data) {
          setChartData(data.chart_data)
        }
      } catch {
        // Fall back to initial data
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [ticker, period])

  // Use initial data if available and we haven't fetched yet
  useEffect(() => {
    if (initialData && initialData.length > 0 && chartData.length === 0) {
      setChartData(initialData)
    }
  }, [initialData])

  const toggleOverlay = (key) => {
    setActiveOverlays(prev =>
      prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]
    )
  }

  // Process data for candlestick rendering
  const processedData = useMemo(() => {
    if (!chartData || chartData.length === 0) return []

    return chartData.map((d, i) => {
      const isUp = (d.close || 0) >= (d.open || d.close || 0)
      return {
        ...d,
        date: d.date,
        // For candlestick body
        candleOpen: d.open || d.close,
        candleClose: d.close,
        candleHigh: d.high || d.close,
        candleLow: d.low || d.close,
        // Body for stacked bar representation
        bodyBottom: Math.min(d.open || d.close, d.close),
        bodyTop: Math.max(d.open || d.close, d.close),
        bodyHeight: Math.abs((d.close) - (d.open || d.close)),
        isUp,
        // Volume
        volume: d.volume || 0,
        // Color
        fill: isUp ? '#22c55e' : '#ef4444',
      }
    })
  }, [chartData])

  // Price range for Y-axis
  const priceRange = useMemo(() => {
    if (!processedData.length) return [0, 100]
    const prices = processedData.flatMap(d => [d.candleHigh, d.candleLow]).filter(Boolean)
    const min = Math.min(...prices) * 0.98
    const max = Math.max(...prices) * 1.02
    return [min, max]
  }, [processedData])

  // Volume range
  const maxVolume = useMemo(() => {
    if (!processedData.length) return 1
    return Math.max(...processedData.map(d => d.volume || 0))
  }, [processedData])

  const latestPrice = processedData.length > 0 ? processedData[processedData.length - 1] : null

  // Custom candlestick shape
  const CandleShape = (props) => {
    const { x, y, width, height, payload } = props
    if (!payload) return null
    const { candleOpen, candleClose, candleHigh, candleLow, isUp } = payload
    const color = isUp ? '#22c55e' : '#ef4444'

    // Calculate pixel positions
    const chartHeight = 300
    const yScale = chartHeight / (priceRange[1] - priceRange[0])

    const openY = (priceRange[1] - candleOpen) * yScale
    const closeY = (priceRange[1] - candleClose) * yScale
    const highY = (priceRange[1] - candleHigh) * yScale
    const lowY = (priceRange[1] - candleLow) * yScale

    const bodyTop = Math.min(openY, closeY)
    const bodyHeight = Math.max(1, Math.abs(closeY - openY))
    const candleWidth = Math.max(1, Math.min(width * 0.7, 8))

    return (
      <g>
        {/* Wick */}
        <line
          x1={x + width / 2} y1={highY}
          x2={x + width / 2} y2={lowY}
          stroke={color} strokeWidth={1}
        />
        {/* Body */}
        <rect
          x={x + (width - candleWidth) / 2}
          y={bodyTop}
          width={candleWidth}
          height={bodyHeight}
          fill={isUp ? color : color}
          stroke={color}
          strokeWidth={0.5}
        />
      </g>
    )
  }

  // Format date for display
  const formatDate = (date) => {
    if (!date) return ''
    const d = new Date(date)
    if (period === '1mo' || period === '3mo') {
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    }
    return d.toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
  }

  // Custom tooltip
  const CustomTooltip = ({ active, payload }) => {
    if (!active || !payload || !payload[0]) return null
    const d = payload[0].payload
    if (!d) return null

    return (
      <div className="bg-neutral-900 border border-neutral-700 rounded-lg p-3 shadow-xl text-xs">
        <div className="text-neutral-400 mb-1">{d.date}</div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1">
          <span className="text-neutral-500">Open</span>
          <span className="text-white font-mono">${(d.open || d.close)?.toFixed(2)}</span>
          <span className="text-neutral-500">High</span>
          <span className="text-white font-mono">${d.candleHigh?.toFixed(2)}</span>
          <span className="text-neutral-500">Low</span>
          <span className="text-white font-mono">${d.candleLow?.toFixed(2)}</span>
          <span className="text-neutral-500">Close</span>
          <span className={`font-mono font-bold ${d.isUp ? 'text-green-400' : 'text-red-400'}`}>
            ${d.close?.toFixed(2)}
          </span>
          <span className="text-neutral-500">Volume</span>
          <span className="text-white font-mono">{(d.volume || 0).toLocaleString()}</span>
        </div>
        {d.sma_20 && activeOverlays.includes('sma_20') && (
          <div className="mt-1 text-blue-400 font-mono">SMA20: ${d.sma_20?.toFixed(2)}</div>
        )}
        {d.sma_50 && activeOverlays.includes('sma_50') && (
          <div className="text-yellow-400 font-mono">SMA50: ${d.sma_50?.toFixed(2)}</div>
        )}
      </div>
    )
  }

  if (!ticker) return null

  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-4 mb-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h3 className="text-lg font-bold text-white">{ticker} Chart</h3>
          {latestPrice && (
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-2xl font-bold text-white font-mono">
                ${latestPrice.close?.toFixed(2)}
              </span>
              {latestPrice.close && processedData.length > 1 && (
                <span className={`text-sm font-mono font-bold px-2 py-0.5 rounded ${
                  latestPrice.isUp ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'
                }`}>
                  {latestPrice.isUp ? '+' : ''}
                  {(((latestPrice.close - processedData[processedData.length - 2]?.close) /
                    processedData[processedData.length - 2]?.close) * 100).toFixed(2)}%
                </span>
              )}
            </div>
          )}
        </div>

        {/* Period selector */}
        <div className="flex items-center gap-1 bg-neutral-900 rounded-lg p-0.5">
          {PERIODS.map(p => (
            <button
              key={p.value}
              onClick={() => setPeriod(p.value)}
              className={`px-3 py-1 rounded-md text-xs font-medium transition-all ${
                period === p.value
                  ? 'bg-white text-black'
                  : 'text-neutral-400 hover:text-white'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Chart type & overlays */}
      <div className="flex flex-wrap items-center gap-3 mb-3">
        <div className="flex items-center gap-1 bg-neutral-900 rounded-lg p-0.5">
          {CHART_TYPES.map(t => (
            <button
              key={t.value}
              onClick={() => setChartType(t.value)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-all ${
                chartType === t.value
                  ? 'bg-neutral-700 text-white'
                  : 'text-neutral-500 hover:text-white'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1.5">
          {OVERLAYS.map(o => (
            <button
              key={o.key}
              onClick={() => toggleOverlay(o.key)}
              className={`px-2 py-1 rounded text-[10px] font-medium border transition-all ${
                activeOverlays.includes(o.key)
                  ? 'border-blue-500 text-blue-400 bg-blue-500/10'
                  : 'border-neutral-700 text-neutral-500 hover:text-neutral-300'
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {/* Chart */}
      {loading ? (
        <div className="h-[350px] flex items-center justify-center">
          <div className="inline-block w-8 h-8 border-2 border-neutral-700 border-t-white rounded-full animate-spin"></div>
        </div>
      ) : (
        <>
          {/* Price chart */}
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={processedData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                <XAxis
                  dataKey="date"
                  tickFormatter={formatDate}
                  tick={{ fill: '#737373', fontSize: 10 }}
                  tickLine={false}
                  axisLine={{ stroke: '#262626' }}
                  interval="preserveStartEnd"
                  minTickGap={50}
                />
                <YAxis
                  domain={priceRange}
                  tick={{ fill: '#737373', fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={v => `$${v.toFixed(0)}`}
                  width={55}
                />
                <Tooltip content={<CustomTooltip />} />

                {/* Bollinger Bands */}
                {activeOverlays.includes('bollinger') && (
                  <>
                    <Area
                      dataKey="bb_upper"
                      stroke="none"
                      fill="#6366f1"
                      fillOpacity={0.05}
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line dataKey="bb_upper" stroke="#6366f1" strokeWidth={1} dot={false}
                      strokeDasharray="4 2" opacity={0.5} isAnimationActive={false} />
                    <Line dataKey="bb_lower" stroke="#6366f1" strokeWidth={1} dot={false}
                      strokeDasharray="4 2" opacity={0.5} isAnimationActive={false} />
                    <Line dataKey="bb_middle" stroke="#6366f1" strokeWidth={1} dot={false}
                      opacity={0.3} isAnimationActive={false} />
                  </>
                )}

                {/* Main chart type */}
                {chartType === 'line' && (
                  <Line
                    dataKey="close"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                )}
                {chartType === 'area' && (
                  <Area
                    dataKey="close"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    fill="#3b82f6"
                    fillOpacity={0.1}
                    dot={false}
                    isAnimationActive={false}
                  />
                )}
                {(chartType === 'candle' || chartType === 'ohlc') && (
                  <Bar
                    dataKey="close"
                    shape={<CandleShape />}
                    isAnimationActive={false}
                  />
                )}

                {/* Moving average overlays */}
                {activeOverlays.includes('sma_20') && (
                  <Line dataKey="sma_20" stroke="#3b82f6" strokeWidth={1.5} dot={false}
                    isAnimationActive={false} connectNulls />
                )}
                {activeOverlays.includes('sma_50') && (
                  <Line dataKey="sma_50" stroke="#eab308" strokeWidth={1.5} dot={false}
                    isAnimationActive={false} connectNulls />
                )}
                {activeOverlays.includes('sma_200') && (
                  <Line dataKey="sma_200" stroke="#ef4444" strokeWidth={1.5} dot={false}
                    isAnimationActive={false} connectNulls />
                )}
                {activeOverlays.includes('ema_12') && (
                  <Line dataKey="ema_12" stroke="#a855f7" strokeWidth={1.5} dot={false}
                    isAnimationActive={false} connectNulls />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {/* Volume chart */}
          <div className="h-[80px] mt-1">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={processedData} margin={{ top: 0, right: 10, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" hide />
                <YAxis hide domain={[0, maxVolume * 2]} />
                <Bar
                  dataKey="volume"
                  isAnimationActive={false}
                  shape={(props) => {
                    const { x, y, width, height, payload } = props
                    return (
                      <rect
                        x={x}
                        y={y}
                        width={Math.max(1, width * 0.7)}
                        height={height}
                        fill={payload?.isUp ? '#22c55e40' : '#ef444440'}
                      />
                    )
                  }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      {/* Legend */}
      <div className="flex flex-wrap gap-3 mt-2 text-[10px] text-neutral-500">
        {activeOverlays.includes('sma_20') && (
          <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-blue-500 inline-block"></span> SMA 20</span>
        )}
        {activeOverlays.includes('sma_50') && (
          <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-yellow-500 inline-block"></span> SMA 50</span>
        )}
        {activeOverlays.includes('sma_200') && (
          <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-red-500 inline-block"></span> SMA 200</span>
        )}
        {activeOverlays.includes('ema_12') && (
          <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-purple-500 inline-block"></span> EMA 12</span>
        )}
        {activeOverlays.includes('bollinger') && (
          <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-indigo-500 inline-block"></span> Bollinger Bands</span>
        )}
      </div>
    </div>
  )
}
