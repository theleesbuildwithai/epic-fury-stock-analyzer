import { useState, useEffect } from 'react'
import PriceChart from './PriceChart'
import TechnicalIndicators from './TechnicalIndicators'
import RiskScore from './RiskScore'
import KeyStats from './KeyStats'
import PriceForecast from './PriceForecast'

function hasConsent() {
  return localStorage.getItem('epic_fury_cookie_consent') === 'accepted'
}

function getSavedStocks() {
  try {
    const data = localStorage.getItem('epic_fury_saved_stocks')
    return data ? JSON.parse(data) : []
  } catch {
    return []
  }
}

function saveStockToLocal(prediction) {
  if (!hasConsent()) return
  const stocks = getSavedStocks()
  const filtered = stocks.filter(s => s.ticker !== prediction.ticker)
  filtered.push({ ...prediction, saved_at: new Date().toISOString() })
  localStorage.setItem('epic_fury_saved_stocks', JSON.stringify(filtered))
}

function removeStockFromLocal(ticker) {
  const stocks = getSavedStocks()
  const filtered = stocks.filter(s => s.ticker !== ticker)
  localStorage.setItem('epic_fury_saved_stocks', JSON.stringify(filtered))
}

function isStockSavedLocally(ticker) {
  return getSavedStocks().some(s => s.ticker === ticker)
}

export default function AnalysisDashboard({ data, onSavePrediction }) {
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [isTracked, setIsTracked] = useState(false)

  useEffect(() => {
    if (data?.info?.ticker) {
      setIsTracked(isStockSavedLocally(data.info.ticker))
      setSaved(false)
    }
  }, [data?.info?.ticker])

  if (!data) return null

  const handleSavePrediction = async () => {
    setSaving(true)
    try {
      const prediction = {
        ticker: data.info.ticker,
        predicted_direction: data.signal.direction,
        confidence_score: data.signal.confidence,
        entry_price: data.latest.price,
        target_price: null,
        check_after_days: 30,
        notes: `Auto-generated from analysis. RSI: ${data.latest.rsi}, Trend: ${data.trend.direction}`,
      }

      const res = await fetch('/api/predictions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prediction),
      })
      if (res.ok) {
        setSaved(true)
        setIsTracked(true)
        saveStockToLocal(prediction)
        setTimeout(() => setSaved(false), 3000)
      }
    } catch (err) {
      console.error('Failed to save prediction:', err)
    }
    setSaving(false)
  }

  const handleRemovePrediction = () => {
    removeStockFromLocal(data.info.ticker)
    setIsTracked(false)
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-8 space-y-6">
      {/* Top: Key Stats */}
      <KeyStats
        info={data.info}
        latest={data.latest}
        supportResistance={data.support_resistance}
      />

      {/* Price Forecast — the main prediction section */}
      <PriceForecast forecast={data.forecast} />

      {/* Middle: Signal + Risk side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <RiskScore risk={data.risk} signal={data.signal} />

          {/* Save / Remove Prediction Button */}
          {isTracked ? (
            <div className="mt-4 space-y-2">
              <div className="w-full py-3 px-4 bg-neutral-900 text-green-500 font-medium rounded-lg
                              border border-green-500/30 text-center text-sm">
                Tracking This Stock
              </div>
              <button
                onClick={handleRemovePrediction}
                className="w-full py-2 px-4 text-neutral-500 hover:text-red-500 text-xs
                           hover:bg-neutral-900 rounded-lg transition-colors"
              >
                Remove from tracker
              </button>
            </div>
          ) : (
            <>
              <button
                onClick={handleSavePrediction}
                disabled={saving || saved}
                className="mt-4 w-full py-3 px-4 bg-neutral-800 hover:bg-neutral-700 disabled:bg-neutral-900
                           text-white font-medium rounded-lg transition-colors border border-neutral-700"
              >
                {saved ? 'Prediction Saved' : saving ? 'Saving...' : 'Save This Prediction'}
              </button>
              <p className="text-neutral-600 text-xs mt-2 text-center">
                Track this prediction on the Performance page
              </p>
            </>
          )}
        </div>

        <div className="lg:col-span-2">
          {/* Price Chart */}
          <PriceChart chartData={data.chart_data} />
        </div>
      </div>

      {/* Bottom: Technical Indicators */}
      <TechnicalIndicators chartData={data.chart_data} />

      {/* Trend Summary */}
      <div className="bg-black rounded-xl p-6 border border-neutral-700">
        <h2 className="text-xl font-semibold text-white mb-3">Trend Analysis</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <p className="text-neutral-400 text-sm">Direction</p>
            <p className={`text-lg font-medium ${
              data.trend.direction.includes('bullish') ? 'text-green-500' :
              data.trend.direction.includes('bearish') ? 'text-red-500' : 'text-white'
            }`}>
              {data.trend.direction.replace('_', ' ')}
            </p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Strength</p>
            <p className="text-lg font-medium text-white">{data.trend.strength}%</p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Price vs 20-day MA</p>
            <p className={`text-lg font-medium ${data.trend.price_vs_sma20 >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {data.trend.price_vs_sma20 >= 0 ? '+' : ''}{data.trend.price_vs_sma20}%
            </p>
          </div>
          <div>
            <p className="text-neutral-400 text-sm">Price vs 50-day MA</p>
            <p className={`text-lg font-medium ${data.trend.price_vs_sma50 >= 0 ? 'text-green-500' : 'text-red-500'}`}>
              {data.trend.price_vs_sma50 >= 0 ? '+' : ''}{data.trend.price_vs_sma50}%
            </p>
          </div>
        </div>

        {/* Volume Analysis */}
        {data.volume_analysis && (
          <div className="mt-4 pt-4 border-t border-neutral-700">
            <h3 className="text-sm font-medium text-neutral-400 mb-2">Volume Analysis</h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <p className="text-neutral-500 text-xs">Volume Trend</p>
                <p className="text-white">{data.volume_analysis.volume_trend}</p>
              </div>
              <div>
                <p className="text-neutral-500 text-xs">Volume Ratio</p>
                <p className="text-white">{data.volume_analysis.volume_ratio}x avg</p>
              </div>
              <div>
                <p className="text-neutral-500 text-xs">Unusual Volume?</p>
                <p className={data.volume_analysis.unusual_volume ? 'text-red-500' : 'text-neutral-400'}>
                  {data.volume_analysis.unusual_volume ? 'Yes' : 'No'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
