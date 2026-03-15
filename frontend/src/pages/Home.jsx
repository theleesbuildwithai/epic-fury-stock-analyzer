import { useState } from 'react'
import TickerSearch from '../components/TickerSearch'
import AnalysisDashboard from '../components/AnalysisDashboard'

export default function Home() {
  const [analysisData, setAnalysisData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleAnalyze = async (ticker) => {
    setLoading(true)
    setError(null)
    setAnalysisData(null)

    try {
      const res = await fetch(`/api/analyze/${ticker}`)
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed to analyze stock')
      }
      const data = await res.json()
      setAnalysisData(data)
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
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-4 border-slate-600 border-t-orange-500"></div>
          <p className="text-slate-400 mt-4">Fetching real data and running analysis...</p>
        </div>
      )}

      {analysisData && <AnalysisDashboard data={analysisData} />}
    </div>
  )
}
