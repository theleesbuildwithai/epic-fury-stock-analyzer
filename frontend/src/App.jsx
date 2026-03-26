import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import Navbar from './components/Navbar'
import TickerBanner from './components/TickerBanner'
import Home from './pages/Home'

import Watchlist from './pages/Watchlist'
import ExtraResources from './pages/ExtraResources'
import News from './pages/News'
import DailySummary from './pages/DailySummary'
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
        </Routes>
        <CookieConsent />
      </div>
    </Router>
  )
}
