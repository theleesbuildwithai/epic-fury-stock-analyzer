import { Link, useLocation } from 'react-router-dom'

export default function Navbar() {
  const location = useLocation()
  const isActive = (path) => location.pathname === path

  return (
    <nav className="bg-neutral-950/90 backdrop-blur-md border-b border-neutral-800/50 sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-14">
          <Link to="/" className="flex items-center space-x-3 group">
            <div className="w-8 h-8 bg-white rounded-lg flex items-center justify-center shadow-lg shadow-white/5">
              <span className="text-black font-black text-sm">EF</span>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-bold text-white tracking-tight">Epic Fury</span>
              <span className="text-xs text-neutral-500 font-medium hidden sm:block">Stock Analyzer</span>
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
              to="/news"
              className={`px-4 py-1.5 rounded-full text-sm font-medium transition-all ${
                isActive('/news')
                  ? 'bg-white text-black shadow-lg shadow-white/10'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/50'
              }`}
            >
              News
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
          </div>
        </div>
      </div>
    </nav>
  )
}
