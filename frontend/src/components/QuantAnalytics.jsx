import { Component, useEffect, useMemo, useState } from 'react'
import {
  ResponsiveContainer, ComposedChart, AreaChart, Area, Line, Bar, BarChart,
  LineChart, XAxis, YAxis, CartesianGrid, Tooltip, ReferenceLine,
  RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar, Cell,
} from 'recharts'
import {
  closesFrom, gbmProjection, probReach, histogram, volCone, drawdownSeries,
  monthlySeasonality, classifyRegime, relativeSeries, earningsReactions,
  factorScores, sectorEtf, dailyReturns, mean, stdev,
} from '../lib/quantAnalytics'

// ══════════════════════════════════════════════════════════════════════════════
// QUANT ANALYTICS — the Analyze-page "cool + useful" section.
// Palette discipline: green = bullish, red = bearish, everything else
// white/neutral/black. NO blue/amber/yellow/etc — EVER.
// Every panel is DOUBLE fail-safe: (1) the math returns empty on bad data so
// the panel renders null, and (2) a per-panel error boundary catches any
// render/recharts exception so one broken panel can never break the section
// or the page. All network fetches are fail-isolated + timed out.
// ══════════════════════════════════════════════════════════════════════════════

const GREEN = '#22c55e'
const RED = '#ef4444'
const WHITE = '#ffffff'
const NEUTRAL = '#a3a3a3'
const NEUTRAL_DIM = '#737373'
const GRID = '#404040'

// ── formatting ────────────────────────────────────────────────────────────────
const fmtPct = (v, d = 1) => (v == null || !Number.isFinite(v)) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(d) + '%'
const fmtMoney = (v) => (v == null || !Number.isFinite(v)) ? '—' : '$' + Number(v).toFixed(2)
const fmtNum = (v, d = 1) => (v == null || !Number.isFinite(v)) ? '—' : Number(v).toFixed(d)

// ── per-panel error boundary: a broken panel disappears, page stays alive ─────
class PanelBoundary extends Component {
  constructor(p) { super(p); this.state = { dead: false } }
  static getDerivedStateFromError() { return { dead: true } }
  componentDidCatch(e) { try { console.error('QuantAnalytics panel error:', e) } catch {} }
  render() { return this.state.dead ? null : this.props.children }
}

// ── shared card shell ─────────────────────────────────────────────────────────
function Card({ title, subtitle, children, right }) {
  return (
    <div className="bg-neutral-900/40 border border-neutral-800 rounded-xl p-5">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h3 className="text-sm font-black text-white tracking-tight">{title}</h3>
          {subtitle && <p className="text-[11px] text-neutral-500 mt-0.5 leading-tight">{subtitle}</p>}
        </div>
        {right}
      </div>
      {children}
    </div>
  )
}

