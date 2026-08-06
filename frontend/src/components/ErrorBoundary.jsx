import { Component } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

// ─── Error Boundary ───────────────────────────────────────────────────────────
// Catches any render/lifecycle error thrown by a page and shows a recoverable
// fallback instead of blanking the whole app. The Navbar/TickerBanner stay
// mounted (this sits inside the Router, around <Routes>), and navigating to a
// new route auto-clears the error via the `resetKey` remount below.
class ErrorBoundaryInner extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    // Log for debugging; never throws.
    try { console.error('Page render error:', error, info?.componentStack) } catch {}
  }

  componentDidUpdate(prevProps) {
    // When the route changes, clear the error so the new page renders fresh.
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false, error: null })
    }
  }

  render() {
    if (!this.state.hasError) return this.props.children

    const msg = (this.state.error && (this.state.error.message || String(this.state.error))) || 'Unknown error'
    return (
      <div className="max-w-2xl mx-auto px-4 py-20 text-center">
        <div className="bg-neutral-900/60 border border-rose-800/40 rounded-2xl p-10">
          <div className="text-rose-300 text-2xl font-black mb-2">Something went wrong on this page</div>
          <p className="text-neutral-400 text-sm mb-6">
            The rest of the app is still running. Your data is safe — nothing was lost.
            You can retry this page or head back home.
          </p>
          <div className="flex items-center justify-center gap-3">
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              className="px-5 py-2 bg-white text-black font-semibold text-sm rounded-lg hover:bg-neutral-200 transition-colors"
            >
              Retry
            </button>
            <button
              onClick={() => this.props.onGoHome && this.props.onGoHome()}
              className="px-5 py-2 bg-neutral-800 text-neutral-200 font-semibold text-sm rounded-lg hover:bg-neutral-700 transition-colors"
            >
              Go home
            </button>
            <button
              onClick={() => { try { window.location.reload() } catch {} }}
              className="px-5 py-2 text-neutral-500 text-sm hover:text-white transition-colors"
            >
              Reload app
            </button>
          </div>
          <details className="mt-6 text-left">
            <summary className="text-[11px] text-neutral-600 cursor-pointer hover:text-neutral-400">Technical details</summary>
            <pre className="mt-2 text-[11px] text-neutral-500 bg-neutral-950/60 border border-neutral-800 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{msg}</pre>
          </details>
        </div>
      </div>
    )
  }
}

// Functional wrapper: supplies the current route as `resetKey` so the boundary
// auto-recovers on navigation, and a `onGoHome` handler.
export default function ErrorBoundary({ children }) {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <ErrorBoundaryInner resetKey={location.pathname} onGoHome={() => navigate('/')}>
      {children}
    </ErrorBoundaryInner>
  )
}
