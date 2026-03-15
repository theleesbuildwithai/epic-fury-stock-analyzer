import { useState } from 'react'

export default function PriceForecast({ forecast }) {
  if (!forecast || forecast.error) return null

  const { current_price, annualized_volatility, recent_volatility, forecasts, data_points_used } = forecast

  // Group timeframes: Short-term (7, 14, 30) and Long-term (60, 90, 180)
  const [activeTab, setActiveTab] = useState(0)

  const tabs = forecasts.map((f) => ({ label: f.timeframe, data: f }))

  const active = tabs[activeTab]?.data
  if (!active) return null

  // All threshold keys across all timeframes
  const upKeys = Object.keys(active.prob_up_by)
  const downKeys = Object.keys(active.prob_down_by)

  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-1">
        <h2 className="text-xl font-bold text-white">Price Forecast</h2>
        <div className="flex items-center gap-3 mt-2 sm:mt-0">
          <span className="text-neutral-500 text-xs">
            Vol: {annualized_volatility}% (full) / {recent_volatility}% (recent)
          </span>
          <span className="text-neutral-600 text-xs">|</span>
          <span className="text-neutral-500 text-xs">{data_points_used} days of data</span>
        </div>
      </div>
      <p className="text-neutral-500 text-sm mb-5">
        Current: ${current_price} | Select a timeframe below
      </p>

      {/* Timeframe tabs */}
      <div className="flex flex-wrap gap-2 mb-6">
        {tabs.map((tab, i) => (
          <button
            key={tab.label}
            onClick={() => setActiveTab(i)}
            className={`px-4 py-2 text-sm font-medium rounded-lg border transition-colors ${
              activeTab === i
                ? 'bg-white text-black border-white'
                : 'bg-neutral-900 text-neutral-400 border-neutral-700 hover:border-neutral-500 hover:text-white'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Main probability display */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
        {/* Left: Up/Down probability */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-5">
          <p className="text-neutral-400 text-sm font-medium mb-4">
            Probability in {active.timeframe}
          </p>

          <div className="space-y-4">
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-green-500 font-bold text-2xl">{active.prob_up}%</span>
                <span className="text-neutral-500 text-sm">chance UP</span>
              </div>
              <div className="h-3 bg-neutral-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-500"
                  style={{ width: `${active.prob_up}%`, backgroundColor: '#22c55e' }}
                />
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-red-500 font-bold text-2xl">{active.prob_down}%</span>
                <span className="text-neutral-500 text-sm">chance DOWN</span>
              </div>
              <div className="h-3 bg-neutral-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-500"
                  style={{ width: `${active.prob_down}%`, backgroundColor: '#ef4444' }}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Right: Price targets */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-5">
          <p className="text-neutral-400 text-sm font-medium mb-4">
            Price Targets — {active.timeframe}
          </p>

          <div className="space-y-3">
            <div className="flex justify-between items-center py-2 border-b border-neutral-800">
              <div>
                <span className="text-neutral-500 text-sm">Bull Case</span>
                <span className="text-neutral-600 text-xs ml-2">(75th percentile)</span>
              </div>
              <span className="text-green-500 font-mono font-bold text-lg">
                ${active.targets.bull.price}
                <span className="text-sm ml-1">({active.targets.bull.pct > 0 ? '+' : ''}{active.targets.bull.pct}%)</span>
              </span>
            </div>
            <div className="flex justify-between items-center py-2 border-b border-neutral-800">
              <div>
                <span className="text-neutral-500 text-sm">Base Case</span>
                <span className="text-neutral-600 text-xs ml-2">(median)</span>
              </div>
              <span className="text-white font-mono font-bold text-lg">
                ${active.targets.base.price}
                <span className="text-sm ml-1">({active.targets.base.pct > 0 ? '+' : ''}{active.targets.base.pct}%)</span>
              </span>
            </div>
            <div className="flex justify-between items-center py-2">
              <div>
                <span className="text-neutral-500 text-sm">Bear Case</span>
                <span className="text-neutral-600 text-xs ml-2">(25th percentile)</span>
              </div>
              <span className="text-red-500 font-mono font-bold text-lg">
                ${active.targets.bear.price}
                <span className="text-sm ml-1">({active.targets.bear.pct > 0 ? '+' : ''}{active.targets.bear.pct}%)</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Probability breakdown */}
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-5">
        <p className="text-neutral-400 text-sm font-medium mb-3">
          Probability of Moving By — {active.timeframe}
        </p>
        <div className="grid grid-cols-2 gap-4">
          {/* Upside probabilities */}
          <div>
            <p className="text-green-500 text-xs font-medium mb-2">UPSIDE</p>
            <div className="space-y-2">
              {upKeys.map((key) => (
                <div key={key} className="flex items-center justify-between">
                  <span className="text-neutral-400 text-sm font-mono">{key}</span>
                  <div className="flex items-center gap-2">
                    <div className="w-20 h-2 bg-neutral-800 rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.min(100, active.prob_up_by[key])}%`, backgroundColor: '#22c55e' }}
                      />
                    </div>
                    <span className="text-white text-sm font-mono w-12 text-right">{active.prob_up_by[key]}%</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
          {/* Downside probabilities */}
          <div>
            <p className="text-red-500 text-xs font-medium mb-2">DOWNSIDE</p>
            <div className="space-y-2">
              {downKeys.map((key) => (
                <div key={key} className="flex items-center justify-between">
                  <span className="text-neutral-400 text-sm font-mono">{key}</span>
                  <div className="flex items-center gap-2">
                    <div className="w-20 h-2 bg-neutral-800 rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.min(100, active.prob_down_by[key])}%`, backgroundColor: '#ef4444' }}
                      />
                    </div>
                    <span className="text-white text-sm font-mono w-12 text-right">{active.prob_down_by[key]}%</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Quick comparison strip */}
      <div className="mt-4 bg-neutral-900 border border-neutral-800 rounded-lg p-4">
        <p className="text-neutral-500 text-xs font-medium mb-2">QUICK COMPARE — Probability of Going Up</p>
        <div className="flex items-center gap-3 flex-wrap">
          {forecasts.map((f) => (
            <div key={f.timeframe} className="flex items-center gap-1.5">
              <span className="text-neutral-400 text-xs">{f.timeframe}:</span>
              <span className={`font-mono font-bold text-sm ${f.prob_up >= 50 ? 'text-green-500' : 'text-red-500'}`}>
                {f.prob_up}%
              </span>
            </div>
          ))}
        </div>
      </div>

      <p className="text-neutral-600 text-xs mt-4 italic">
        Probabilities use log-normal distribution with trend decay, mean reversion, and dual volatility.
        Short-term forecasts weight recent market conditions; long-term forecasts blend toward historical averages.
        Based on real Yahoo Finance data. Not financial advice.
      </p>
    </div>
  )
}
