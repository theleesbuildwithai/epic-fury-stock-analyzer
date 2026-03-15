import { useState, useEffect } from 'react'

export default function CookieConsent() {
  const [show, setShow] = useState(false)

  useEffect(() => {
    const consent = localStorage.getItem('epic_fury_cookie_consent')
    if (!consent) {
      setShow(true)
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

  if (!show) return null

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 bg-neutral-900 border-t border-neutral-700 p-4 shadow-2xl">
      <div className="max-w-5xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4">
        <div className="text-sm text-neutral-300">
          <p className="font-medium text-white mb-1">Cookie & Storage Notice</p>
          <p className="text-neutral-400">
            We use browser storage to save your stock predictions and preferences locally on your device.
            No data is shared with third parties. Accept to enable prediction tracking across sessions.
          </p>
        </div>
        <div className="flex gap-3 shrink-0">
          <button
            onClick={handleDecline}
            className="px-4 py-2 text-sm text-neutral-400 hover:text-white border border-neutral-700
                       rounded-lg hover:bg-neutral-800 transition-colors"
          >
            Decline
          </button>
          <button
            onClick={handleAccept}
            className="px-6 py-2 text-sm font-medium bg-white text-black rounded-lg
                       hover:bg-neutral-200 transition-colors"
          >
            Accept
          </button>
        </div>
      </div>
    </div>
  )
}
