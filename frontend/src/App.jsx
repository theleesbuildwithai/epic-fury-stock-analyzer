import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import Navbar from './components/Navbar'
import TickerBanner from './components/TickerBanner'
import Home from './pages/Home'
import HowItWorks from './pages/HowItWorks'
import Performance from './pages/Performance'
import ExtraResources from './pages/ExtraResources'
import CookieConsent from './components/CookieConsent'

export default function App() {
  return (
    <Router>
      <div className="min-h-screen bg-neutral-950">
        <TickerBanner />
        <Navbar />
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/how-it-works" element={<HowItWorks />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/extra-resources" element={<ExtraResources />} />
        </Routes>
        <CookieConsent />
      </div>
    </Router>
  )
}
