import { useState, useEffect, useRef, Component } from 'react'

// ─── Per-card error boundary: one malformed rec can never blank the grid ──────
class CardBoundary extends Component {
  constructor(props) { super(props); this.state = { failed: false } }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(e) { try { console.error('Options card error:', e) } catch {} }
  render() {
    if (this.state.failed) {
      return (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-lg px-4 py-3 text-xs text-neutral-500">
          <span className="font-bold text-neutral-300 font-mono">{this.props.ticker || '—'}</span>
          {' '}— couldn't render this ticket safely; skipped.
        </div>
      )
    }
    return this.props.children
  }
}

// ─── Strategy metadata (buy + collateralized-sell, never naked) ───────────────
const STRUCTURE_META = {
  LONG_CALL:        { label: 'Buy Call',        kind: 'BUY',  tone: 'green', outlookTxt: 'Bullish · leveraged' },
  LONG_PUT:         { label: 'Buy Put',         kind: 'BUY',  tone: 'red',   outlookTxt: 'Bearish · leveraged' },
  CASH_SECURED_PUT: { label: 'Sell Put (CSP)',  kind: 'SELL', tone: 'green', outlookTxt: 'Bullish/neutral · income' },
  COVERED_CALL:     { label: 'Sell Call (CC)',  kind: 'SELL', tone: 'amber', outlookTxt: 'Neutral/mild-bearish · income' },
}

// OPTIONS TRADING — actionable, defined-risk options recommendations.
// Same universe as "Symbols to Buy" (/api/symbols-to-buy) plus a manual lookup.
// For each ticker we ask /api/options-recommendation/{ticker}, which returns a
// complete order ticket (what to buy, call/put, strike, expiry, cost, breakeven,
// max loss) or a plain-English reason when no safe trade exists.
//
// Every number is finite-checked before render (never show a corrupt price), and
// requests are staggered to stay rate-limit-safe. Recommendation only — nothing
// here places a trade.