// safe JSON fetch with timeout — never throws, returns null on any failure.
async function safeJson(url, timeoutMs = 9000) {
  try {
    const ctrl = new AbortController()
    const t = setTimeout(() => { try { ctrl.abort() } catch {} }, timeoutMs)
    const res = await fetch(url, { signal: ctrl.signal })
    clearTimeout(t)
    if (!res || !res.ok) return null
    return await res.json()
  } catch { return null }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// ══════════════════════════════════════════════════════════════════════════════
// 1) MONTE CARLO / GBM PROJECTION CONE
// ══════════════════════════════════════════════════════════════════════════════
function MonteCarloPanel({ closes, targets }) {
  const proj = useMemo(() => gbmProjection(closes, { horizon: 60 }), [closes])
  if (!proj.stats || proj.points.length < 5) return null
  const s = proj.stats
  const medColor = s.muAnnPct > 0.5 ? GREEN : s.muAnnPct < -0.5 ? RED : WHITE
  const pTarget = targets?.target != null ? probReach(closes, targets.target, 60) : null
  const pStop = targets?.stop != null ? probReach(closes, targets.stop, 60) : null

  const CustomTip = ({ active, payload, label }) => {
    if (!active || !payload || !payload.length) return null
    const pt = payload[0]?.payload
    if (!pt) return null
    return (
      <div className="bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2 text-[11px]">
        <div className="text-neutral-400">Day +{label}</div>
        <div className="text-white font-mono">median {fmtMoney(pt.median)}</div>
        {Array.isArray(pt.band90) && (
          <div className="text-neutral-500 font-mono">80% range {fmtMoney(pt.band90[0])} – {fmtMoney(pt.band90[1])}</div>
        )}
      </div>
    )
  }

  return (
    <Card
      title="Monte Carlo price projection"
      subtitle="60-trading-day outcome cone — analytic lognormal (GBM), drift & σ from realized returns"
    >
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={proj.points} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis dataKey="day" stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }}
            tickFormatter={(d) => d === 0 ? 'now' : `+${d}d`} interval={9} />
          <YAxis stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} domain={['auto', 'auto']}
            tickFormatter={(v) => '$' + Math.round(v)} width={48} />
          <Tooltip content={<CustomTip />} />
          <Area type="monotone" dataKey="band90" stroke="none" fill={NEUTRAL} fillOpacity={0.08} isAnimationActive={false} />
          <Area type="monotone" dataKey="band50" stroke="none" fill={NEUTRAL} fillOpacity={0.16} isAnimationActive={false} />
          <Line type="monotone" dataKey="median" stroke={medColor} strokeWidth={2} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-3 gap-2 mt-3 text-center">
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Expected 60d move</div>
          <div className="text-sm font-mono font-bold text-white">±{fmtNum(s.expMoveUpPct)}%</div>
        </div>
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">P(up in 60d)</div>
          <div className={`text-sm font-mono font-bold ${s.probUpPct >= 50 ? 'text-emerald-400' : 'text-rose-400'}`}>{fmtNum(s.probUpPct, 0)}%</div>
        </div>
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Annualized σ</div>
          <div className="text-sm font-mono font-bold text-white">{fmtNum(s.sigAnnPct, 0)}%</div>
        </div>
      </div>
      {(pTarget != null || pStop != null) && (
        <div className="grid grid-cols-2 gap-2 mt-2 text-center">
          {pTarget != null && (
            <div className="bg-emerald-950/20 border border-emerald-900/40 rounded-lg px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-neutral-500">P(hit target {fmtMoney(targets.target)})</div>
              <div className="text-sm font-mono font-bold text-emerald-300">{fmtNum(pTarget, 0)}%</div>
            </div>
          )}
          {pStop != null && (
            <div className="bg-rose-950/20 border border-rose-900/40 rounded-lg px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-neutral-500">P(hit stop {fmtMoney(targets.stop)})</div>
              <div className="text-sm font-mono font-bold text-rose-300">{fmtNum(pStop, 0)}%</div>
            </div>
          )}
        </div>
      )}
      <p className="text-[10px] text-neutral-600 mt-2 leading-tight">
        Median path in {medColor === GREEN ? 'green (positive drift)' : medColor === RED ? 'red (negative drift)' : 'white (flat drift)'};
        shaded bands = 50% and 80% probability ranges. Drift damped to ±40%/yr. Projection, not a guarantee.
      </p>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 2) RETURN DISTRIBUTION + 3) VOLATILITY CONE
