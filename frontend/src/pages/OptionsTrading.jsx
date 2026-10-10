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
          {' '}— order ticket could not be rendered safely; excluded.
        </div>
      )
    }
    return this.props.children
  }
}

// ─── Strategy metadata (buy + collateralized-sell, never naked) ───────────────
// `bar` is the distribution-bar swatch. Palette discipline: bullish structures
// are green shades, bearish are red, the neutral-outlook covered call is grey.
const STRUCTURE_META = {
  LONG_CALL:        { label: 'Buy Call',        short: 'Buy Call',  kind: 'BUY',  tone: 'green', bar: 'bg-emerald-500', outlookTxt: 'Bullish · leveraged' },
  LONG_PUT:         { label: 'Buy Put',         short: 'Buy Put',   kind: 'BUY',  tone: 'red',   bar: 'bg-rose-500',    outlookTxt: 'Bearish · leveraged' },
  CASH_SECURED_PUT: { label: 'Sell Put (CSP)',  short: 'Sell Put',  kind: 'SELL', tone: 'green', bar: 'bg-emerald-700', outlookTxt: 'Bullish/neutral · income' },
  COVERED_CALL:     { label: 'Sell Call (CC)',  short: 'Sell Call', kind: 'SELL', tone: 'neutral', bar: 'bg-neutral-400', outlookTxt: 'Neutral/mild-bearish · income' },
}
// Bucket order for the desk: the two premium-buying trades first (that is what
// most people are looking for), then the two collateralized income trades.
const DESK_ORDER = ['LONG_CALL', 'LONG_PUT', 'CASH_SECURED_PUT', 'COVERED_CALL']

// ─── Entry-timing grade metadata (mirrors Symbols-to-Buy) ─────────────────────
const TIMING_META = {
  BUY_NOW:    { label: 'Buy Now',    text: 'text-emerald-400', bar: 'bg-emerald-500', cls: 'text-emerald-400 border-emerald-600/50 bg-emerald-600/10' },
  ACCUMULATE: { label: 'Accumulate', text: 'text-white',       bar: 'bg-white',       cls: 'text-white border-neutral-500/60 bg-neutral-700/30' },
  WAIT:       { label: 'Wait',       text: 'text-neutral-300', bar: 'bg-neutral-500', cls: 'text-neutral-300 border-neutral-600/50 bg-neutral-800/50' },
  AVOID:      { label: 'Avoid',      text: 'text-rose-400',    bar: 'bg-rose-500',    cls: 'text-rose-400 border-rose-600/50 bg-rose-600/10' },
  UNRATED:    { label: 'Unrated',    text: 'text-neutral-500', bar: 'bg-neutral-700', cls: 'text-neutral-500 border-neutral-700 bg-neutral-900/60' },
}
const timingMeta = (g) => TIMING_META[g] || TIMING_META.UNRATED
const TIMING_GRADES = ['BUY_NOW', 'ACCUMULATE', 'WAIT', 'AVOID']
const alignText = (a) => a === 'SUPPORTS' ? 'text-emerald-400' : a === 'CONFLICTS' ? 'text-rose-400' : 'text-neutral-400'
const tierText = (t) => t === 'PRIME' ? 'text-emerald-400' : t === 'STRONG' ? 'text-white' : t === 'WEAK' ? 'text-neutral-500' : 'text-neutral-300'

