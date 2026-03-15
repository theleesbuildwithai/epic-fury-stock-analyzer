export default function HowItWorks() {
  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <h1 className="text-4xl font-bold text-white mb-2">How It Works</h1>
      <p className="text-neutral-400 text-lg mb-8">
        Everything under the hood, explained in plain English.
      </p>

      {/* Architecture Diagram */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">The Big Picture</h2>
        <div className="bg-black rounded-xl p-8 border border-neutral-700">
          <p className="text-neutral-300 mb-4">Think of our app like making a PB&J sandwich:</p>
          <div className="space-y-4 font-mono text-sm">
            <div>
              <p className="text-white font-bold">Frontend (The Bread)</p>
              <p className="text-neutral-400">What you see and touch — the website, buttons, charts</p>
            </div>
            <div className="text-neutral-600 pl-6">|</div>
            <div>
              <p className="text-white font-bold">Backend API (The Peanut Butter)</p>
              <p className="text-neutral-400">The logic — fetches data, runs math, makes predictions</p>
            </div>
            <div className="text-neutral-600 pl-6">|</div>
            <div>
              <p className="text-white font-bold">Database (The Jelly)</p>
              <p className="text-neutral-400">Remembers your predictions so we can track accuracy</p>
            </div>
            <div className="text-neutral-600 pl-6">|</div>
            <div>
              <p className="text-white font-bold">Yahoo Finance (The Store)</p>
              <p className="text-neutral-400">Where we get the real stock prices — nothing is made up</p>
            </div>
          </div>
        </div>
      </section>

      {/* Tech Stack */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">Tech Stack</h2>
        <div className="space-y-4">
          <TechCard name="React" what="A JavaScript library for building websites." why="Most popular tool for modern websites. Instagram, Netflix, and Discord use it." alternative="Vue.js or plain HTML" />
          <TechCard name="Tailwind CSS" what="A styling tool that makes the website look good." why="Faster than writing CSS from scratch." alternative="Regular CSS or Bootstrap" />
          <TechCard name="FastAPI (Python)" what="The engine that runs our backend server." why="Fastest Python web framework, great for data analysis." alternative="Express.js or Django" />
          <TechCard name="Yahoo Finance (yfinance)" what="Free service for real stock prices and company info." why="Free, reliable, same data as finance.yahoo.com." alternative="Alpha Vantage or Polygon.io (paid)" />
          <TechCard name="scipy" what="Scientific computing library for probability calculations." why="Industry-standard for statistical modeling — our price forecasts use log-normal distributions." alternative="Custom math implementations" />
          <TechCard name="SQLite" what="Lightweight database that stores predictions in one file." why="Simple, fast, zero setup." alternative="PostgreSQL or MongoDB" />
          <TechCard name="Recharts" what="React library for interactive charts." why="Designed for React, professional charts out of the box." alternative="Chart.js or D3.js" />
          <TechCard name="Docker + AWS App Runner" what="Packages and deploys the app to the cloud." why="Guarantees it works the same everywhere. App Runner handles scaling." alternative="EC2 or ECS" />
        </div>
      </section>

      {/* Technical Indicators Explained */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">Technical Indicators Explained</h2>
        <div className="space-y-3">
          <IndicatorCard name="RSI (Relative Strength Index)" explanation="Measures if a stock is overbought or oversold. Above 70 = might slow down. Below 30 = might bounce back. Uses Wilder's smoothing method (industry standard)." />
          <IndicatorCard name="MACD" explanation="Shows momentum shifts by comparing fast (12-day) and slow (26-day) averages. Crossing above = bullish. Crossing below = bearish." />
          <IndicatorCard name="Bollinger Bands" explanation="Creates a channel around price. Hitting top band = might be too high. Bottom = too low. Bands squeezing = big move coming." />
          <IndicatorCard name="Moving Averages (SMA/EMA)" explanation="Smooths daily swings to show the real trend. 20-day = short term, 50-day = medium term." />
          <IndicatorCard name="Price Forecast" explanation="Uses historical volatility and log-normal distribution to calculate probability of the stock moving up or down over 7, 14, and 30 days. Based on real math, not guessing." />
          <IndicatorCard name="Support & Resistance" explanation="Price levels where the stock tends to bounce. Support = floor. Resistance = ceiling." />
        </div>
      </section>

      <p className="text-neutral-600 text-sm text-center">
        Built with React, FastAPI, and Yahoo Finance data. Deployed on AWS App Runner.
      </p>
    </div>
  )
}

function TechCard({ name, what, why, alternative }) {
  return (
    <div className="bg-black rounded-xl p-5 border border-neutral-700">
      <h3 className="text-lg font-semibold text-white mb-2">{name}</h3>
      <p className="text-neutral-300 text-sm mb-1"><strong className="text-white">What:</strong> {what}</p>
      <p className="text-neutral-300 text-sm mb-1"><strong className="text-white">Why:</strong> {why}</p>
      <p className="text-neutral-400 text-sm"><strong className="text-neutral-300">Alternative:</strong> {alternative}</p>
    </div>
  )
}

function IndicatorCard({ name, explanation }) {
  return (
    <div className="bg-black rounded-lg p-4 border border-neutral-700">
      <p className="text-white font-medium mb-1">{name}</p>
      <p className="text-neutral-300 text-sm">{explanation}</p>
    </div>
  )
}