// ══════════════════════════════════════════════════════════════════════════════
function DistributionPanel({ closes }) {
  const { bars, stats } = useMemo(() => histogram(dailyReturns(closes).map(x => x * 100), 25), [closes])
  if (!stats || !bars.length) return null
  const sd = stats.std
  return (
    <Card
      title="Daily return distribution"
      subtitle={`${stats.n} sessions · mean ${fmtPct(stats.mean, 2)} · σ ${fmtNum(sd, 2)}%`}
    >
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={bars} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="x" stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }}
            tickFormatter={(v) => Number(v).toFixed(0) + '%'} interval={4} />
          <YAxis stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} width={30} />
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #525252', borderRadius: 8, fontSize: 11 }}
            labelStyle={{ color: '#e5e5e5' }} itemStyle={{ color: '#e5e5e5' }}
            formatter={(v) => [v, 'sessions']} labelFormatter={(l) => `${Number(l).toFixed(2)}% day`} />
          {sd != null && <ReferenceLine x={sd} stroke={NEUTRAL} strokeDasharray="3 3" />}
          {sd != null && <ReferenceLine x={-sd} stroke={NEUTRAL} strokeDasharray="3 3" />}
          <ReferenceLine x={0} stroke={WHITE} strokeOpacity={0.4} />
          <Bar dataKey="count" isAnimationActive={false}>
            {bars.map((b, i) => <Cell key={i} fill={b.bull ? GREEN : RED} fillOpacity={0.65} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <p className="text-[10px] text-neutral-600 mt-1">Dashed lines mark ±1σ ({fmtNum(sd, 2)}%). Fat tails beyond them = event risk.</p>
    </Card>
  )
}