function TimingChip({ t }) {
  if (!t || typeof t !== 'object') return null
  const m = timingMeta(t.ok === false ? 'UNRATED' : t.grade)
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[10px] font-bold ${m.cls}`}>
      {m.label}{isNum(t.timing_score) ? ` ${Math.round(t.timing_score)}` : ''}
    </span>
  )
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
    amber: 'bg-neutral-800/60 border-neutral-600/50 text-neutral-200',
    blue: 'bg-white/10 border-white/25 text-white',
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
    : strat.liquidity_quality === 'FAIR' ? 'text-neutral-300' : 'text-rose-400'
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
            {isNum(strat.structure_score) && (
              <Chip tone={strat.is_aligned ? 'blue' : 'neutral'}>{strat.structure_tier || 'SCORE'} {Math.round(strat.structure_score)}</Chip>
            )}
            {strat.role && <Chip tone="neutral">{strat.role}</Chip>}
          </div>
          <div className="text-sm font-bold text-neutral-100 font-mono leading-tight truncate">
            <span className={isBuy ? 'text-emerald-400' : 'text-neutral-200'}>{strat.action}</span>
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
          ? <Stat label="Cash collateral" value={money(strat.collateral_required)} accent="text-neutral-100" />
          : strat.requires_shares > 0
            ? <Stat label="Shares needed" value={`${strat.requires_shares}`} accent="text-neutral-100" />
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
                  accent={strat.iv_environment === 'ELEVATED' ? 'text-white' : 'text-neutral-100'} />
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
            <div className="bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-3 py-2.5">
              <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-300 mb-1.5">Risk disclosures — review before execution</div>
              <ul className="space-y-1">
                {(strat.risk_disclosures || []).map((r, i) => (
                  <li key={i} className="text-[11px] text-neutral-400 flex gap-1.5">
                    <span className="text-neutral-500">•</span><span>{r}</span>
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

// ─── Entry-timing detail (same engine Symbols-to-Buy uses) ────────────────────
// Direction tells you WHICH way to trade. Timing tells you whether today is a
// defensible price to pay for it. On a long option that distinction is the whole
// trade: theta charges rent daily, so a correct view bought into a hostile tape
// still loses money. Scored on validated price history only — no extra requests.
function TimingBlock({ t, alignment, reason }) {
  if (!t || typeof t !== 'object') {
    return (
      <div className="bg-neutral-950/40 border border-neutral-800/60 rounded-lg px-3 py-2.5">
        <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-500 mb-1">Entry timing</div>
        <p className="text-[11px] text-neutral-500">
          No verified short-term timing read on this name — the ticket is graded on direction, conviction and
          option economics alone. Treated as neutral: never rewarded, never penalised.
        </p>
      </div>
    )
  }
  const m = timingMeta(t.ok === false ? 'UNRATED' : t.grade)
  const plan = t.entry_plan || {}
  const ev = t.event_risk || {}
  const caps = Array.isArray(t.grade_caps) ? t.grade_caps : []
  const signals = (Array.isArray(t.signals) ? t.signals : [])
    .filter(s => s && isNum(s.score))
    .sort((a, b) => a.score - b.score)
    .slice(0, 6)

  return (
    <div className="bg-neutral-950/40 border border-neutral-800/60 rounded-lg px-3 py-2.5">
      <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
        <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">Entry timing</div>
        <div className="flex items-center gap-1.5">
          <TimingChip t={t} />
          {alignment && (
            <span className={`text-[10px] font-bold uppercase tracking-wider ${alignText(alignment)}`}>
              {alignment === 'SUPPORTS' ? 'tape agrees' : alignment === 'CONFLICTS' ? 'tape disagrees' : 'tape neutral'}
            </span>
          )}
        </div>
      </div>

      {t.ok === false ? (
        <p className="text-[11px] text-neutral-500">
          {t.reason === 'price_mismatch'
            ? 'Timing withheld: the cached price history disagrees with the live quote by more than 5%. A timing score computed on the wrong series would be confident, specific and false, so it is suppressed rather than guessed.'
            : (t.summary || 'No verified short-term timing read available — treated as neutral.')}
        </p>
      ) : (
        <>
          <p className="text-[11px] text-neutral-300 leading-relaxed">{t.summary || reason}</p>
          {reason && t.summary && <p className="text-[10px] text-neutral-500 mt-0.5">{reason}</p>}
          {caps.length > 0 && (
            <p className="text-[10px] text-rose-300/80 mt-1">Grade capped: {caps.join(' · ')}</p>
          )}

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
            <Stat label="Entry zone" value={isNum(plan.entry_zone_low) ? `${px(plan.entry_zone_low)}–${px(plan.entry_zone_high)}` : '—'} />
            <Stat label="Ideal entry" value={px(plan.ideal_entry)} />
            <Stat label="Thesis invalid below" value={px(plan.structural_stop)} accent="text-rose-400" />
            <Stat label="1-ATR adverse move" value={isNum(t.near_term_risk_pct) ? pctPos(t.near_term_risk_pct) : '—'} />
          </div>

          {signals.length > 0 && (
            <div className="mt-2">
              <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-600 mb-1">Weakest signals first</div>
              <div className="space-y-1">
                {signals.map((s, i) => (
                  <div key={s.name || i} className="flex items-center gap-2">
                    <span className="text-[10px] text-neutral-500 w-28 shrink-0 truncate">{s.label || s.name}</span>
                    <span className="flex-1 h-1 bg-neutral-800 rounded-full overflow-hidden">
                      <span
                        className={`block h-full ${s.state === 'BULL' ? 'bg-emerald-500' : s.state === 'BEAR' ? 'bg-rose-500' : 'bg-neutral-500'}`}
                        style={{ width: `${Math.max(0, Math.min(100, s.score))}%` }}
                      />
                    </span>
                    <span className="text-[10px] font-mono text-neutral-400 w-7 text-right shrink-0">{Math.round(s.score)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {Array.isArray(ev.items) && ev.items.length > 0 && (
            <p className="text-[10px] text-neutral-500 mt-2">
              <span className="font-bold text-neutral-400">Event watch ({String(ev.level || 'LOW').toLowerCase()}):</span>{' '}
              {ev.items.filter(x => typeof x === 'string').join(' · ')}
            </p>
          )}
        </>
      )}
    </div>
  )
}

// ─── Trade Today ──────────────────────────────────────────────────────────────
// The decisive answer to "what do I actually trade?", split into the four
// structures: buy a call, buy a put, sell a put, sell a call.
//
// A row appears only when BOTH hold:
//   1. the structure agrees with the engine's directional read (is_aligned), so
//      a counter-view income trade can never masquerade as the desk's call, and
//   2. its per-structure score clears DESK_MIN_SCORE.
// Per-structure scoring matters because buying and selling premium have OPPOSITE
// preferences on implied vol, on theta and on where the tape is — ranking a
// cash-secured put with the long-call formula recommends the wrong trade.
// An empty bucket is a real answer, not a failure.
const DESK_MIN_SCORE = 55          // STRONG or better
const DESK_ROWS_PER_BUCKET = 5

function deskBuckets(recs) {
  const buckets = { LONG_CALL: [], LONG_PUT: [], CASH_SECURED_PUT: [], COVERED_CALL: [] }
  let considered = 0
  for (const r of recs || []) {
    const strats = Array.isArray(r?.strategies) ? r.strategies : []
    for (const s of strats) {
      if (!s || !buckets[s.structure]) continue
      considered += 1
      if (!s.is_aligned) continue
      if (!isNum(s.structure_score) || s.structure_score < DESK_MIN_SCORE) continue
      buckets[s.structure].push({ rec: r, s })
    }
  }
  for (const k of DESK_ORDER) buckets[k].sort((a, b) => b.s.structure_score - a.s.structure_score)
  return { buckets, considered }
}

function DeskRow({ rec, s }) {
  const credit = s.net_type === 'CREDIT'
  return (
    <div className="grid grid-cols-12 gap-2 px-4 py-2.5 border-b border-neutral-800/40 items-center text-xs hover:bg-neutral-800/20">
      <div className="col-span-3 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="font-bold text-base text-white truncate">{rec.ticker}</span>
          <span className={`text-[9px] font-bold uppercase tracking-wider ${tierText(s.structure_tier)}`}>{s.structure_tier}</span>
        </div>
        <div className="text-[10px] text-neutral-600 truncate">
          {px(rec.underlying_price)} spot · {isNum(rec.confidence) ? `conf ${rec.confidence}%` : '—'}
        </div>
      </div>
      <div className="col-span-3 min-w-0">
        <div className="font-mono text-[11px] text-neutral-200 truncate">
          {s.contracts}× {s.expiration_human} {px(s.strike)} {s.option_type}
        </div>
        <div className="text-[10px] text-neutral-600 truncate">
          {isNum(s.dte) ? `${s.dte} DTE` : '—'} · limit ≈ {px(s.est_premium_per_share)}/sh
        </div>
      </div>
      <div className="col-span-2 text-right font-mono">
        <div className={credit ? 'text-emerald-400' : 'text-white'}>
          {credit ? money(s.net_credit) : money(s.net_debit)}
        </div>
        <div className="text-[9px] text-neutral-600">{credit ? 'credit in' : 'cash out'}</div>
      </div>
      <div className="col-span-2 text-right font-mono">
        <div className="text-rose-300/80">{money(s.max_loss)}</div>
        <div className="text-[9px] text-neutral-600">max loss</div>
      </div>
      <div className="col-span-1 text-right font-mono">
        <div className="text-neutral-200">{px(s.breakeven)}</div>
        <div className="text-[9px] text-neutral-600">B/E</div>
      </div>
      <div className="col-span-1 text-right font-mono">
        <div className={tierText(s.structure_tier)}>{Math.round(s.structure_score)}</div>
        <div className="text-[9px] text-neutral-600">{timingMeta(s.timing_grade).label === 'Unrated' ? 'score' : timingMeta(s.timing_grade).label.toLowerCase()}</div>
      </div>
    </div>
  )
}

function DeskBucket({ structure, rows, considered }) {
  const m = STRUCTURE_META[structure]
  const count = rows.length
  return (
    <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-sm shrink-0 ${m.bar}`} />
            <h3 className="text-sm font-bold text-white">{m.label}</h3>
          </div>
          <p className="text-[11px] text-neutral-500 mt-0.5">{m.outlookTxt} · {m.kind === 'BUY' ? 'defined risk, max loss = premium' : 'collateralized, never naked'}</p>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-xl font-bold font-mono ${count ? (m.tone === 'red' ? 'text-rose-400' : m.tone === 'green' ? 'text-emerald-400' : 'text-white') : 'text-neutral-600'}`}>{count}</div>
          <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">qualified</div>
        </div>
      </div>
      {count === 0 ? (
        <div className="px-4 py-4">
          <p className="text-[12px] text-neutral-400 font-bold">Nothing qualifies.</p>
          <p className="text-[10px] text-neutral-600 mt-1 leading-snug">
            {considered
              ? `No ${m.label.toLowerCase()} in the current scan both agrees with its directional read and scores ${DESK_MIN_SCORE}+ for this structure. `
              : 'Scan has not produced a graded structure for this leg yet. '}
            Forcing the trade anyway means paying premium for a setup the engine does not rate.
          </p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-12 gap-2 px-4 py-2 bg-neutral-900/60 border-b border-neutral-800/60 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
            <div className="col-span-3">Ticker</div>
            <div className="col-span-3">Contract</div>
            <div className="col-span-2 text-right">Net</div>
            <div className="col-span-2 text-right">Risk</div>
            <div className="col-span-1 text-right">B/E</div>
            <div className="col-span-1 text-right">Score</div>
          </div>
          {rows.slice(0, DESK_ROWS_PER_BUCKET).map(({ rec, s }) => (
            <DeskRow key={rec.ticker} rec={rec} s={s} />
          ))}
          {count > DESK_ROWS_PER_BUCKET && (
            <div className="px-4 py-2 text-[10px] text-neutral-600">
              + {count - DESK_ROWS_PER_BUCKET} more below the top {DESK_ROWS_PER_BUCKET} — see the full tickets.
            </div>
          )}
        </>
      )}
    </div>
  )
}

function TradeToday({ recs }) {
  const { buckets, considered } = deskBuckets(recs)
  const total = DESK_ORDER.reduce((n, k) => n + buckets[k].length, 0)
  const filled = DESK_ORDER.filter(k => buckets[k].length > 0).length
  const best = DESK_ORDER
    .map(k => buckets[k][0])
    .filter(Boolean)
    .sort((a, b) => b.s.structure_score - a.s.structure_score)[0]

  return (
    <div className="space-y-4">
      <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-neutral-800/60 bg-neutral-900/60 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold text-white">Trade Today</h2>
            <p className="text-xs text-neutral-500 mt-0.5">
              The four ways to express a view — buy a call, buy a put, sell a put, sell a call — each ranked by a
              score built for that structure, not a blended one. Buying and selling premium want opposite implied
              vol, opposite theta and opposite tape.
            </p>
          </div>
          <div className="text-right shrink-0">
            <div className={`text-2xl font-bold font-mono ${total ? 'text-emerald-400' : 'text-rose-400'}`}>{total}</div>
            <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">Qualified trades</div>
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-neutral-800/60">
          {DESK_ORDER.map(k => {
            const m = STRUCTURE_META[k]
            const n = buckets[k].length
            return (
              <div key={k} className="px-4 py-3">
                <div className="text-[9px] font-bold uppercase tracking-widest text-neutral-500">{m.short}</div>
                <div className={`text-2xl font-bold font-mono ${n ? (m.tone === 'red' ? 'text-rose-400' : m.tone === 'green' ? 'text-emerald-400' : 'text-white') : 'text-neutral-600'}`}>{n}</div>
                <div className="text-[10px] text-neutral-600 mt-0.5">
                  {n ? `top score ${Math.round(buckets[k][0].s.structure_score)} · ${buckets[k][0].rec.ticker}` : 'no qualifying setup'}
                </div>
              </div>
            )
          })}
        </div>
        <div className="px-4 py-3 border-t border-neutral-800/60">
          <div className="flex h-2 rounded-full overflow-hidden bg-neutral-800 mb-2">
            {total > 0 && DESK_ORDER.map(k => buckets[k].length > 0 && (
              <div key={k} className={STRUCTURE_META[k].bar} style={{ width: `${buckets[k].length / total * 100}%` }} />
            ))}
          </div>
          <div className="text-[10px] text-neutral-600 leading-snug">
            {total === 0 ? (
              <span className="text-rose-300/90 font-bold">
                No structure clears the bar right now. {considered} candidate legs were scored and none combined an
                aligned directional read with a {DESK_MIN_SCORE}+ structure score. Holding cash and paying no premium is the position.
              </span>
            ) : (
              <>
                {filled} of 4 structures have a qualifying trade · {considered} candidate legs scored ·
                {best ? ` desk's single best read: ${STRUCTURE_META[best.s.structure].label} on ${best.rec.ticker} at ${Math.round(best.s.structure_score)}` : ''}.
                {' '}Qualified = structure agrees with its directional read AND scores ≥ {DESK_MIN_SCORE}. Work limit orders — do not pay the market.
              </>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {DESK_ORDER.map(k => (
          <CardBoundary key={k} ticker={STRUCTURE_META[k].label}>
            <DeskBucket structure={k} rows={buckets[k]} considered={considered} />
          </CardBoundary>
        ))}
      </div>
    </div>
  )
}