// ─── Format helpers (defensive: never render NaN/Infinity) ─────────────────────
function isNum(v) { return typeof v === 'number' && Number.isFinite(v) }
function money(v) { return isNum(v) ? '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—' }
function money0(v) { return isNum(v) ? '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }) : '—' }
function px(v) { return isNum(v) ? '$' + v.toFixed(2) : '—' }
function pct(v, dec = 1) { return isNum(v) ? (v >= 0 ? '+' : '') + v.toFixed(dec) + '%' : '—' }
function pctPos(v, dec = 1) { return isNum(v) ? v.toFixed(dec) + '%' : '—' }
function num(v, dec = 2) { return isNum(v) ? v.toFixed(dec) : '—' }
function big(v) {
  if (!isNum(v)) return '—'
  if (v >= 1e12) return '$' + (v / 1e12).toFixed(1) + 'T'
  if (v >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B'
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(0) + 'M'
  return '$' + v.toFixed(0)
}

// ─── Small stat cell ──────────────────────────────────────────────────────────
function Stat({ label, value, accent }) {
  return (
    <div className="bg-neutral-950/60 border border-neutral-800/60 rounded-lg px-3 py-2">
      <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">{label}</div>
      <div className={`text-sm font-mono font-bold mt-0.5 ${accent || 'text-neutral-100'}`}>{value}</div>
    </div>
  )
}

function Chip({ children, tone = 'neutral' }) {
  const map = {
    neutral: 'bg-neutral-900 border-neutral-700 text-neutral-300',
    green: 'bg-emerald-950/50 border-emerald-800/40 text-emerald-300',
    red: 'bg-rose-950/50 border-rose-800/40 text-rose-300',
    amber: 'bg-amber-950/40 border-amber-800/40 text-amber-200',
    blue: 'bg-blue-950/40 border-blue-800/40 text-blue-200',
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-bold ${map[tone] || map.neutral}`}>
      {children}
    </span>
  )
}

// ─── One strategy block (buy call/put, or collateralized sell put/call) ────────
function StrategyBlock({ strat, ticker, underlying, defaultOpen }) {
  const [open, setOpen] = useState(!!defaultOpen)
  const meta = STRUCTURE_META[strat.structure] || { label: strat.title || strat.structure, kind: strat.action?.startsWith('BUY') ? 'BUY' : 'SELL', tone: 'neutral', outlookTxt: '' }
  const isBuy = meta.kind === 'BUY'
  const netTone = strat.net_type === 'CREDIT' ? 'green' : 'blue'
  const liqTone = strat.liquidity_quality === 'GOOD' ? 'text-emerald-400'
    : strat.liquidity_quality === 'FAIR' ? 'text-amber-300' : 'text-rose-400'
  const headMoney = strat.net_type === 'CREDIT'
    ? { label: 'Credit received', val: money(strat.net_credit), tone: 'text-emerald-400' }
    : { label: 'Total cost (debit)', val: money(strat.net_debit), tone: 'text-white' }

  return (
    <div className={`rounded-lg border ${strat.is_primary ? 'border-neutral-600' : 'border-neutral-800/70'} bg-neutral-950/40 overflow-hidden`}>
      {/* header */}
      <button onClick={() => setOpen(o => !o)} className="w-full text-left px-4 py-3 flex items-start justify-between gap-3 hover:bg-neutral-900/40 transition-colors">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <Chip tone={meta.tone}>{meta.label}</Chip>
            <Chip tone={netTone}>{strat.net_type}</Chip>
            <Chip tone={strat.risk_type === 'DEFINED' ? 'green' : 'amber'}>{strat.risk_type === 'DEFINED' ? 'DEFINED RISK' : 'COLLATERALIZED'}</Chip>
            {strat.is_primary && <Chip tone="blue">PRIMARY</Chip>}
          </div>
          <div className="text-sm font-bold text-neutral-100 font-mono leading-tight truncate">
            <span className={isBuy ? 'text-emerald-400' : 'text-blue-300'}>{strat.action}</span>
            {' '}{strat.contracts}× {ticker} {strat.expiration_human} {px(strat.strike)} {strat.option_type}
          </div>
          <div className="text-[11px] text-neutral-500 mt-0.5">{meta.outlookTxt} · {strat.moneyness || '—'} · {isNum(strat.dte) ? strat.dte + ' DTE' : '—'} · limit ≈ {px(strat.est_premium_per_share)}/sh</div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">{headMoney.label}</div>
          <div className={`text-lg font-black font-mono ${headMoney.tone}`}>{headMoney.val}</div>
          <div className="text-[10px] text-neutral-500 mt-0.5">{open ? '▲ hide' : '▼ details'}</div>
        </div>
      </button>

      {/* economics grid — always visible */}
      <div className="px-4 pb-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Stat label="Breakeven" value={px(strat.breakeven)} accent="text-white" />
        <Stat label="Max loss" value={money(strat.max_loss)} accent="text-rose-400" />
        <Stat label="Max gain" value={strat.max_gain == null ? 'Unlimited' : money(strat.max_gain)} accent="text-emerald-400" />
        {strat.collateral_required > 0
          ? <Stat label="Cash collateral" value={money(strat.collateral_required)} accent="text-amber-300" />
          : strat.requires_shares > 0
            ? <Stat label="Shares needed" value={`${strat.requires_shares}`} accent="text-amber-300" />
            : <Stat label="Need to move" value={isNum(strat.breakeven_move_pct) ? pctPos(Math.abs(strat.breakeven_move_pct)) : '—'} />}
      </div>

      {open && (
        <>
          {/* advanced data */}
          <div className="px-4 pb-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Stat label="Expected move (1σ)" value={isNum(strat.expected_move_pct) ? '±' + pctPos(strat.expected_move_pct) : '—'} />
            <Stat label="Prob. ITM (est)" value={isNum(strat.prob_itm_est_pct) ? pctPos(strat.prob_itm_est_pct, 0) : '—'} />
            <Stat label="Delta" value={num(strat.delta)} />
            <Stat label="Impl. Vol" value={isNum(strat.iv_pct) ? pctPos(strat.iv_pct, 0) : '—'}
                  accent={strat.iv_environment === 'ELEVATED' ? 'text-amber-300' : 'text-neutral-100'} />
            <Stat label="IV env" value={strat.iv_environment || '—'} />
            <Stat label="Volume / OI" value={`${strat.volume ?? '—'} / ${strat.open_interest ?? '—'}`} />
            <Stat label="Liquidity" value={strat.liquidity_quality || '—'} accent={liqTone} />
            <Stat label="Premium/contract" value={money(strat.est_premium_per_contract)} />
          </div>

          <div className="px-4 pb-3">
            <p className="text-[11px] text-neutral-400 leading-relaxed">{strat.summary}</p>
            <p className="text-[11px] text-neutral-500 mt-1"><span className="text-neutral-400 font-semibold">Max loss:</span> {strat.max_loss_label}</p>
            <p className="text-[11px] text-neutral-500"><span className="text-neutral-400 font-semibold">Max gain:</span> {strat.max_gain_label}</p>
          </div>

          {/* order steps */}
          <div className="px-4 pb-3">
            <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 mb-2">How to place this order</div>
            <ol className="space-y-1.5">
              {(strat.order_instructions || []).map((s, i) => (
                <li key={i} className="flex gap-2 text-xs text-neutral-300">
                  <span className="shrink-0 w-4 h-4 rounded-full bg-neutral-800 text-neutral-400 text-[9px] font-bold flex items-center justify-center mt-0.5">{i + 1}</span>
                  <span>{s}</span>
                </li>
              ))}
            </ol>
          </div>

          {/* risk */}
          <div className="px-4 pb-4">
            <div className="bg-amber-950/20 border border-amber-800/40 rounded-lg px-3 py-2.5">
              <div className="text-[10px] font-bold uppercase tracking-widest text-amber-300 mb-1.5">Risk — read before you trade</div>
              <ul className="space-y-1">
                {(strat.risk_disclosures || []).map((r, i) => (
                  <li key={i} className="text-[11px] text-amber-100/80 flex gap-1.5">
                    <span className="text-amber-500">•</span><span>{r}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// ─── The order ticket card (headline + analysis + all strategies) ─────────────
function OrderTicket({ rec }) {
  const isCall = rec.option_type === 'CALL'
  const dirTone = isCall ? 'green' : 'red'
  const accentClass = isCall ? 'card-accent-green' : 'card-accent-red'
  const ta = rec.technical_analysis || {}
  const fa = rec.fundamental_analysis || {}
  const na = rec.news_analysis || {}
  const stb = rec.stb_context || {}
  const strategies = Array.isArray(rec.strategies) && rec.strategies.length
    ? rec.strategies
    : [rec]  // backward-compat: synthesize from the flat rec if strategies absent

  const alignTone = rec.fundamental_alignment === 'AGREES' ? 'green'
    : rec.fundamental_alignment === 'DIVERGES' ? 'red' : 'neutral'
  const newsTone = rec.news_alignment === 'AGREES' ? 'green'
    : rec.news_alignment === 'DIVERGES' ? 'red' : 'neutral'
  const macroTone = rec.macro_alignment === 'SUPPORTS' ? 'green'
    : rec.macro_alignment === 'CAUTION' ? 'amber' : 'neutral'

  return (
    <div className={`bg-neutral-900/50 border border-neutral-700 rounded-xl overflow-hidden ${accentClass}`}>
      {/* Headline */}
      <div className="px-5 py-4 border-b border-neutral-800/70">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <span className="text-2xl font-black text-white tracking-tight">{rec.ticker}</span>
              <Chip tone={dirTone}>{isCall ? '▲ BULLISH' : '▼ BEARISH'}</Chip>
              {isNum(rec.confidence) && <Chip tone="blue">conf {rec.confidence}%</Chip>}
              {rec.signal_confluence && <Chip tone="blue">confluence {rec.signal_confluence}</Chip>}
              {stb.in_stb_longs && <Chip tone="green">★ STB long</Chip>}
              {rec.stale_reused && <Chip tone="amber">stale</Chip>}
            </div>
            <div className="text-xs text-neutral-500 mt-0.5">
              underlying {px(rec.underlying_price)} · signal {rec.signal || '—'} · {rec.regime || 'NEUTRAL'} regime · {strategies.length} way{strategies.length !== 1 ? 's' : ''} to trade
            </div>
          </div>
        </div>
      </div>

      {/* Rationale + analysis chips */}
      <div className="px-5 py-4 space-y-3 border-b border-neutral-800/70">
        <p className="text-xs text-neutral-300 leading-relaxed">{rec.rationale}</p>
        <div className="flex flex-wrap gap-1.5">
          {ta.ema_trend && <Chip tone={ta.ema_trend === 'Bullish' ? 'green' : ta.ema_trend === 'Bearish' ? 'red' : 'neutral'}>EMA {ta.ema_trend}</Chip>}
          {isNum(ta.rsi14) && <Chip>RSI {ta.rsi14.toFixed(0)}</Chip>}
          {isNum(ta.momentum_pct) && <Chip tone={ta.momentum_pct >= 0 ? 'green' : 'red'}>Mom {pct(ta.momentum_pct, 0)}</Chip>}
          {isNum(ta.volatility_ann_pct) && <Chip>Vol {ta.volatility_ann_pct.toFixed(0)}%</Chip>}
          <Chip tone={alignTone}>Fundamentals {rec.fundamental_alignment || 'N/A'}</Chip>
          {isNum(fa.trailing_pe) && <Chip>P/E {fa.trailing_pe.toFixed(0)}</Chip>}
          {isNum(fa.eps) && <Chip tone={fa.eps >= 0 ? 'green' : 'red'}>EPS {fa.eps.toFixed(2)}</Chip>}
          {isNum(fa.market_cap) && <Chip>Cap {big(fa.market_cap)}</Chip>}
          {rec.news_alignment && <Chip tone={newsTone}>News {rec.news_alignment}{na.sentiment ? ` · ${na.sentiment}` : ''}</Chip>}
          {rec.macro_regime && <Chip tone={macroTone}>Macro {rec.macro_regime.replace('_', '-')}</Chip>}
          {isNum(stb.revenue_growth_pct) && <Chip tone="green">Rev {pct(stb.revenue_growth_pct, 0)}</Chip>}
          {isNum(stb.roe_pct) && <Chip>ROE {stb.roe_pct.toFixed(0)}%</Chip>}
          {isNum(stb.peg_ratio) && <Chip>PEG {stb.peg_ratio.toFixed(2)}</Chip>}
        </div>
      </div>

      {/* Strategies — buy + collateralized-sell */}
      <div className="px-5 py-4 space-y-3">
        <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">Ways to trade this view</div>
        {strategies.map((s, i) => (
          <StrategyBlock key={s.structure || i} strat={s} ticker={rec.ticker} underlying={rec.underlying_price} defaultOpen={i === 0} />
        ))}
      </div>
    </div>
  )
}

// ─── No-trade card ─────────────────────────────────────────────────────────────
function NoTrade({ rec }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-lg px-4 py-3 flex items-start gap-3">
      <span className="font-bold text-neutral-300 font-mono w-16 shrink-0">{rec.ticker}</span>
      <span className="text-xs text-neutral-500">{rec.reason || 'No options trade right now.'}</span>
    </div>
  )
}

// ─── Client-side cache (survives reloads so we ALWAYS show last-good picks) ────
const CACHE_KEY = 'optionsTrading:v1'
const CACHE_TTL = 30 * 60 * 1000   // 30 min — matches the backend anti-flap window
const REQ_TIMEOUT = 12000          // per-request cap so one hung fetch can't stall the scan
const REQ_RETRIES = 2              // attempts per ticker before giving up this pass
// Safety net: if Symbols-to-Buy is momentarily empty (cold cache / warming up),
// fall back to these liquid, deep-options large-caps so the page ALWAYS shows
// picks. Each still runs through the same gated recommendation engine, so a
// fallback ticker only surfaces a card if it independently earns an actionable
// signal — no fabricated trades. STB stays the source of truth when it's warm.
const FALLBACK_UNIVERSE = [
  'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AMD',
  'NFLX', 'JPM', 'BAC', 'GS', 'XOM', 'CVX', 'AVGO', 'CRM',
]

function loadCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const c = JSON.parse(raw)
    if (!c || typeof c !== 'object' || !c.recs) return null
    return c   // { recs, tickers, ts }
  } catch { return null }
}
function saveCache(recs, tickers) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify({ recs, tickers, ts: Date.now() })) } catch {}
}

// fetch with an abort timeout so a slow/hung upstream can never block the loop.
async function fetchJSON(url, timeout = REQ_TIMEOUT) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const r = await fetch(url, { signal: ctrl.signal })
    if (!r.ok) return null
    return await r.json()
  } catch { return null } finally { clearTimeout(timer) }
}

// ─── Page ──────────────────────────────────────────────────────────────────────
export default function OptionsTrading() {
  const cached = typeof window !== 'undefined' ? loadCache() : null
  const cacheFresh = cached && (Date.now() - (cached.ts || 0) < CACHE_TTL)
  const [recs, setRecs] = useState(cached?.recs || {})           // ticker -> rec (hydrated from cache)
  const [tickers, setTickers] = useState(cached?.tickers || [])  // universe order
  const [loading, setLoading] = useState(!cached)                // only block if we have nothing to show
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [query, setQuery] = useState('')
  const [lookupBusy, setLookupBusy] = useState(false)
  const runningRef = useRef(false)
  const recsRef = useRef(recs)          // latest recs for cache persistence without stale closures
  recsRef.current = recs

  // Fetch one ticker with timeout + retry/backoff; commit into state + cache.
  async function fetchRec(t, { retries = REQ_RETRIES } = {}) {
    for (let attempt = 0; attempt <= retries; attempt++) {
      const j = await fetchJSON(`/api/options-recommendation/${encodeURIComponent(t)}`)
      if (j && typeof j === 'object' && typeof j.ticker === 'string' && j.ticker) {
        // Never trust `actionable` unless the core fields are present & sane.
        if (j.actionable && !(isNum(j.underlying_price) && (Array.isArray(j.strategies) ? j.strategies.length : isNum(j.strike)))) {
          j.actionable = false
          j.reason = j.reason || 'Incomplete recommendation data — withheld for safety.'
        }
        setRecs(prev => {
          const next = { ...prev, [j.ticker]: j }
          saveCache(next, tickers.includes(j.ticker) ? tickers : [...tickers, j.ticker])
          return next
        })
        return j
      }
      // transient miss — back off briefly, then retry (unless last attempt).
      if (attempt < retries) await new Promise(r => setTimeout(r, 600 * (attempt + 1)))
    }
    return null   // keep any previously-cached rec for this ticker (don't wipe it)
  }

  async function loadUniverse(force = false) {
    if (runningRef.current) return
    runningRef.current = true
    if (force) setRefreshing(true)
    else if (!cacheFresh) setLoading(true)   // if cache is fresh, refresh silently in the background
    setError(null)
    try {
      let j = null
      try { j = await fetchJSON('/api/symbols-to-buy', 15000) } catch { j = null }
      let list = [...new Set(((j && j.long_picks) || []).map(p => p.ticker).filter(Boolean))]
      // Safety net: STB momentarily empty (cold cache) → use the fallback large-caps
      // so the page still produces picks. The gated engine still decides per ticker.
      let usingFallback = false
      if (!list.length) {
        list = [...FALLBACK_UNIVERSE]
        usingFallback = true
      }
      if (list.length) {
        setTickers(list)
        setLoading(false)   // universe known → stop the full-page blocker; cards stream in
        setProgress({ done: 0, total: list.length })
        // Sequential + staggered to stay rate-limit-safe; each has its own timeout+retry.
        let done = 0
        for (const t of list) {
          await fetchRec(t)
          done += 1
          setProgress({ done, total: list.length })
          await new Promise(r => setTimeout(r, 200))
        }
        // If we ran the fallback because STB was cold, quietly re-pull STB once it's
        // likely warm so the real universe takes over on the next pass.
        if (usingFallback && !force) {
          setTimeout(() => { runningRef.current = false; loadUniverse(true) }, 45000)
        }
      } else if (!Object.keys(recsRef.current).length) {
        // No universe AND nothing cached to show.
        setError((j && j.message) || 'Universe warming up — check back shortly.')
      }
    } catch (e) {
      if (!Object.keys(recsRef.current).length) setError(e.message || 'Failed to load options.')
    } finally {
      setLoading(false)
      setRefreshing(false)
      runningRef.current = false
    }
  }

  useEffect(() => { loadUniverse(false) }, [])

  async function onLookup(e) {
    e.preventDefault()
    const t = query.trim().toUpperCase()
    if (!t) return
    setLookupBusy(true)
    setTickers(prev => (prev.includes(t) ? prev : [t, ...prev]))
    const j = await fetchRec(t)
    setLookupBusy(false)
    if (!j && !recsRef.current[t]) setError(`Could not fetch options for ${t}.`)
  }

  const ordered = tickers.map(t => recs[t]).filter(Boolean)
  const actionable = ordered.filter(r => r.actionable)
  const withheld = ordered.filter(r => !r.actionable)
  const scanning = progress.total > 0 && progress.done < progress.total
  const cacheAgeMin = cached?.ts ? Math.round((Date.now() - cached.ts) / 60000) : null

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <div className="flex items-end justify-between mb-2 flex-wrap gap-3">
          <div>
            <h1 className="text-3xl font-black text-white tracking-tight">Options Trading</h1>
            <p className="text-sm text-neutral-400 mt-1">
              Buy &amp; collateralized-sell options ideas · same universe as Symbols to Buy · technical + fundamental + news + macro-regime + IV analytics · recommendation only
            </p>
          </div>
          <div className="text-right">
            <button
              onClick={() => loadUniverse(true)}
              disabled={refreshing}
              className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 rounded-lg text-sm font-medium text-neutral-200 transition-colors"
            >
              {refreshing || scanning ? 'Refreshing…' : 'Refresh'}
            </button>
            {isNum(cacheAgeMin) && !scanning && (
              <div className="text-[10px] text-neutral-600 mt-1">cached {cacheAgeMin === 0 ? 'just now' : `${cacheAgeMin}m ago`}</div>
            )}
          </div>
        </div>

        {/* Manual lookup */}
        <form onSubmit={onLookup} className="mt-4 flex items-center gap-2">
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Look up any ticker (e.g. AAPL)"
            className="px-3 py-2 w-64 bg-neutral-900 border border-neutral-700 rounded-lg text-sm text-white font-mono placeholder-neutral-600 focus:border-blue-500 focus:outline-none uppercase"
          />
          <button
            type="submit"
            disabled={lookupBusy}
            className="px-4 py-2 bg-white text-black hover:bg-neutral-200 disabled:opacity-50 rounded-lg text-sm font-bold transition-colors"
          >
            {lookupBusy ? 'Loading…' : 'Get options'}
          </button>
        </form>

        {/* Safety banner */}
        <div className="mt-4 bg-blue-950/20 border border-blue-800/40 rounded-lg px-4 py-2.5 text-xs text-blue-200 flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="font-bold">Safety:</span>
          <span>Buy calls/puts (max loss = premium) + collateralized selling (cash-secured puts / covered calls)</span>
          <span className="text-blue-500">·</span>
          <span>No naked shorts — ever</span>
          <span className="text-blue-500">·</span>
          <span>Withholds on low conviction, thin liquidity, or price disagreement</span>
          <span className="text-blue-500">·</span>
          <span>Never auto-executes</span>
        </div>
      </div>

      {/* Full-page blocker ONLY when we truly have nothing to show yet. */}
      {loading && ordered.length === 0 && (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Scanning options across the universe…
        </div>
      )}

      {/* Live progress bar — cards stream in beneath it while the scan continues. */}
      {scanning && (
        <div className="mb-4">
          <div className="flex items-center justify-between text-[11px] text-neutral-400 mb-1">
            <span>Scanning options… {progress.done}/{progress.total}{ordered.length ? ` · ${actionable.length} actionable so far` : ''}</span>
            <span>{Math.round((progress.done / progress.total) * 100)}%</span>
          </div>
          <div className="h-1 bg-neutral-800 rounded-full overflow-hidden">
            <div className="h-full bg-blue-500 transition-all duration-300" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
        </div>
      )}

      {error && !loading && (
        <div className="bg-amber-950/30 border border-amber-800/40 rounded-lg p-4 text-amber-200 mb-6 text-sm">
          {error}
          <button onClick={() => loadUniverse(false)} className="ml-4 text-xs underline text-amber-400 hover:text-amber-200">Retry</button>
        </div>
      )}

      {(!loading || ordered.length > 0) && (
        <div className="space-y-6">
          {actionable.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-3">
                <h2 className="text-lg font-bold text-white">Actionable trades</h2>
                <span className="text-xs text-neutral-500">{actionable.length} ready to place</span>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {actionable.map(r => (
                  <CardBoundary key={r.ticker} ticker={r.ticker}>
                    <OrderTicket rec={r} />
                  </CardBoundary>
                ))}
              </div>
            </div>
          )}

          {actionable.length === 0 && !error && !scanning && (
            <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-8 text-center text-neutral-500">
              No actionable options trades right now — the engine is withholding until conviction, liquidity, and price all line up.
            </div>
          )}

          {withheld.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-3">
                <h2 className="text-sm font-bold text-neutral-400 uppercase tracking-wider">No trade (why)</h2>
                <span className="text-xs text-neutral-600">{withheld.length}</span>
              </div>
              <div className="space-y-2">
                {withheld.map(r => <NoTrade key={r.ticker} rec={r} />)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