function VolConePanel({ closes }) {
  const cone = useMemo(() => volCone(closes), [closes])
  if (!cone.length) return null
  return (
    <Card title="Volatility cone" subtitle="Realized annualized σ per window vs its own historical range">
      <div className="space-y-2 mt-1">
        {cone.map((w) => {
          const pct = Math.max(0, Math.min(100, w.percentile))
          const hot = pct >= 66
          return (
            <div key={w.window} className="flex items-center gap-3">
              <span className="text-[10px] text-neutral-500 w-10 font-mono">{w.window}d</span>
              <div className="flex-1 h-2 bg-neutral-800 rounded-full relative overflow-visible">
                <div className="absolute inset-y-0 left-0 right-0 rounded-full bg-neutral-800" />
                <div
                  className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-2.5 h-2.5 rounded-full ${hot ? 'bg-rose-400' : 'bg-emerald-400'}`}
                  style={{ left: `${pct}%` }}
                  title={`${fmtNum(w.current, 0)}% (pctile ${fmtNum(pct, 0)})`}
                />
              </div>
              <span className="text-[10px] font-mono text-white w-12 text-right">{fmtNum(w.current, 0)}%</span>
            </div>
          )
        })}
      </div>
      <div className="flex justify-between text-[9px] text-neutral-600 mt-2 px-10">
        <span>calm</span><span>historical range</span><span>stressed</span>
      </div>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 4) RELATIVE STRENGTH vs SPY (+ sector)
// ══════════════════════════════════════════════════════════════════════════════
function RelativeStrengthPanel({ rel, sectorRel }) {
  if (!rel || !rel.series || rel.series.length < 10) return null
  const st = rel.stats
  const merged = rel.series.map((p, i) => ({
    date: p.date, stock: p.stock, bench: p.bench,
    sector: sectorRel?.series?.[i]?.stock != null ? sectorRel.series[i].bench : undefined,
  }))
  return (
    <Card
      title="Relative strength"
      subtitle={`Normalized to 100 · vs ${st.benchName}${sectorRel?.stats ? ' + sector' : ''}`}
      right={
        <div className="text-right">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">vs {st.benchName}</div>
          <div className={`text-sm font-mono font-black ${st.relPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{fmtPct(st.relPct)}</div>
        </div>
      }
    >
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={merged} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis dataKey="date" stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }}
            tickFormatter={(d) => String(d).slice(5)} interval={Math.max(1, Math.floor(merged.length / 6))} />
          <YAxis stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} domain={['auto', 'auto']} width={36} />
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #525252', borderRadius: 8, fontSize: 11 }}
            labelStyle={{ color: '#e5e5e5' }} itemStyle={{ color: '#e5e5e5' }} />
          <ReferenceLine y={100} stroke={NEUTRAL_DIM} strokeDasharray="2 2" />
          <Line type="monotone" dataKey="stock" name="This stock" stroke={WHITE} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="bench" name={st.benchName} stroke={NEUTRAL} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          {sectorRel?.stats && <Line type="monotone" dataKey="sector" name="Sector" stroke={NEUTRAL_DIM} strokeWidth={1} strokeDasharray="4 2" dot={false} isAnimationActive={false} />}
        </LineChart>
      </ResponsiveContainer>
      <div className="flex gap-4 text-[10px] text-neutral-500 mt-1">
        <span>Stock {fmtPct(st.stockRetPct)}</span>
        <span>{st.benchName} {fmtPct(st.benchRetPct)}</span>
        {sectorRel?.stats && <span>Sector {fmtPct(sectorRel.stats.benchRetPct)}</span>}
      </div>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 5) DRAWDOWN / UNDERWATER
// ══════════════════════════════════════════════════════════════════════════════
function DrawdownPanel({ chart, info }) {
  const dd = useMemo(() => drawdownSeries(chart), [chart])
  if (!dd.stats || dd.series.length < 2) return null
  const fromHigh = (info && Number.isFinite(Number(info.fifty_two_week_high)) && Number(info.fifty_two_week_high) > 0 &&
    Number.isFinite(Number(info.current_price)) && Number(info.current_price) > 0)
    ? (Number(info.current_price) / Number(info.fifty_two_week_high) - 1) * 100 : null
  return (
    <Card
      title="Drawdown (underwater)"
      subtitle="Decline from each running peak — capital-preservation view"
      right={
        <div className="text-right">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Max drawdown</div>
          <div className="text-sm font-mono font-black text-rose-400">{fmtPct(dd.stats.maxDDPct)}</div>
        </div>
      }
    >
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={dd.series} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="ddFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={RED} stopOpacity={0.05} />
              <stop offset="100%" stopColor={RED} stopOpacity={0.4} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
          <XAxis dataKey="date" stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }}
            tickFormatter={(d) => String(d).slice(2, 7)} interval={Math.max(1, Math.floor(dd.series.length / 6))} />
          <YAxis stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} width={38} tickFormatter={(v) => v + '%'} />
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #525252', borderRadius: 8, fontSize: 11 }}
            labelStyle={{ color: '#e5e5e5' }} itemStyle={{ color: '#e5e5e5' }}
            formatter={(v) => [fmtPct(v), 'drawdown']} />
          <Area type="monotone" dataKey="dd" stroke={RED} strokeWidth={1.5} fill="url(#ddFill)" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-2 gap-2 mt-2 text-center">
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Current drawdown</div>
          <div className={`text-sm font-mono font-bold ${dd.stats.currentDDPct < -0.05 ? 'text-rose-300' : 'text-emerald-300'}`}>{fmtPct(dd.stats.currentDDPct)}</div>
        </div>
        <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Off 52-wk high</div>
          <div className={`text-sm font-mono font-bold ${fromHigh == null ? 'text-white' : fromHigh < -0.05 ? 'text-rose-300' : 'text-emerald-300'}`}>{fromHigh == null ? '—' : fmtPct(fromHigh)}</div>
        </div>
      </div>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 6) FACTOR EXPOSURE RADAR