// ─── The order ticket card ────────────────────────────────────────────────────
// Compact by default: one glance = ticker, the trade, cost, max loss. Click the
// card to reveal the full detail (rationale, all analytics chips, every strategy
// with order steps + risk). Keeps the grid scannable instead of bulky.
function OrderTicket({ rec }) {
  const [expanded, setExpanded] = useState(false)
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
  const primary = strategies.find(s => s.is_primary) || strategies[0] || rec

  const alignTone = rec.fundamental_alignment === 'AGREES' ? 'green'
    : rec.fundamental_alignment === 'DIVERGES' ? 'red' : 'neutral'
  const newsTone = rec.news_alignment === 'AGREES' ? 'green'
    : rec.news_alignment === 'DIVERGES' ? 'red' : 'neutral'
  const macroTone = rec.macro_alignment === 'SUPPORTS' ? 'green'
    : rec.macro_alignment === 'CAUTION' ? 'amber' : 'neutral'
  const headMoney = primary.net_type === 'CREDIT'
    ? { label: 'Credit', val: money(primary.net_credit), tone: 'text-emerald-400' }
    : { label: 'Total cost', val: money(primary.net_debit), tone: 'text-white' }

  return (
    <div className={`bg-neutral-900/50 border border-neutral-700 rounded-xl overflow-hidden ${accentClass}`}>
      {/* Compact header — always visible; click toggles full detail */}
      <button onClick={() => setExpanded(e => !e)} className="w-full text-left px-5 py-4 flex items-start justify-between gap-3 hover:bg-neutral-900/40 transition-colors">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className="text-xl font-black text-white tracking-tight">{rec.ticker}</span>
            <Chip tone={dirTone}>{isCall ? '▲ BULLISH' : '▼ BEARISH'}</Chip>
            {isNum(rec.opportunity_score) && <Chip tone="blue">{rec.opportunity_tier || 'SCORE'} {rec.opportunity_score}</Chip>}
            {isNum(rec.confidence) && <Chip tone="blue">conf {rec.confidence}%</Chip>}
            <TimingChip t={rec.entry_timing} />
            {stb.in_stb_longs && <Chip tone="green">★ STB</Chip>}
            {isNum(rec.earnings?.next_earnings_days) && <Chip tone="amber">⚠ earnings {rec.earnings.next_earnings_days}d</Chip>}
            {rec.stale_reused && <Chip tone="amber">stale</Chip>}
          </div>
          <div className="text-sm font-bold text-neutral-100 font-mono leading-tight truncate">
            <span className={isCall ? 'text-emerald-400' : 'text-rose-400'}>{primary.action}</span>
            {' '}{primary.contracts}× {rec.ticker} {primary.expiration_human} {px(primary.strike)} {primary.option_type}
          </div>
          {/* Structure chips — surface EVERY way to trade this view (buy + */}
          {/* collateralized sell) right in the compact header, so calls, puts, */}
          {/* and sells are all visible at a glance without expanding the card. */}
          <div className="flex items-center gap-1.5 flex-wrap mt-1">
            {strategies.map((s, i) => {
              const m = STRUCTURE_META[s.structure] || { label: s.title || s.structure, tone: 'neutral' }
              return (
                <Chip key={s.structure || i} tone={s.is_aligned ? m.tone : 'neutral'}>
                  {s.is_primary ? '★ ' : ''}{m.label}{isNum(s.structure_score) ? ` ${Math.round(s.structure_score)}` : ''}
                </Chip>
              )
            })}
          </div>
          <div className="text-[11px] text-neutral-500 mt-0.5">
            underlying {px(rec.underlying_price)} · {isNum(primary.dte) ? primary.dte + ' DTE' : '—'} · limit ≈ {px(primary.est_premium_per_share)}/sh · BE {px(primary.breakeven)} · {strategies.length} way{strategies.length !== 1 ? 's' : ''}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">{headMoney.label}</div>
          <div className={`text-lg font-black font-mono ${headMoney.tone}`}>{headMoney.val}</div>
          <div className="text-[10px] text-rose-400 mt-0.5">max loss {money(primary.max_loss)}</div>
          <div className="text-[10px] text-neutral-500 mt-0.5">{expanded ? '▲ hide' : '▼ full detail'}</div>
        </div>
      </button>

      {expanded && (
        <>
          {/* Rationale + analysis chips */}
          <div className="px-5 py-4 space-y-3 border-t border-neutral-800/70">
            <div className="text-xs text-neutral-500">signal {rec.signal || '—'} · {rec.regime || 'NEUTRAL'} regime{rec.signal_confluence ? ` · confluence ${rec.signal_confluence}` : ''}</div>
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
            <TimingBlock t={rec.entry_timing} alignment={rec.timing_alignment} reason={rec.timing_reason} />
          </div>

          {/* Strategies — buy + collateralized-sell */}
          <div className="px-5 py-4 space-y-3">
            <div className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">Ways to trade this view · ranked per structure</div>
            {strategies.map((s, i) => (
              <StrategyBlock key={s.structure || i} strat={s} ticker={rec.ticker} underlying={rec.underlying_price} defaultOpen={i === 0} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Client-side cache (survives reloads so we ALWAYS show last-good picks) ────
// v2: bumped to invalidate any pre-direction-fix cache (old caches could be
// calls-only from before the interleave + call/put filter shipped). Bumping the
// key forces every client to re-scan once on first load after deploy, so the
// true bullish/bearish mix always shows instead of a stale calls-only snapshot.
const CACHE_KEY = 'optionsTrading:v2'
const CACHE_TTL = 30 * 60 * 1000   // 30 min — matches the backend anti-flap window
const REQ_TIMEOUT = 12000          // per-request cap so one hung fetch can't stall the scan
const REQ_RETRIES = 2              // attempts per ticker before giving up this pass
// Safety net: if Symbols-to-Buy is momentarily empty (cold cache / warming up),
// fall back to these liquid, deep-options large-caps so the page ALWAYS shows
// picks. Each still runs through the same gated recommendation engine, so a
// fallback ticker only surfaces a card if it independently earns an actionable
// signal — no fabricated trades. STB stays the source of truth when it's warm.
// Broad, liquid, deep-options mainstream S&P names across every sector. The
// options page scans THIS whole set (merged with STB's long picks) so the engine
// can surface BOTH directions — bullish names → calls, bearish names → puts —
// instead of only STB's long-only (bullish) list. Each ticker still runs through
// the same gated engine: a card appears only if it independently earns an
// actionable signal (no fabricated trades). STB stays long-only and untouched.
const SP_MAINSTREAM = [
  // Mega-cap tech & semis
  'AAPL', 'MSFT', 'NVDA', 'AMD', 'AVGO', 'CRM', 'ORCL', 'ADBE', 'INTC', 'QCOM',
  'TXN', 'CSCO', 'IBM', 'MU', 'AMAT', 'NOW', 'ANET', 'PANW', 'CRWD', 'SNPS',
  'CDNS', 'KLAC', 'LRCX', 'MRVL', 'ADI', 'MCHP', 'FTNT', 'ADSK', 'INTU', 'SMCI',
  // Communication & media
  'GOOGL', 'META', 'NFLX', 'DIS', 'CMCSA', 'T', 'VZ', 'TMUS', 'CHTR', 'EA', 'TTWO', 'WBD',
  // Consumer discretionary
  'AMZN', 'TSLA', 'HD', 'MCD', 'NKE', 'SBUX', 'LOW', 'TGT', 'BKNG', 'TJX', 'ROST',
  'ORLY', 'AZO', 'YUM', 'CMG', 'MAR', 'GM', 'F', 'ABNB', 'DASH', 'EBAY', 'LULU',
  // Consumer staples
  'WMT', 'COST', 'PG', 'KO', 'PEP', 'PM', 'MO', 'CL', 'KMB', 'GIS', 'KHC', 'STZ',
  'MDLZ', 'KDP', 'HSY', 'SYY',
  // Financials & payments
  'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'AXP', 'BLK', 'SCHW', 'V', 'MA', 'PYPL',
  'USB', 'PNC', 'TFC', 'COF', 'MET', 'PRU', 'AIG', 'CB', 'ICE', 'CME', 'SPGI', 'MCO', 'PGR',
  // Healthcare
  'UNH', 'JNJ', 'LLY', 'PFE', 'MRK', 'ABBV', 'TMO', 'ABT', 'BMY', 'AMGN', 'GILD', 'CVS',
  'DHR', 'ISRG', 'SYK', 'MDT', 'ELV', 'CI', 'HUM', 'VRTX', 'REGN', 'ZTS', 'BSX', 'BDX', 'HCA', 'MRNA', 'BIIB',
  // Energy
  'XOM', 'CVX', 'COP', 'SLB', 'OXY', 'MPC', 'PSX', 'EOG', 'VLO', 'KMI', 'WMB', 'HAL', 'DVN', 'HES',
  // Industrials
  'BA', 'CAT', 'GE', 'HON', 'UPS', 'RTX', 'LMT', 'DE', 'UNP', 'MMM', 'EMR', 'ETN',
  'ITW', 'GD', 'NOC', 'NSC', 'FDX', 'WM', 'PH', 'CSX',
  // Materials, utilities & real estate
  'LIN', 'SHW', 'FCX', 'NEM', 'APD', 'DOW', 'NUE', 'ECL', 'NEE', 'DUK', 'SO', 'AMT', 'PLD', 'EQIX', 'O',
]
// Hard cap so the scan can never run away, even if STB returns a huge list.
const MAX_UNIVERSE = 200
// Bounded concurrency: a small worker pool scans several tickers at once so the
// grid fills in ~1/SCAN_CONCURRENCY of the sequential time (a 184-name scan drops
// from ~6 min to ~2 min) while staying well under the backend's 200-req/60s limit
// (~106 req/min at 3 workers, leaving headroom for concurrent quote calls and the
// shared App-Runner IP). The endpoint already throttles Yahoo and guards each
// chain fetch with a 10s thread-timeout, so a few in-flight requests are safe.
const SCAN_CONCURRENCY = 3
// Only scan well-formed tickers (defense-in-depth; endpoint validates too).
const isValidTicker = (t) => typeof t === 'string' && /^[A-Z][A-Z.\-]{0,6}$/.test(t)

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
  const [sortBy, setSortBy] = useState('opportunity')  // best-first by default
  const [dirFilter, setDirFilter] = useState('all')    // all | calls | puts — keeps puts visible when opportunity-sort would bury them
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
      // STB long picks (bullish, long-only) + broad S&P mainstream so the engine
      // can surface BOTH calls and puts. Deduped, format-validated, hard-capped.
      let stbPicks = []
      try {
        stbPicks = ((j && j.long_picks) || []).map(p => p && p.ticker).filter(Boolean)
      } catch { stbPicks = [] }
      const stbCold = !stbPicks.length
      let list = []
      try {
        // INTERLEAVE STB (long-only → bullish) with the broad S&P set so the scan
        // is directionally MIXED as it streams in. Concatenating STB-first would
        // front-load every bullish name and only reach the bearish (put) candidates
        // at the very end — making the page look calls-only mid-scan. Alternating
        // lets calls AND puts surface together from the start.
        const merged = []
        const maxLen = Math.max(stbPicks.length, SP_MAINSTREAM.length)
        for (let i = 0; i < maxLen; i++) {
          if (i < stbPicks.length) merged.push(stbPicks[i])
          if (i < SP_MAINSTREAM.length) merged.push(SP_MAINSTREAM[i])
        }
        list = [...new Set(merged)]
          .map(t => String(t).toUpperCase().trim())
          .filter(isValidTicker)
          .slice(0, MAX_UNIVERSE)
      } catch {
        list = SP_MAINSTREAM.slice(0, MAX_UNIVERSE)   // last-ditch safety net
      }
      if (!list.length) list = [...SP_MAINSTREAM]      // never end up empty
      if (list.length) {
        setTickers(list)
        setLoading(false)   // universe known → stop the full-page blocker; cards stream in
        setProgress({ done: 0, total: list.length })
        // Bounded-concurrency worker pool: SCAN_CONCURRENCY tickers scan at once so
        // cards stream in fast, while each worker staggers its own successive calls
        // to stay rate-limit-safe. Each fetch has its own timeout+retry.
        let done = 0
        let idx = 0
        const worker = async () => {
          while (true) {
            const myIdx = idx++
            if (myIdx >= list.length) return
            await fetchRec(list[myIdx])
            done += 1
            setProgress({ done, total: list.length })
            await new Promise(r => setTimeout(r, 200))
          }
        }
        await Promise.all(
          Array.from({ length: Math.min(SCAN_CONCURRENCY, list.length) }, worker)
        )
        // If STB was cold, quietly re-pull once it's warm so its picks join the set.
        if (stbCold && !force) {
          setTimeout(() => { runningRef.current = false; loadUniverse(true) }, 45000)
        }
      } else if (!Object.keys(recsRef.current).length) {
        // No universe AND nothing cached to show.
        setError((j && j.message) || 'Screening universe is initializing — please retry shortly.')
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
  const actionableRaw = ordered.filter(r => r.actionable)
  // Rank the actionable trades so the strongest opportunities surface first
  // (mirrors how Symbols-to-Buy ranks its picks). All keys palette-neutral.
  const primaryOf = (r) => (Array.isArray(r.strategies) && r.strategies.find(s => s.is_primary)) || (r.strategies && r.strategies[0]) || r
  const costOf = (r) => { const p = primaryOf(r); return isNum(p.net_debit) ? p.net_debit : (isNum(r.est_total_cost) ? r.est_total_cost : Infinity) }
  const beProxOf = (r) => { const m = r.options_analytics?.breakeven_move_pct; return isNum(m) ? Math.abs(m) : Infinity }
  const timingOf = (r) => { const t = r.entry_timing; return (t && t.ok && isNum(t.timing_score)) ? t.timing_score : -1 }
  const SORTS = {
    opportunity: { label: 'Opportunity', fn: (a, b) => (b.opportunity_score ?? -1) - (a.opportunity_score ?? -1) },
    structure:   { label: 'Best structure', fn: (a, b) => (b.best_structure_score ?? -1) - (a.best_structure_score ?? -1) },
    timing:      { label: 'Entry timing', fn: (a, b) => timingOf(b) - timingOf(a) },
    conviction:  { label: 'Conviction',  fn: (a, b) => (b.confidence ?? -1) - (a.confidence ?? -1) },
    cost:        { label: 'Cost (low→high)', fn: (a, b) => costOf(a) - costOf(b) },
    breakeven:   { label: 'Breakeven proximity', fn: (a, b) => beProxOf(a) - beProxOf(b) },
  }
  const actionable = [...actionableRaw].sort((SORTS[sortBy] || SORTS.opportunity).fn)
  const callCount = actionable.filter(r => r.option_type === 'CALL').length
  const putCount = actionable.filter(r => r.option_type === 'PUT').length
  const shown = dirFilter === 'all'
    ? actionable
    : actionable.filter(r => r.option_type === (dirFilter === 'calls' ? 'CALL' : 'PUT'))
  const scanning = progress.total > 0 && progress.done < progress.total
  const cacheAgeMin = cached?.ts ? Math.round((Date.now() - cached.ts) / 60000) : null

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-6">
        <div className="flex items-end justify-between mb-2 flex-wrap gap-3">
          <div>
            <h1 className="text-3xl font-black text-white tracking-tight">Options Trading</h1>
            <p className="text-sm text-neutral-400 mt-1">
              Buy calls, buy puts, sell cash-secured puts, sell covered calls — each scored on its own terms, because buying and selling premium want opposite implied vol, opposite theta and opposite tape. Direction from the technical, fundamental, news and macro stack; entry graded by the same 14-signal timing engine Symbols-to-Buy uses. Recommendation only.
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
            className="px-3 py-2 w-64 bg-neutral-900 border border-neutral-700 rounded-lg text-sm text-white font-mono placeholder-neutral-600 focus:border-neutral-500 focus:outline-none uppercase"
          />
          <button
            type="submit"
            disabled={lookupBusy}
            className="px-4 py-2 bg-white text-black hover:bg-neutral-200 disabled:opacity-50 rounded-lg text-sm font-bold transition-colors"
          >
            {lookupBusy ? 'Loading…' : 'Analyze options'}
          </button>
        </form>

        {/* Safety banner */}
        <div className="mt-4 bg-neutral-900/50 border border-neutral-700/50 rounded-lg px-4 py-2.5 text-xs text-neutral-300 flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="font-bold text-white">Risk framework:</span>
          <span>Long calls/puts (max loss = premium) plus collateralized selling (cash-secured puts / covered calls)</span>
          <span className="text-neutral-600">·</span>
          <span>No naked short premium</span>
          <span className="text-neutral-600">·</span>
          <span>Withholds on low conviction, thin liquidity, price disagreement, earnings IV-crush windows, or a tape that fights the trade</span>
          <span className="text-neutral-600">·</span>
          <span>Recommendation only — never auto-executes</span>
        </div>
      </div>

      {/* Full-page blocker ONLY when we truly have nothing to show yet. */}
      {loading && ordered.length === 0 && (
        <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-12 text-center text-neutral-500">
          Scanning options chains across the universe…
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
            <div className="h-full bg-white transition-all duration-300" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
        </div>
      )}

      {error && !loading && (
        <div className="bg-neutral-900/50 border border-neutral-700/50 rounded-lg p-4 text-neutral-300 mb-6 text-sm">
          {error}
          <button onClick={() => loadUniverse(false)} className="ml-4 text-xs underline text-neutral-400 hover:text-white">Retry</button>
        </div>
      )}

      {(!loading || ordered.length > 0) && (
        <div className="space-y-6">
          {/* The decisive desk — what to trade, split by structure. */}
          {actionable.length > 0 && <TradeToday recs={actionable} />}

          {actionable.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-3 flex-wrap">
                <h2 className="text-lg font-bold text-white">Full order tickets</h2>
                <span className="text-xs text-neutral-500">{shown.length} of {actionable.length}</span>
                <button
                  onClick={() => setDirFilter('all')}
                  className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${dirFilter === 'all' ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
                >
                  All {actionable.length}
                </button>
                <button
                  onClick={() => setDirFilter('calls')}
                  className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${dirFilter === 'calls' ? 'bg-emerald-500 text-black border-emerald-500' : 'bg-neutral-900 border-neutral-700 text-emerald-400 hover:bg-neutral-800'}`}
                >
                  ▲ {callCount} bullish
                </button>
                <button
                  onClick={() => setDirFilter('puts')}
                  className={`px-2 py-1 rounded border text-[11px] font-bold transition-colors ${dirFilter === 'puts' ? 'bg-rose-500 text-black border-rose-500' : 'bg-neutral-900 border-neutral-700 text-rose-400 hover:bg-neutral-800'}`}
                >
                  ▼ {putCount} bearish
                </button>
                <div className="ml-auto flex items-center gap-1 flex-wrap">
                  <span className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold mr-1">Sort</span>
                  {Object.entries(SORTS).map(([k, v]) => (
                    <button
                      key={k}
                      onClick={() => setSortBy(k)}
                      className={`px-2 py-1 rounded border text-[10px] font-bold transition-colors ${sortBy === k ? 'bg-white text-black border-white' : 'bg-neutral-900 border-neutral-700 text-neutral-300 hover:bg-neutral-800'}`}
                    >
                      {v.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {shown.map(r => (
                  <CardBoundary key={r.ticker} ticker={r.ticker}>
                    <OrderTicket rec={r} />
                  </CardBoundary>
                ))}
              </div>
              {shown.length === 0 && (
                <div className="text-neutral-500 text-sm py-6 text-center">
                  No {dirFilter === 'puts' ? 'bearish (put)' : 'bullish (call)'} trades in the current scan.
                </div>
              )}
            </div>
          )}

          {actionable.length === 0 && !error && !scanning && (
            <div className="bg-neutral-900/40 border border-neutral-800/60 rounded-xl p-8 text-center text-neutral-500">
              No actionable options trades right now — the engine is withholding until conviction, liquidity, and price all line up.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
