import { useState } from 'react'
import TickerSearch from '../components/TickerSearch'
import AnalysisDashboard from '../components/AnalysisDashboard'

export default function Home() {
  const [analysisData, setAnalysisData] = useState(null)
  const [quantSignal, setQuantSignal] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleAnalyze = async (ticker) => {
    setLoading(true)
    setError(null)
    setAnalysisData(null)
    setQuantSignal(null)

    try {
      // Fetch analyze + quant-picks in parallel.
      // quant-picks reads only in-memory cache — zero external API calls.
      const [res, qRes] = await Promise.all([
        fetch(`/api/analyze/${ticker}`),
        fetch(`/api/quant-picks`),
      ])
      if (!res.ok) {
        let errMsg = 'Failed to analyze stock'
        try { const err = await res.json(); errMsg = err.detail || errMsg } catch {}
        throw new Error(errMsg)
      }
      const data = await res.json()
      setAnalysisData(data)

      // Wire quant HF signal if ticker appears in the queue
      if (qRes.ok) {
        const qJson = await qRes.json()
        const allPicks = [...(qJson.long_picks || []), ...(qJson.short_picks || [])]
        const qp = allPicks.find(p => p.ticker === ticker?.toUpperCase())
        if (qp) setQuantSignal({
          direction: qp.direction,
          confidence: qp.confidence,
          composite_score: qp.composite_score,
          factors: qp.factors,
        })
      }
    } catch (err) {
      setError(err.message)
    }
    setLoading(false)
  }

  return (
    <div>
      <div className="max-w-4xl mx-auto px-4">
        <TickerSearch onAnalyze={handleAnalyze} loading={loading} />
      </div>

      {error && (
        <div className="max-w-2xl mx-auto px-4 mb-8">
          <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300">
            {error}
          </div>
        </div>
      )}

      {loading && (
        <div className="text-center py-20">
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-4 border-neutral-600 border-t-green-500"></div>
          <p className="text-neutral-400 mt-4">Fetching real data and running analysis...</p>
        </div>
      )}

      {analysisData && <AnalysisDashboard data={analysisData} quantSignal={quantSignal} />}
    </div>
  )
}
