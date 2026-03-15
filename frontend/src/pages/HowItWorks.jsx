export default function HowItWorks() {
  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <h1 className="text-4xl font-bold text-white mb-2">How It Works</h1>
      <p className="text-slate-400 text-lg mb-8">
        Everything under the hood, explained in plain English.
      </p>

      {/* Architecture Diagram */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">The Big Picture</h2>
        <div className="bg-slate-800 rounded-xl p-8 border border-slate-700">
          <p className="text-slate-300 mb-4">Think of our app like making a PB&J sandwich:</p>
          <div className="space-y-4 font-mono text-sm">
            <div className="flex items-center gap-3">
              <span className="text-3xl">🍞</span>
              <div>
                <p className="text-orange-400 font-bold">Frontend (The Bread)</p>
                <p className="text-slate-400">What you see and touch — the website, buttons, charts</p>
              </div>
            </div>
            <div className="text-slate-600 pl-6">↕</div>
            <div className="flex items-center gap-3">
              <span className="text-3xl">🥜</span>
              <div>
                <p className="text-amber-400 font-bold">Backend API (The Peanut Butter)</p>
                <p className="text-slate-400">The logic — fetches data, runs math, makes decisions</p>
              </div>
            </div>
            <div className="text-slate-600 pl-6">↕</div>
            <div className="flex items-center gap-3">
              <span className="text-3xl">🍇</span>
              <div>
                <p className="text-purple-400 font-bold">Database (The Jelly)</p>
                <p className="text-slate-400">Remembers your predictions so we can track accuracy</p>
              </div>
            </div>
            <div className="text-slate-600 pl-6">↕</div>
            <div className="flex items-center gap-3">
              <span className="text-3xl">📊</span>
              <div>
                <p className="text-blue-400 font-bold">Yahoo Finance (The Store)</p>
                <p className="text-slate-400">Where we get the real stock prices — nothing is made up</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Tech Stack */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">Tech Stack</h2>
        <div className="space-y-4">
          <TechCard
            name="React"
            what="A JavaScript library for building websites. It lets us create interactive pages with buttons, charts, and animations."
            why="It's the most popular tool for building modern websites. Instagram, Netflix, and Discord all use it."
            alternative="We could have used Vue.js or plain HTML, but React has the biggest community and most learning resources."
          />
          <TechCard
            name="Tailwind CSS"
            what="A styling tool that lets us make the website look good by adding classes directly to HTML elements."
            why="It's way faster than writing CSS from scratch. Instead of naming things like 'big-blue-button', we just write 'text-xl text-blue-500 px-4 py-2'."
            alternative="We could have used regular CSS or Bootstrap, but Tailwind is more flexible and doesn't force a specific look."
          />
          <TechCard
            name="FastAPI (Python)"
            what="The engine that runs our backend server. When the website asks for stock data, FastAPI handles the request."
            why="It's the fastest Python web framework, and Python is the easiest programming language to read. Plus, Python is amazing for data analysis."
            alternative="We could have used Express.js (JavaScript) or Django (heavier Python framework), but FastAPI is lighter and faster."
          />
          <TechCard
            name="Yahoo Finance API (yfinance)"
            what="A free service that gives us real stock prices, volumes, and company info straight from Yahoo Finance."
            why="It's free, reliable, and gives us the same data you see on finance.yahoo.com. Every number in our app comes from here — nothing is fake."
            alternative="We could have used Alpha Vantage or Polygon.io, but those require API keys and have strict rate limits on free tiers."
          />
          <TechCard
            name="SQLite"
            what="A lightweight database that stores our predictions in a single file. No server needed."
            why="Perfect for our use case — simple, fast, and zero setup. It's actually used in every iPhone and Android phone!"
            alternative="We could have used PostgreSQL (more powerful) or MongoDB (different structure), but SQLite is simpler and free."
          />
          <TechCard
            name="Recharts"
            what="A React library for creating beautiful, interactive charts — like the stock price chart and RSI gauge."
            why="It's specifically designed for React and is easy to customize. The charts look professional right out of the box."
            alternative="We could have used Chart.js or D3.js, but Recharts integrates perfectly with React components."
          />
          <TechCard
            name="Docker"
            what="A tool that packages our entire app (code, libraries, settings) into a 'container' — like a shipping container for software."
            why="It guarantees the app works the same everywhere. No more 'it works on my computer but not yours' problems."
            alternative="We could deploy without Docker, but then we'd have to manually install Python, Node.js, etc. on the server."
          />
          <TechCard
            name="AWS App Runner"
            what="Amazon's service that runs our Docker container in the cloud and gives us a public URL."
            why="It's dead simple — just point it at our container and it handles everything: scaling, HTTPS, load balancing."
            alternative="We could have used EC2 (full server control) or ECS (more complex containers), but App Runner is the simplest option for deployment."
          />
        </div>
      </section>

      {/* Key Tradeoffs */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">Key Tradeoffs</h2>
        <div className="space-y-3">
          <Tradeoff
            decision="We used App Runner instead of EC2"
            why="App Runner manages the server for us — we don't have to worry about updates, security patches, or scaling. The tradeoff is we have less control over the server, but for learning that's perfect."
          />
          <Tradeoff
            decision="We used SQLite instead of PostgreSQL"
            why="SQLite stores everything in one file — no database server to manage. The tradeoff is it can only handle one person writing at a time, but for a small app that's fine."
          />
          <Tradeoff
            decision="We used yfinance (free) instead of a paid data provider"
            why="Yahoo Finance data is free and reliable. The tradeoff is there's a slight delay (15-20 min) and rate limits, but for analysis (not day trading) that's acceptable."
          />
          <Tradeoff
            decision="We cache results for 5 minutes"
            why="This prevents us from hitting Yahoo Finance too often (they'd block us). The tradeoff is data might be 5 minutes old, but stock analysis doesn't need second-by-second updates."
          />
          <Tradeoff
            decision="We used Tailwind CSS instead of writing custom styles"
            why="Tailwind lets us style things 10x faster. The tradeoff is the HTML looks messier with lots of class names, but we ship faster and the app looks great."
          />
        </div>
      </section>

      {/* Technical Indicators Explained */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">Technical Indicators Explained</h2>
        <div className="space-y-3">
          <IndicatorCard
            name="RSI (Relative Strength Index)"
            explanation="Measures if a stock has been bought too much (overbought) or sold too much (oversold). Think of it like a speedometer — above 70 means the stock might be going too fast and could slow down. Below 30 means it might be oversold and could bounce back."
          />
          <IndicatorCard
            name="MACD"
            explanation="Shows when the momentum of a stock is shifting. It compares a fast average (12-day) with a slow average (26-day). When the fast one crosses above the slow one, it's like a car accelerating — bullish signal. When it crosses below, it's like braking — bearish signal."
          />
          <IndicatorCard
            name="Bollinger Bands"
            explanation="Creates a 'channel' around the stock price. When the price hits the top band, it might be too high. When it hits the bottom, it might be too low. When the bands squeeze tight together, it usually means a big move is coming — like a coiled spring."
          />
          <IndicatorCard
            name="Moving Averages (SMA/EMA)"
            explanation="Smooths out the daily price swings to show the real trend. The 20-day average shows the short-term trend, the 50-day shows medium-term. When a short average crosses above a long average, it's called a 'golden cross' — historically a bullish sign."
          />
          <IndicatorCard
            name="Support & Resistance"
            explanation="Price levels where the stock tends to 'bounce.' Support is the floor — the price keeps bouncing up from there. Resistance is the ceiling — the price keeps getting rejected there. These levels matter because lots of traders watch them and act on them."
          />
        </div>
      </section>

      {/* What You Learned */}
      <section className="mb-12">
        <h2 className="text-2xl font-semibold text-white mb-4">What You Learned By Building This</h2>
        <div className="bg-slate-800 rounded-xl p-6 border border-slate-700">
          <ul className="space-y-3 text-slate-300">
            <li className="flex gap-2"><span className="text-green-400">✓</span> How to build a full-stack web application (frontend + backend)</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How APIs work — sending requests and getting data back</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How to fetch and analyze real financial data</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> What technical indicators are and how traders use them</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How databases store and retrieve information</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How Docker containers package apps for deployment</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How cloud services (AWS) host websites for the world to see</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> How to track predictions and measure accuracy (like a scientist!)</li>
            <li className="flex gap-2"><span className="text-green-400">✓</span> Why you should never blindly trust a computer's stock predictions</li>
          </ul>
        </div>
      </section>

      <p className="text-slate-600 text-sm text-center">
        Built with React, FastAPI, and Yahoo Finance data. Deployed on AWS App Runner.
      </p>
    </div>
  )
}

function TechCard({ name, what, why, alternative }) {
  return (
    <div className="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <h3 className="text-lg font-semibold text-orange-400 mb-2">{name}</h3>
      <p className="text-slate-300 text-sm mb-2"><strong className="text-slate-200">What it is:</strong> {what}</p>
      <p className="text-slate-300 text-sm mb-2"><strong className="text-slate-200">Why we use it:</strong> {why}</p>
      <p className="text-slate-400 text-sm"><strong className="text-slate-300">What else we could use:</strong> {alternative}</p>
    </div>
  )
}

function Tradeoff({ decision, why }) {
  return (
    <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
      <p className="text-white font-medium mb-1">{decision}</p>
      <p className="text-slate-400 text-sm">{why}</p>
    </div>
  )
}

function IndicatorCard({ name, explanation }) {
  return (
    <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
      <p className="text-cyan-400 font-medium mb-1">{name}</p>
      <p className="text-slate-300 text-sm">{explanation}</p>
    </div>
  )
}
