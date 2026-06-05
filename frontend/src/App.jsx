import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import Navbar from './components/Navbar'
import TickerBanner from './components/TickerBanner'
import Home from './pages/Home'

import Watchlist from './pages/Watchlist'
import ExtraResources from './pages/ExtraResources'
import News from './pages/News'
import DailySummary from './pages/DailySummary'
import QuantDashboard from './pages/QuantDashboard'
import IBKRDashboard from './pages/IBKRDashboard'
import BacktestDashboard from './pages/BacktestDashboard'
import SymbolsToBuy from './pages/SymbolsToBuy'
import SymbolDetail from './pages/SymbolDetail'
import SystemIntelligence from './pages/SystemIntelligence'
import CookieConsent from './components/CookieConsent'

export default function App() {
  return (
    <Router>
      <div className="min-h-screen bg-neutral-950">
        <TickerBanner />
        <Navbar />
        <Routes>
          <Route path="/" element={<Home />} />

          <Route path="/watchlist" element={<Watchlist />} />
          <Route path="/extra-resources" element={<ExtraResources />} />
          <Route path="/news" element={<News />} />
          <Route path="/daily-summary" element={<DailySummary />} />
          <Route path="/quant" element={<QuantDashboard />} />
          <Route path="/ibkr" element={<IBKRDashboard />} />
          <Route path="/backtest" element={<BacktestDashboard />} />
          <Route path="/symbols-to-buy" element={<SymbolsToBuy />} />
          <Route path="/symbol/:ticker" element={<SymbolDetail />} />
          <Route path="/system-intelligence" element={<SystemIntelligence />} />
        </Routes>
        <CookieConsent />
      </div>
    </Router>
  )
}
