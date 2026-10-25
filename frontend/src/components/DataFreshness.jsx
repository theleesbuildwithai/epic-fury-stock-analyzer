// DATA FRESHNESS — fail-visible labels for the analyze payload.
// The backend already annotates every report with staleness, cache-age,
// drift-rescale and cross-source verification metadata; this strip puts that
// on screen instead of letting fallbacks stay silent. Renders nothing when
// everything is fresh and verified — the normal state shows no chrome.
//
// Palette discipline: rose = integrity problems, neutral = informational,
// emerald reserved for bullish verdicts elsewhere (not used here).

function Chip({ cls, children }) {
  return (
    <span className={`text-[10px] font-bold px-2 py-1 rounded border inline-block ${cls}`}>
      {children}
    </span>
  )
}

export default function DataFreshness({ data }) {
  if (!data || typeof data !== 'object') return null

  const chips = []

  // Stale lookback — the verdict is built on an old report.
  const staleDays = Number(data._stale_days)
  if (Number.isFinite(staleDays) && staleDays > 0) {
    chips.push(
      <Chip key="stale" cls="bg-rose-500/15 border-rose-500/40 text-rose-300">
        DATA STALE — {staleDays} day{staleDays === 1 ? '' : 's'} old
      </Chip>
    )
  }

  // Cached report older than an hour — informational, not a problem.
  const ageS = Number(data._cache_age_seconds)
  if (!chips.length && Number.isFinite(ageS) && ageS > 3600) {
    const hrs = Math.floor(ageS / 3600)
    chips.push(
      <Chip key="cache" cls="bg-white/5 border-neutral-700 text-neutral-400">
        CACHED — {hrs}h old{data._cache_source ? ` · ${String(data._cache_source).replace('_', ' ')}` : ''}
      </Chip>
    )
  }

  // Historical series was rescaled to match the live quote.
  const drift = data._data_drift
  if (drift && typeof drift === 'object') {
    const pct = Number(drift.drift_pct ?? drift.pct)
    chips.push(
      <Chip key="drift" cls="bg-white/5 border-neutral-700 text-neutral-400">
        SERIES RESCALED{Number.isFinite(pct) ? ` — drift ${pct.toFixed(1)}%` : ''}
      </Chip>
    )
  }

  // Cross-source price verification (CNBC second opinion).
  const pv = data.price_verification
  const pvStatus = typeof pv?.status === 'string' ? pv.status.toUpperCase() : ''
  const dev = Number(pv?.deviation_pct)
  if (pvStatus === 'MISMATCH') {
    chips.push(
      <Chip key="pv" cls="bg-rose-500/15 border-rose-500/40 text-rose-300">
        PRICE MISMATCH{Number.isFinite(dev) ? ` — sources ${dev.toFixed(1)}% apart` : ''} · verdict withheld
      </Chip>
    )
  } else if (pvStatus === 'TOLERANCE') {
    chips.push(
      <Chip key="pv" cls="bg-rose-500/10 border-rose-500/25 text-rose-400">
        PRICE VARIANCE{Number.isFinite(dev) ? ` — ${dev.toFixed(1)}%` : ''} between sources
      </Chip>
    )
  }
  // VERIFIED and UNVERIFIED render nothing: agreement is the normal state,
  // and an unreachable reference source is not an integrity finding.

  if (!chips.length) return null

  return (
    <div className="flex flex-wrap items-center gap-2">
      {chips}
    </div>
  )
}
