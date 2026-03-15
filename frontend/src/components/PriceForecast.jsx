export default function PriceForecast({ forecast }) {
  if (!forecast || forecast.error) return null

  const { current_price, annualized_volatility, forecasts } = forecast

  return (
    <div className="bg-black border border-neutral-700 rounded-xl p-6">
      <h2 className="text-xl font-bold text-white mb-1">Price Forecast</h2>
      <p className="text-neutral-500 text-sm mb-6">
        Based on {annualized_volatility}% annualized volatility | Current: ${current_price}
      </p>

      {/* Probability cards for each timeframe */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        {forecasts.map((f) => (
          <div key={f.timeframe} className="bg-neutral-900 border border-neutral-800 rounded-lg p-4">
            <p className="text-neutral-400 text-sm font-medium mb-3">{f.timeframe}</p>

            {/* Up/Down probability bars */}
            <div className="space-y-2 mb-4">
              <div className="flex items-center justify-between">
                <span className="text-green-500 font-bold text-lg">{f.prob_up}%</span>
                <span className="text-neutral-500 text-xs">chance UP</span>
              </div>
              <div className="h-2 bg-neutral-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full"
                  style={{ width: `${f.prob_up}%`, backgroundColor: '#22c55e' }}
                />
              </div>

              <div className="flex items-center justify-between mt-2">
                <span className="text-red-500 font-bold text-lg">{f.prob_down}%</span>
                <span className="text-neutral-500 text-xs">chance DOWN</span>
              </div>
              <div className="h-2 bg-neutral-800 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full"
                  style={{ width: `${f.prob_down}%`, backgroundColor: '#ef4444' }}
                />
              </div>
            </div>

            {/* Price targets */}
            <div className="border-t border-neutral-800 pt-3 space-y-1">
              <div className="flex justify-between text-sm">
                <span className="text-neutral-500">Bull</span>
                <span className="text-green-500 font-mono">
                  ${f.targets.bull.price} ({f.targets.bull.pct > 0 ? '+' : ''}{f.targets.bull.pct}%)
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-neutral-500">Base</span>
                <span className="text-white font-mono">
                  ${f.targets.base.price} ({f.targets.base.pct > 0 ? '+' : ''}{f.targets.base.pct}%)
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-neutral-500">Bear</span>
                <span className="text-red-500 font-mono">
                  ${f.targets.bear.price} ({f.targets.bear.pct > 0 ? '+' : ''}{f.targets.bear.pct}%)
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Probability breakdown table */}
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-4">
        <p className="text-neutral-400 text-sm font-medium mb-3">Probability of Moving By</p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-neutral-500 border-b border-neutral-800">
                <th className="text-left py-2 pr-4">Move</th>
                {forecasts.map((f) => (
                  <th key={f.timeframe} className="text-right py-2 px-2">{f.timeframe}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {['+5%', '+10%', '+15%'].map((key) => (
                <tr key={key} className="border-b border-neutral-800/50">
                  <td className="py-2 pr-4 text-green-500 font-mono">{key}</td>
                  {forecasts.map((f) => (
                    <td key={f.timeframe} className="text-right py-2 px-2 text-white font-mono">
                      {f.prob_up_by[key]}%
                    </td>
                  ))}
                </tr>
              ))}
              {['-5%', '-10%', '-15%'].map((key) => (
                <tr key={key} className="border-b border-neutral-800/50">
                  <td className="py-2 pr-4 text-red-500 font-mono">{key}</td>
                  {forecasts.map((f) => (
                    <td key={f.timeframe} className="text-right py-2 px-2 text-white font-mono">
                      {f.prob_down_by[key]}%
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-neutral-600 text-xs mt-4 italic">
        Probabilities calculated using historical volatility and log-normal distribution model.
        Based on real historical price data from Yahoo Finance. Not financial advice.
      </p>
    </div>
  )
}
