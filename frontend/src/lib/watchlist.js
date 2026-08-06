// ─── Watchlist localStorage — single source of truth ──────────────────────────
// Both the STB list (SymbolsToBuy) and the pick drill-down (SymbolDetail) add
// picks through this one writer so the schema Watchlist.jsx reads can never drift
// between call sites. Adding a stop/target field here updates every entry point
// at once — keeping the STB ↔ Watchlist sell-signal loop consistent.
export const WATCHLIST_KEY = 'sentinel_quant_watchlist'

const num = v => (Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : null)

// Adds a pick to the watchlist, carrying the exact entry/stop/target levels it
// was built on. previous_close is filled by Watchlist's live refresh on mount.
// Returns 'added' | 'exists' | 'error'.
export function addPickToWatchlist(pick) {
  try {
    const ticker = String(pick?.ticker || '').trim().toUpperCase()
    if (!ticker) return 'error'
    let list = []
    try {
      const raw = localStorage.getItem(WATCHLIST_KEY)
      const parsed = raw ? JSON.parse(raw) : []
      if (Array.isArray(parsed)) list = parsed
    } catch { list = [] }
    if (list.some(s => s && String(s.ticker).toUpperCase() === ticker)) return 'exists'
    const entry = num(pick.entry_price)
    list.push({
      ticker,
      name: pick.company_name || pick.name || ticker,
      entry_price: entry ?? 0,
      stop_loss: num(pick.stop_loss),
      target_price: num(pick.target_price),
      current_price: entry ?? 0,
      previous_close: null,
      added_at: new Date().toISOString(),
      last_updated: new Date().toISOString(),
    })
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(list))
    return 'added'
  } catch { return 'error' }
}
