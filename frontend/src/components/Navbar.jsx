import { Link, useLocation } from 'react-router-dom'

export default function Navbar() {
  const location = useLocation()
  const isActive = (path) => location.pathname === path

  return (
    <nav className="bg-neutral-950/90 backdrop-blur-md border-b border-neutral-800/50 sticky top-0 z-40 nav-accent">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-14">
          <Link to="/" className="flex items-center space-x-3 group">
            <img src="/logo.png" alt="Sentinel Quant" className="w-9 h-9 object-contain" />
            <div className="flex items-baseline gap-2">
              <span className="text-2xl font-black text-white tracking-tight">Sentinel Quant</span>
              <span className="text-[10px] text-neutral-600 font-medium hidden sm:block tracking-wider uppercase">Stock Analyzer</span>
            </div>
          </Link>
          <div className="flex items-center space-x-1">
            <Link
              to="/"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Analyze
            </Link>
            <Link
              to="/extra-resources"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/extra-resources')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Resources
            </Link>
            <Link
              to="/daily-summary"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/daily-summary')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Daily Summary
            </Link>
            <Link
              to="/news"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/news')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              News
            </Link>
            {/* QUANT HF NAV LINK — hidden; restore by uncommenting
            <Link
              to="/quant"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/quant')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Quant HF
            </Link>
            */}
            <Link
              to="/backtest"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/backtest')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Backtest
            </Link>
            <Link
              to="/watchlist"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/watchlist')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              Watchlist
            </Link>
            <Link
              to="/symbols-to-buy"
              className={`px-4 py-1.5 rounded-full text-sm font-bold transition-all ${
                (isActive('/symbols-to-buy') || location.pathname.startsWith('/symbol/'))
                  ? 'bg-emerald-500 text-black shadow-lg shadow-emerald-500/20'
                  : 'bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 hover:text-emerald-200'
              }`}
            >
              Symbols to Buy
            </Link>
          </div>
        </div>
      </div>
    </nav>
  )
}