// ══════════════════════════════════════════════════════════════════════════════
function FactorRadarPanel({ chart, relPct }) {
  const factors = useMemo(() => factorScores(chart, { relPct }), [chart, relPct])
  if (!factors || factors.length < 3) return null
  const avg = factors.reduce((s, f) => s + f.score, 0) / factors.length
  const col = avg >= 55 ? GREEN : avg <= 45 ? RED : WHITE
  return (
    <Card title="Factor profile" subtitle="Price-derived factor scores (0–100, higher = more favorable)">
      <ResponsiveContainer width="100%" height={240}>
        <RadarChart data={factors} margin={{ top: 8, right: 24, bottom: 8, left: 24 }}>
          <PolarGrid stroke={GRID} />
          <PolarAngleAxis dataKey="factor" tick={{ fill: NEUTRAL, fontSize: 10 }} />
          <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
          <Radar dataKey="score" stroke={col} fill={col} fillOpacity={0.22} isAnimationActive={false} />
          <Tooltip
            contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #525252', borderRadius: 8, fontSize: 11 }}
            labelStyle={{ color: '#e5e5e5' }} itemStyle={{ color: '#e5e5e5' }}
            formatter={(v) => [fmtNum(v, 0), 'score']} />
        </RadarChart>
      </ResponsiveContainer>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 7) SEASONALITY HEATMAP
// ══════════════════════════════════════════════════════════════════════════════
function SeasonalityPanel({ chart }) {
  const seas = useMemo(() => monthlySeasonality(chart), [chart])
  if (!seas.hasData) return null
  const maxAbs = Math.max(0.01, ...seas.cells.map(c => Math.abs(c.avg || 0)))
  return (
    <Card title="Seasonality" subtitle="Average return by calendar month (all years in window)">
      <div className="grid grid-cols-6 gap-1.5 mt-1">
        {seas.cells.map((c) => {
          const v = c.avg
          let bg = 'rgba(163,163,163,0.08)'
          if (v != null && c.count > 0) {
            const intensity = Math.min(0.85, 0.15 + Math.abs(v) / maxAbs * 0.7)
            bg = v >= 0 ? `rgba(34,197,94,${intensity})` : `rgba(239,68,68,${intensity})`
          }
          return (
            <div key={c.month} className="rounded-md px-1.5 py-2 text-center border border-neutral-800" style={{ backgroundColor: bg }}
              title={c.count > 0 ? `${c.label}: avg ${fmtPct(v)} over ${c.count} yr(s)` : `${c.label}: no data`}>
              <div className="text-[9px] font-bold text-white/90">{c.label}</div>
              <div className="text-[10px] font-mono text-white">{c.count > 0 ? fmtPct(v, 1) : '—'}</div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 8) REGIME RIBBON
// ══════════════════════════════════════════════════════════════════════════════
function RegimeRibbonPanel({ chart }) {
  const reg = useMemo(() => classifyRegime(chart), [chart])
  const runs = useMemo(() => {
    const pts = (reg.series || []).filter(p => p.regime)
    if (!pts.length) return []
    const total = pts.length
    const out = []
    let cur = pts[0].regime, count = 0
    for (const p of pts) {
      if (p.regime === cur) count++
      else { out.push({ regime: cur, w: count / total }); cur = p.regime; count = 1 }
    }
    out.push({ regime: cur, w: count / total })
    return out
  }, [reg])
  if (!runs.length || !reg.current) return null
  const colorOf = (r) => r === 'bull' ? GREEN : r === 'bear' ? RED : NEUTRAL_DIM
  const badge = reg.current === 'bull'
    ? { t: 'BULL', c: 'text-emerald-300 border-emerald-800/50 bg-emerald-950/30' }
    : reg.current === 'bear'
    ? { t: 'BEAR', c: 'text-rose-300 border-rose-800/50 bg-rose-950/30' }
    : { t: 'SIDEWAYS', c: 'text-neutral-300 border-neutral-700 bg-neutral-900/50' }
  return (
    <Card
      title="Trend regime timeline"
      subtitle="Bull = price>50>200-day · Bear = price<50<200-day · else Sideways"
      right={<span className={`px-2 py-1 rounded text-[11px] font-bold border ${badge.c}`}>{badge.t}</span>}
    >
      <div className="flex h-6 w-full rounded-md overflow-hidden border border-neutral-800 mt-1">
        {runs.map((r, i) => (
          <div key={i} style={{ width: `${Math.max(0.5, r.w * 100)}%`, backgroundColor: colorOf(r.regime), opacity: 0.7 }} title={r.regime} />
        ))}
      </div>
      <div className="flex items-center gap-4 mt-2 text-[10px] text-neutral-500">
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: GREEN }} /> Bull</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: NEUTRAL_DIM }} /> Sideways</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: RED }} /> Bear</span>
        <span className="ml-auto text-neutral-600">left = oldest → right = now</span>
      </div>
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// 9) EARNINGS DRIFT
// ══════════════════════════════════════════════════════════════════════════════
function EarningsPanel({ chart, earnings }) {
  const reactions = useMemo(
    () => earningsReactions(chart, (earnings && earnings.history) || []),
    [chart, earnings])
  const hasNext = earnings && Number.isFinite(Number(earnings.days_until))
  if (!hasNext && reactions.length === 0) return null
  const avgAbs = reactions.length ? mean(reactions.map(r => Math.abs(r.reactionPct))) : null
  const posShare = reactions.length ? (reactions.filter(r => r.reactionPct >= 0).length / reactions.length) * 100 : null
  const bars = reactions.map(r => ({ ...r, label: String(r.date).slice(2, 7) }))
  return (
    <Card
      title="Earnings reaction"
      subtitle="Next report + historical next-day price moves"
      right={hasNext ? (
        <div className="text-right">
          <div className="text-[9px] uppercase tracking-wider text-neutral-500">Next earnings</div>
          <div className="text-sm font-mono font-bold text-white">{earnings.next_date || '—'}</div>
          <div className="text-[10px] text-neutral-500">in {Math.round(earnings.days_until)} days</div>
        </div>
      ) : null}
    >
      {bars.length > 0 ? (
        <>
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={bars} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="label" stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} />
              <YAxis stroke={NEUTRAL_DIM} tick={{ fontSize: 10 }} width={34} tickFormatter={(v) => v + '%'} />
              <Tooltip
                contentStyle={{ backgroundColor: '#0a0a0a', border: '1px solid #525252', borderRadius: 8, fontSize: 11 }}
                labelStyle={{ color: '#e5e5e5' }} itemStyle={{ color: '#e5e5e5' }}
                formatter={(v) => [fmtPct(v), 'next-day move']} />
              <ReferenceLine y={0} stroke={WHITE} strokeOpacity={0.4} />
              <Bar dataKey="reactionPct" isAnimationActive={false}>
                {bars.map((b, i) => <Cell key={i} fill={b.reactionPct >= 0 ? GREEN : RED} fillOpacity={0.7} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="grid grid-cols-2 gap-2 mt-2 text-center">
            <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-neutral-500">Avg abs move</div>
              <div className="text-sm font-mono font-bold text-white">{avgAbs == null ? '—' : fmtNum(avgAbs, 1) + '%'}</div>
            </div>
            <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-neutral-500">Positive reactions</div>
              <div className={`text-sm font-mono font-bold ${posShare != null && posShare >= 50 ? 'text-emerald-400' : 'text-rose-400'}`}>{posShare == null ? '—' : fmtNum(posShare, 0) + '%'}</div>
            </div>
          </div>
        </>
      ) : (
        <p className="text-[11px] text-neutral-500">Reaction history unavailable; showing next report date only.</p>
      )}
    </Card>
  )
}

// ══════════════════════════════════════════════════════════════════════════════
// SECTION CONTAINER — fetches supporting data (fail-isolated) and lays out panels
// ══════════════════════════════════════════════════════════════════════════════
export default function QuantAnalytics({ analysis, ticker, pick }) {
  const [longChart, setLongChart] = useState(null)   // 5y stock series for stats
  const [rel, setRel] = useState(null)               // relative strength vs SPY
  const [sectorRel, setSectorRel] = useState(null)   // vs sector ETF
  const [earnings, setEarnings] = useState(null)     // extras earnings
  const [ready, setReady] = useState(false)

  const baseChart = analysis?.chart_data || []
  const info = analysis?.info || null
  const sector = info?.sector || null

  useEffect(() => {
    let cancelled = false
    setLongChart(null); setRel(null); setSectorRel(null); setEarnings(null); setReady(false)
    if (!ticker || !Array.isArray(baseChart) || baseChart.length < 30) { setReady(true); return }

    async function load() {
      // 1) Longer history for stats panels (5y). Fail -> fall back to base 1y.
      const longData = await safeJson(`/api/analyze/${encodeURIComponent(ticker)}?period=5y`)
      if (!cancelled && longData && Array.isArray(longData.chart_data) && longData.chart_data.length > baseChart.length) {
        setLongChart(longData.chart_data)
      }
      await sleep(250)

      // 2) SPY benchmark for relative strength (aligned on the base 1y window).
      const spy = await safeJson(`/api/analyze/SPY?period=1y`)
      if (!cancelled && spy && Array.isArray(spy.chart_data)) {
        try {
          const r = relativeSeries(baseChart, spy.chart_data, 'SPY')
          if (r.series.length) setRel(r)
        } catch { /* hide */ }
      }
      await sleep(250)

      // 3) Sector ETF (optional).
      const etf = sectorEtf(sector)
      if (etf) {
        const se = await safeJson(`/api/analyze/${etf}?period=1y`)
        if (!cancelled && se && Array.isArray(se.chart_data)) {
          try {
            const sr = relativeSeries(baseChart, se.chart_data, etf)
            if (sr.series.length) setSectorRel(sr)
          } catch { /* hide */ }
        }
        await sleep(250)
      }

      // 4) Earnings extras (fully optional).
      const ex = await safeJson(`/api/analyze-extras/${encodeURIComponent(ticker)}`)
      if (!cancelled && ex && ex.earnings) setEarnings(ex.earnings)

      if (!cancelled) setReady(true)
    }
    load()
    return () => { cancelled = true }
  }, [ticker]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!Array.isArray(baseChart) || baseChart.length < 30) return null

  const statChart = (Array.isArray(longChart) && longChart.length > baseChart.length) ? longChart : baseChart
  const statCloses = closesFrom(statChart)
  const relPct = rel?.stats?.relPct
  const targets = pick ? { target: Number(pick.target_price), stop: Number(pick.stop_loss) } : null

  return (
    <div className="mt-8">
      <div className="flex items-center gap-3 mb-4">
        <div className="h-px flex-1 bg-neutral-800" />
        <h2 className="text-xs font-black uppercase tracking-[0.25em] text-neutral-400">Quant Analytics</h2>
        <div className="h-px flex-1 bg-neutral-800" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PanelBoundary><MonteCarloPanel closes={statCloses} targets={targets} /></PanelBoundary>
        <PanelBoundary><RelativeStrengthPanel rel={rel} sectorRel={sectorRel} /></PanelBoundary>
        <PanelBoundary><DistributionPanel closes={statCloses} /></PanelBoundary>
        <PanelBoundary><DrawdownPanel chart={statChart} info={info} /></PanelBoundary>
        <PanelBoundary><FactorRadarPanel chart={statChart} relPct={relPct} /></PanelBoundary>
        <PanelBoundary><VolConePanel closes={statCloses} /></PanelBoundary>
        <PanelBoundary><SeasonalityPanel chart={statChart} /></PanelBoundary>
        <PanelBoundary><RegimeRibbonPanel chart={statChart} /></PanelBoundary>
        <PanelBoundary><EarningsPanel chart={statChart} earnings={earnings} /></PanelBoundary>
      </div>
      {!ready && (
        <p className="text-[11px] text-neutral-600 mt-3 text-center">Loading extended analytics (benchmarks, history)…</p>
      )}
    </div>
  )
}
