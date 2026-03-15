import { Link, useLocation } from 'react-router-dom'

export default function Navbar() {
  const location = useLocation()
  const isActive = (path) => location.pathname === path

  return (
    <nav className="bg-black border-b border-neutral-800">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          <Link to="/" className="flex items-center space-x-2">
            <span className="text-xl font-bold text-white">Epic Fury</span>
            <span className="text-sm text-neutral-400">Stock Analyzer</span>
          </Link>
          <div className="flex space-x-4">
            <Link
              to="/"
              className={`px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive('/') ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-white hover:bg-neutral-800'
              }`}
            >
              Analyze
            </Link>
            <Link
              to="/extra-resources"
              className={`px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive('/extra-resources') ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-white hover:bg-neutral-800'
              }`}
            >
              Extra Resources
            </Link>
            <Link
              to="/performance"
              className={`px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive('/performance') ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-white hover:bg-neutral-800'
              }`}
            >
              Performance
            </Link>
            <Link
              to="/how-it-works"
              className={`px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive('/how-it-works') ? 'bg-neutral-800 text-white' : 'text-neutral-400 hover:text-white hover:bg-neutral-800'
              }`}
            >
              How It Works
            </Link>
          </div>
        </div>
      </div>
    </nav>
  )
}
