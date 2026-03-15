import { useState, useEffect } from 'react'

export default function CookieConsent() {
  const [show, setShow] = useState(false)

  useEffect(() => {
    const consent = localStorage.getItem('epic_fury_cookie_consent')
    if (!consent) {
      // Small delay so it slides in smoothly
      setTimeout(() => setShow(true), 1000)
    }
  }, [])

  const handleAccept = () => {
    localStorage.setItem('epic_fury_cookie_consent', 'accepted')
    localStorage.setItem('epic_fury_consent_date', new Date().toISOString())
    setShow(false)
  }

  const handleDecline = () => {
    localStorage.setItem('epic_fury_cookie_consent', 'declined')
    setShow(false)
  }

  return (
    <div className={`fixed bottom-4 left-4 right-4 sm:left-auto sm:right-6 sm:bottom-6 z-50
                     max-w-md transition-all duration-500 ease-out
                     ${show ? 'translate-y-0 opacity-100' : 'translate-y-8 opacity-0 pointer-events-none'}`}>
      <div className="bg-neutral-900/95 backdrop-blur-lg border border-neutral-700/50 rounded-2xl p-5 shadow-2xl shadow-black/40">
        <div className="flex items-start gap-3 mb-4">
          <div className="w-8 h-8 bg-neutral-800 rounded-lg flex items-center justify-center shrink-0 mt-0.5">
            <svg className="w-4 h-4 text-neutral-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
            </svg>
          </div>
          <div>
            <p className="font-semibold text-white text-sm mb-1">Storage & Privacy</p>
            <p className="text-neutral-400 text-xs leading-relaxed">
              We store your predictions and search history locally on your device to enhance your experience.
              No data leaves your browser.
            </p>
          </div>
        </div>
        <div className="flex gap-2 justify-end">
          <button
            onClick={handleDecline}
            className="px-4 py-2 text-xs text-neutral-400 hover:text-white rounded-lg
                       hover:bg-neutral-800 transition-all font-medium"
          >
            Decline
          </button>
          <button
            onClick={handleAccept}
            className="px-5 py-2 text-xs font-semibold bg-white text-black rounded-lg
                       hover:bg-neutral-200 transition-all shadow-lg shadow-white/10"
          >
            Accept
          </button>
        </div>
      </div>
    </div>
  )
}
