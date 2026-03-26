import { useState, useMemo } from 'react'

export default function CompoundInterestCalc() {
  const [principal, setPrincipal] = useState('0')
  const [years, setYears] = useState('0')
  const [rate, setRate] = useState('0')
  const [contribution, setContribution] = useState('0')
  const [frequency, setFrequency] = useState('monthly')

  const clearOnFocus = (setter) => (e) => {
    if (e.target.value === '0') {
      setter('')
    }
  }

  const numPrincipal = Number(principal) || 0
  const numYears = Number(years) || 0
  const numRate = Number(rate) || 0
  const numContribution = Number(contribution) || 0

  const results = useMemo(() => {
    const r = numRate / 100
    const periods = frequency === 'monthly' ? 12 : 1
    const periodicRate = r / periods
    const totalPeriods = numYears * periods
    const contrib = numContribution

    // Build year-by-year data for the graph
    const yearData = []
    let balance = numPrincipal
    let totalContributions = numPrincipal

    for (let year = 0; year <= numYears; year++) {
      if (year === 0) {
        yearData.push({
          year,
          balance: numPrincipal,
          contributions: numPrincipal,
          interest: 0,
        })
        continue
      }

      for (let p = 0; p < periods; p++) {
        balance = balance * (1 + periodicRate) + contrib
        totalContributions += contrib
      }

      yearData.push({
        year,
        balance: Math.round(balance * 100) / 100,
        contributions: Math.round(totalContributions * 100) / 100,
        interest: Math.round((balance - totalContributions) * 100) / 100,
      })
    }

    const finalBalance = yearData[yearData.length - 1]?.balance || 0
    const totalContrib = yearData[yearData.length - 1]?.contributions || 0
    const totalInterest = yearData[yearData.length - 1]?.interest || 0

    return { yearData, finalBalance, totalContrib, totalInterest }
  }, [numPrincipal, numYears, numRate, numContribution, frequency])

  // Find max value for scaling the chart
  const maxBalance = Math.max(...results.yearData.map(d => d.balance), 1)

  const formatMoney = (n) => {
    if (n >= 1000000) return `$${(n / 1000000).toFixed(2)}M`
    if (n >= 1000) return `$${(n / 1000).toFixed(1)}K`
    return `$${n.toFixed(2)}`
  }

  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-6">
      <h2 className="text-xl font-bold text-white mb-1">Compound Interest Calculator</h2>
      <p className="text-neutral-500 text-sm mb-6">See how your money grows over time with compound interest</p>

      {/* Inputs */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
        <div>
          <label className="text-neutral-500 text-xs uppercase block mb-1">Initial Amount</label>
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500">$</span>
            <input
              type="number"
              value={principal}
              onChange={e => setPrincipal(e.target.value)}
              onFocus={clearOnFocus(setPrincipal)}
              placeholder="0"
              className="w-full bg-neutral-900 border border-neutral-700 rounded-lg pl-7 pr-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-white transition-colors"
            />
          </div>
        </div>
        <div>
          <label className="text-neutral-500 text-xs uppercase block mb-1">Years</label>
          <input
            type="number"
            value={years}
            onChange={e => setYears(e.target.value)}
            onFocus={clearOnFocus(setYears)}
            placeholder="0"
            className="w-full bg-neutral-900 border border-neutral-700 rounded-lg px-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-white transition-colors"
          />
        </div>
        <div>
          <label className="text-neutral-500 text-xs uppercase block mb-1">Interest Rate</label>
          <div className="relative">
            <input
              type="number"
              step="0.1"
              value={rate}
              onChange={e => setRate(e.target.value)}
              onFocus={clearOnFocus(setRate)}
              placeholder="0"
              className="w-full bg-neutral-900 border border-neutral-700 rounded-lg px-3 pr-7 py-2 text-white text-sm font-mono focus:outline-none focus:border-white transition-colors"
            />
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-neutral-500">%</span>
          </div>
        </div>
        <div>
          <label className="text-neutral-500 text-xs uppercase block mb-1">Contribution</label>
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500">$</span>
            <input
              type="number"
              value={contribution}
              onChange={e => setContribution(e.target.value)}
              onFocus={clearOnFocus(setContribution)}
              placeholder="0"
              className="w-full bg-neutral-900 border border-neutral-700 rounded-lg pl-7 pr-3 py-2 text-white text-sm font-mono focus:outline-none focus:border-white transition-colors"
            />
          </div>
        </div>
        <div>
          <label className="text-neutral-500 text-xs uppercase block mb-1">Frequency</label>
          <select
            value={frequency}
            onChange={e => setFrequency(e.target.value)}
            className="w-full bg-neutral-900 border border-neutral-700 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-white transition-colors"
          >
            <option value="monthly">Monthly</option>
            <option value="yearly">Yearly</option>
          </select>
        </div>
      </div>

      {/* Result Cards */}
      <div className="grid grid-cols-3 gap-4 mb-8">
        <div className="bg-neutral-900 rounded-lg p-4 text-center">
          <p className="text-neutral-500 text-xs uppercase mb-1">Total Balance</p>
          <p className="text-2xl font-bold text-white font-mono">{formatMoney(results.finalBalance)}</p>
        </div>
        <div className="bg-neutral-900 rounded-lg p-4 text-center">
          <p className="text-neutral-500 text-xs uppercase mb-1">Total Contributed</p>
          <p className="text-2xl font-bold text-blue-400 font-mono">{formatMoney(results.totalContrib)}</p>
        </div>
        <div className="bg-neutral-900 rounded-lg p-4 text-center">
          <p className="text-neutral-500 text-xs uppercase mb-1">Interest Earned</p>
          <p className="text-2xl font-bold text-green-500 font-mono">{formatMoney(results.totalInterest)}</p>
        </div>
      </div>

      {/* Chart */}
      <div className="bg-neutral-900 rounded-lg p-4">
        <p className="text-neutral-500 text-xs uppercase mb-4">Growth Over Time</p>
        <div className="flex items-end gap-1" style={{ height: '200px' }}>
          {results.yearData.map((d) => {
            const totalHeight = (d.balance / maxBalance) * 100
            const contribHeight = (d.contributions / maxBalance) * 100
            return (
              <div
                key={d.year}
                className="flex-1 flex flex-col items-center justify-end group relative"
                style={{ height: '100%' }}
              >
                {/* Tooltip */}
                <div className="absolute bottom-full mb-2 bg-neutral-800 border border-neutral-600 rounded-lg p-2 text-xs opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap z-10">
                  <p className="text-white font-bold">Year {d.year}</p>
                  <p className="text-neutral-300">Balance: {formatMoney(d.balance)}</p>
                  <p className="text-blue-400">Contributed: {formatMoney(d.contributions)}</p>
                  <p className="text-green-500">Interest: {formatMoney(d.interest)}</p>
                </div>
                {/* Interest portion (green) */}
                <div
                  className="w-full bg-green-500/60 rounded-t"
                  style={{ height: `${totalHeight - contribHeight}%`, minHeight: d.interest > 0 ? '1px' : '0' }}
                ></div>
                {/* Contribution portion (blue) */}
                <div
                  className="w-full bg-blue-500/60"
                  style={{ height: `${contribHeight}%`, minHeight: '2px' }}
                ></div>
                {/* Year label */}
                {(d.year % Math.max(1, Math.floor(numYears / 10)) === 0 || d.year === numYears) && (
                  <span className="text-neutral-600 text-[10px] mt-1">{d.year}</span>
                )}
              </div>
            )
          })}
        </div>
        <div className="flex items-center gap-4 mt-3 justify-center">
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 bg-blue-500/60 rounded"></div>
            <span className="text-neutral-500 text-xs">Contributions</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 bg-green-500/60 rounded"></div>
            <span className="text-neutral-500 text-xs">Interest</span>
          </div>
        </div>
      </div>
    </div>
  )
}
