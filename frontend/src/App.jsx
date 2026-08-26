import { lazy, Suspense } from 'react'
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom'
import Navbar from './components/Navbar'
import TickerBanner from './components/TickerBanner'
import CookieConsent from './components/CookieConsent'
import ErrorBoundary from './components/ErrorBoundary'

// Route-level code splitting: each page ships in its own chunk and loads on
// demand, so the initial bundle stays small instead of one ~830 kB blob.
const Home              = lazy(() => import('./pages/Home'))
const Watchlist         = lazy(() => import('./pages/Watchlist'))
const ExtraResources    = lazy(() => import('./pages/ExtraResources'))
const News              = lazy(() => import('./pages/News'))
const DailySummary      = lazy(() => import('./pages/DailySummary'))
// QUANT HF PAGE — hidden; restore by uncommenting the lazy import and route below
// const QuantDashboard = lazy(() => import('./pages/QuantDashboard'))
const SymbolsToBuy      = lazy(() => import('./pages/SymbolsToBuy'))
const OptionsTrading    = lazy(() => import('./pages/OptionsTrading'))
const SymbolDetail      = lazy(() => import('./pages/SymbolDetail'))
const SystemIntelligence = lazy(() => import('./pages/SystemIntelligence'))

function PageFallback() {
  return (
    <div className="max-w-7xl mx-auto px-4 py-20 text-center text-neutral-500 text-sm">
      Loading…
    </div>
  )
}

export default function App() {
  return (
    <Router>
      <div className="min-h-screen bg-neutral-950">
        <TickerBanner />
        <Navbar />
        <ErrorBoundary>
        <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/" element={<Home />} />

          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/extra-resources" element={<ExtraResources />} />
          <Route path="/news" element={<News />} />
          <Route path="/daily-summary" element={<DailySummary />} />
          {/* QUANT HF ROUTE — hidden; restore by uncommenting and re-enabling import above
          <Route path="/quant" element={<QuantDashboard />} />
          */}
          <Route path="/symbols-to-buy" element={<SymbolsToBuy />} />
          <Route path="/options" element={<OptionsTrading />} />
          <Route path="/symbol/:ticker" element={<SymbolDetail />} />
          <Route path="/system-intelligence" element={<SystemIntelligence />} />
          {/* Catch-all: any unknown/removed route (e.g. old /ibkr) redirects
              home instead of rendering a blank page. */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
        </ErrorBoundary>
        <CookieConsent />
      </div>
    </Router>
  )
}
