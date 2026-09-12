// quantAnalytics.js — PURE, dependency-free math for the Analyze page's
// "Quant Analytics" section. Every function is defensive: bad/empty/short
// input returns a safe empty result and NEVER throws. All outputs are finite
// (NaN/Infinity are scrubbed to null) so the charts can never render garbage.
//
// This module has ZERO imports so it runs identically in the browser (Vite)
// and under plain `node` for unit tests. Keep it that way.

// ── low-level guards ─────────────────────────────────────────────────────────
export function isFiniteNum(x) {
  return typeof x === 'number' && Number.isFinite(x)
}

// Coerce to a finite number or return null (never NaN/Infinity leaks out).
export function fin(x) {
  return isFiniteNum(x) ? x : null
}

// Always return an array — any non-array (object, number, null) becomes [].
export function asArray(x) {
  return Array.isArray(x) ? x : []
}

// Pull a clean, strictly-positive close series out of chart_data rows.
export function closesFrom(chartData) {
  if (!Array.isArray(chartData)) return []
  const out = []
  for (const row of chartData) {
    const c = row && Number(row.close)
    if (Number.isFinite(c) && c > 0) out.push(c)
  }
  return out
}

export function datesFrom(chartData) {
  if (!Array.isArray(chartData)) return []
  return chartData.map(r => (r && r.date) ? String(r.date) : null)
}

// ── stats primitives ─────────────────────────────────────────────────────────
export function mean(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return null
  let s = 0, n = 0
  for (const v of arr) { if (isFiniteNum(v)) { s += v; n++ } }
  return n ? s / n : null
}

// Sample standard deviation (n-1). Needs >= 2 finite points.
export function stdev(arr) {
  if (!Array.isArray(arr)) return null
  const clean = arr.filter(isFiniteNum)
  if (clean.length < 2) return null
  const m = mean(clean)
  let acc = 0
  for (const v of clean) acc += (v - m) * (v - m)
  const variance = acc / (clean.length - 1)
  return variance >= 0 ? Math.sqrt(variance) : null
}

// Simple daily returns: close[i]/close[i-1] - 1
export function dailyReturns(closes) {
  const out = []
  if (!Array.isArray(closes)) return out
  for (let i = 1; i < closes.length; i++) {
    const a = closes[i - 1], b = closes[i]
    if (isFiniteNum(a) && isFiniteNum(b) && a > 0) out.push(b / a - 1)
  }
  return out
}

// Log returns: ln(close[i]/close[i-1]) — used for the GBM projection.
export function logReturns(closes) {
  const out = []
  if (!Array.isArray(closes)) return out
  for (let i = 1; i < closes.length; i++) {
    const a = closes[i - 1], b = closes[i]
    if (isFiniteNum(a) && isFiniteNum(b) && a > 0 && b > 0) out.push(Math.log(b / a))
  }
  return out
}

// Standard normal CDF via Abramowitz & Stegun 7.1.26 (max abs error ~7.5e-8).
export function normalCdf(x) {
  if (!isFiniteNum(x)) return null
  const t = 1 / (1 + 0.2316419 * Math.abs(x))
  const d = 0.3989422804014327 * Math.exp(-x * x / 2)
  let p = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
          t * (-1.821255978 + t * 1.330274429))))
  p = 1 - p
  return x >= 0 ? p : 1 - p
}

const ANNUAL = 252

// ── 1) Monte Carlo / analytic GBM projection ─────────────────────────────────
// Deterministic (no RNG) lognormal cone: at horizon t (trading days),
//   ln(P_t / P0) ~ Normal(m*t, s^2 * t)   with m,s = mean/std of daily log rets.
// We return percentile bands (p10..p90) + median + end-of-horizon stats.
// Drift is damped to +/-40% annualized so a noisy short sample can't produce
// an absurd cone (capital-preservation: never over-promise upside).
export function gbmProjection(closes, opts = {}) {
  const horizon = Math.max(5, Math.min(250, Math.floor(opts.horizon || 60)))
  const empty = { points: [], stats: null }
  const cleanCloses = asArray(closes).filter(c => isFiniteNum(c) && c > 0)
  if (cleanCloses.length < 30) return empty
  const lr = logReturns(cleanCloses)
  if (lr.length < 20) return empty
  let m = mean(lr)
  const s = stdev(lr)
  if (!isFiniteNum(m) || !isFiniteNum(s) || s <= 0) return empty
  // damp annualized drift into [-0.40, +0.40]
  const muAnnRaw = m * ANNUAL
  const muAnn = Math.max(-0.4, Math.min(0.4, muAnnRaw))
  m = muAnn / ANNUAL
  const P0 = cleanCloses[cleanCloses.length - 1]
  const Z = { p10: -1.2815515655, p25: -0.6744897502, p75: 0.6744897502, p90: 1.2815515655 }
  const points = []
  for (let t = 0; t <= horizon; t++) {
    const drift = m * t
    const vol = s * Math.sqrt(t)
    const median = P0 * Math.exp(drift)
    const at = z => P0 * Math.exp(drift + vol * z)
    points.push({
      day: t,
      band90: [fin(at(Z.p10)), fin(at(Z.p90))],
      band50: [fin(at(Z.p25)), fin(at(Z.p75))],
      median: fin(median),
    })
  }
  const T = horizon
  const volT = s * Math.sqrt(T)
  const medianEnd = P0 * Math.exp(m * T)
  const p90End = P0 * Math.exp(m * T + volT * Z.p90)
  const p10End = P0 * Math.exp(m * T + volT * Z.p10)
  // Expected +/-1σ move (%) at the horizon around spot.
  const expMoveUpPct = (Math.exp(volT) - 1) * 100
  const expMoveDnPct = (1 - Math.exp(-volT)) * 100
  // P(price above spot at horizon) = Φ( m*T / (s*sqrt(T)) )
  const probUp = normalCdf((m * T) / (s * Math.sqrt(T)))
  return {
    points,
    stats: {
      spot: fin(P0),
      horizonDays: T,
      muAnnPct: fin(muAnn * 100),
      sigAnnPct: fin(s * Math.sqrt(ANNUAL) * 100),
      medianEnd: fin(medianEnd),
      p10End: fin(p10End),
      p90End: fin(p90End),
      expMoveUpPct: fin(expMoveUpPct),
      expMoveDnPct: fin(expMoveDnPct),
      probUpPct: isFiniteNum(probUp) ? fin(probUp * 100) : null,
    },
  }
}

// Probability of touching a target/stop by the horizon, using the same
// lognormal terminal distribution (approximation: terminal, not first-passage).
export function probReach(closes, targetPrice, horizon = 60) {
  const cleanCloses = asArray(closes).filter(c => isFiniteNum(c) && c > 0)
  if (cleanCloses.length < 30 || !isFiniteNum(targetPrice) || targetPrice <= 0) return null
  const lr = logReturns(cleanCloses)
  const s = stdev(lr)
  let m = mean(lr)
  if (!isFiniteNum(s) || s <= 0 || !isFiniteNum(m)) return null
  m = Math.max(-0.4, Math.min(0.4, m * ANNUAL)) / ANNUAL
  const P0 = cleanCloses[cleanCloses.length - 1]
  const T = Math.max(1, horizon)
  const z = (Math.log(targetPrice / P0) - m * T) / (s * Math.sqrt(T))
  const cdf = normalCdf(z)
  if (cdf == null) return null
  // target above spot -> P(exceed); below -> P(fall below)
  return fin((targetPrice >= P0 ? (1 - cdf) : cdf) * 100)
}

// ── 2) Return distribution histogram ─────────────────────────────────────────
export function histogram(returnsPct, bins = 21) {
  const empty = { bars: [], stats: null }
  const vals = asArray(returnsPct).filter(isFiniteNum)
  if (vals.length < 10) return empty
  const nb = Math.max(5, Math.min(51, Math.floor(bins)))
  let lo = Math.min(...vals), hi = Math.max(...vals)
  if (!(hi > lo)) return empty
  // pad edges slightly so extremes land inside a bin
  const pad = (hi - lo) * 0.02
  lo -= pad; hi += pad
  const width = (hi - lo) / nb
  const counts = new Array(nb).fill(0)
  for (const v of vals) {
    let idx = Math.floor((v - lo) / width)
    if (idx < 0) idx = 0
    if (idx >= nb) idx = nb - 1
    counts[idx]++
  }
  const m = mean(vals), sd = stdev(vals)
  const bars = counts.map((c, i) => {
    const center = lo + width * (i + 0.5)
    return { x: fin(center), count: c, bull: center >= 0 }
  })
  return { bars, stats: { mean: fin(m), std: fin(sd), n: vals.length,
    minPct: fin(Math.min(...vals)), maxPct: fin(Math.max(...vals)) } }
}

// ── volatility cone: realized annualized vol over several windows ─────────────
export function volCone(closes) {
  const cleanCloses = asArray(closes).filter(c => isFiniteNum(c) && c > 0)
  const windows = [10, 20, 30, 60, 90, 120]
  const out = []
  if (cleanCloses.length < 15) return out
  const lr = logReturns(cleanCloses)
  for (const w of windows) {
    if (lr.length < w + 5) continue
    // current window vol
    const cur = stdev(lr.slice(-w))
    // rolling min/max of window vol across the full history
    let mn = Infinity, mx = -Infinity, samples = 0
    for (let end = w; end <= lr.length; end++) {
      const v = stdev(lr.slice(end - w, end))
      if (isFiniteNum(v)) { mn = Math.min(mn, v); mx = Math.max(mx, v); samples++ }
    }
    if (!isFiniteNum(cur) || samples < 3) continue
    const ann = x => x * Math.sqrt(ANNUAL) * 100
    const range = mx - mn
    const pct = range > 0 ? ((cur - mn) / range) * 100 : 50
    out.push({
      window: w,
      current: fin(ann(cur)),
      min: fin(ann(mn)),
      max: fin(ann(mx)),
      percentile: fin(Math.max(0, Math.min(100, pct))),
    })
  }
  return out
}

// ── 5) Drawdown / underwater curve ───────────────────────────────────────────
export function drawdownSeries(chartData) {
  const empty = { series: [], stats: null }
  if (!Array.isArray(chartData) || chartData.length < 2) return empty
  let peak = -Infinity
  const series = []
  let maxDD = 0, maxDDDate = null
  for (const row of chartData) {
    const c = row && Number(row.close)
    if (!Number.isFinite(c) || c <= 0) continue
    if (c > peak) peak = c
    const dd = peak > 0 ? (c / peak - 1) * 100 : 0
    if (dd < maxDD) { maxDD = dd; maxDDDate = row.date || null }
    series.push({ date: row.date || null, dd: fin(dd) })
  }
  if (series.length < 2) return empty
  const currentDD = series[series.length - 1].dd
  return { series, stats: { maxDDPct: fin(maxDD), maxDDDate, currentDDPct: fin(currentDD) } }
}

// ── 7) Monthly seasonality ───────────────────────────────────────────────────
// Average calendar-month return across all years present. Needs dates in
// YYYY-MM-DD form. Returns 12 cells (Jan..Dec) with avg %, sample count.
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
export function monthlySeasonality(chartData) {
  const out = MONTHS.map((label, i) => ({ month: i + 1, label, avg: null, count: 0, sum: 0 }))
  if (!Array.isArray(chartData) || chartData.length < 40) return { cells: out, hasData: false }
  // Build (yearMonth -> {first, last}) close to compute monthly returns.
  const byMonth = new Map()
  for (const row of chartData) {
    if (!row || !row.date) continue
    const c = Number(row.close)
    if (!Number.isFinite(c) || c <= 0) continue
    const ym = String(row.date).slice(0, 7) // YYYY-MM
    if (!/^\d{4}-\d{2}$/.test(ym)) continue
    const cur = byMonth.get(ym)
    if (!cur) byMonth.set(ym, { first: c, last: c })
    else cur.last = c
  }
  let filled = 0
  for (const [ym, v] of byMonth) {
    if (!(v.first > 0)) continue
    const ret = (v.last / v.first - 1) * 100
    const mi = parseInt(ym.slice(5, 7), 10) - 1
    if (mi < 0 || mi > 11 || !Number.isFinite(ret)) continue
    out[mi].sum += ret
    out[mi].count += 1
    filled++
  }
  for (const cell of out) {
    cell.avg = cell.count > 0 ? fin(cell.sum / cell.count) : null
  }
  return { cells: out, hasData: filled >= 6 }
}

// ── 9) Regime classification (bull / bear / sideways) ────────────────────────
// Uses close vs SMA50 vs SMA200 already present in chart_data; falls back to
// computing SMA50/200 from closes if the fields are missing.
export function classifyRegime(chartData) {
  const empty = { series: [], current: null }
  if (!Array.isArray(chartData) || chartData.length < 60) return empty
  const closes = chartData.map(r => Number(r && r.close))
  const sma = (i, n) => {
    if (i + 1 < n) return null
    let s = 0
    for (let k = i - n + 1; k <= i; k++) {
      const c = closes[k]
      if (!Number.isFinite(c) || c <= 0) return null
      s += c
    }
    return s / n
  }
  const series = []
  for (let i = 0; i < chartData.length; i++) {
    const row = chartData[i]
    const c = closes[i]
    if (!Number.isFinite(c) || c <= 0) { series.push({ date: row && row.date, close: null, regime: null }); continue }
    const s50 = isFiniteNum(row && row.sma_50) ? row.sma_50 : sma(i, 50)
    const s200 = isFiniteNum(row && row.sma_200) ? row.sma_200 : sma(i, 200)
    let regime = 'sideways'
    if (isFiniteNum(s50) && isFiniteNum(s200)) {
      if (c > s50 && s50 > s200) regime = 'bull'
      else if (c < s50 && s50 < s200) regime = 'bear'
      else regime = 'sideways'
    } else if (isFiniteNum(s50)) {
      regime = c > s50 ? 'bull' : 'bear'
    }
    series.push({ date: row && row.date, close: fin(c), regime })
  }
  const withRegime = series.filter(p => p.regime)
  const current = withRegime.length ? withRegime[withRegime.length - 1].regime : null
  return { series, current }
}

// ── 3) Relative strength: normalize two series to 100 at a common start ───────
export function relativeSeries(stockChart, benchChart, benchName = 'Benchmark') {
  const empty = { series: [], stats: null }
  if (!Array.isArray(stockChart) || !Array.isArray(benchChart)) return empty
  // Index benchmark closes by date for alignment.
  const benchMap = new Map()
  for (const r of benchChart) {
    if (r && r.date && Number.isFinite(Number(r.close)) && Number(r.close) > 0) {
      benchMap.set(String(r.date), Number(r.close))
    }
  }
  const aligned = []
  for (const r of stockChart) {
    if (!r || !r.date) continue
    const sc = Number(r.close)
    const bc = benchMap.get(String(r.date))
    if (Number.isFinite(sc) && sc > 0 && Number.isFinite(bc) && bc > 0) {
      aligned.push({ date: r.date, stock: sc, bench: bc })
    }
  }
  if (aligned.length < 10) return empty
  const s0 = aligned[0].stock, b0 = aligned[0].bench
  const series = aligned.map(a => ({
    date: a.date,
    stock: fin((a.stock / s0) * 100),
    bench: fin((a.bench / b0) * 100),
  }))
  const last = series[series.length - 1]
  const relPct = fin((last.stock - last.bench))
  return { series, stats: { benchName, stockRetPct: fin(last.stock - 100),
    benchRetPct: fin(last.bench - 100), relPct } }
}

// ── 4) Earnings reactions: map earnings dates onto the price series ───────────
// earningsHistory: [{date:'YYYY-MM-DD', surprise_pct?:number}]
// Returns the next-day (or next-available) % reaction for each event.
export function earningsReactions(chartData, earningsHistory) {
  const out = []
  if (!Array.isArray(chartData) || !Array.isArray(earningsHistory)) return out
  const rows = chartData
    .filter(r => r && r.date && Number.isFinite(Number(r.close)) && Number(r.close) > 0)
    .map(r => ({ date: String(r.date), close: Number(r.close) }))
  if (rows.length < 3) return out
  for (const ev of earningsHistory) {
    if (!ev || !ev.date) continue
    const d = String(ev.date)
    // find the first bar on/after the earnings date
    let idx = rows.findIndex(r => r.date >= d)
    if (idx <= 0 || idx >= rows.length) continue
    const before = rows[idx - 1].close
    const after = rows[idx].close
    if (!(before > 0)) continue
    const reactionPct = (after / before - 1) * 100
    out.push({
      date: d,
      reactionPct: fin(reactionPct),
      surprisePct: isFiniteNum(ev.surprise_pct) ? fin(ev.surprise_pct) : null,
    })
  }
  return out
}

// ── 6) Factor radar scores (0..100) ──────────────────────────────────────────
// Self-contained scores derived from the price series, optionally blended with
// the engine's quant factors when available. Higher = more favorable.
function clamp01to100(x) { return Math.max(0, Math.min(100, x)) }

export function factorScores(chartData, opts = {}) {
  const closes = closesFrom(chartData)
  if (closes.length < 60) return []
  const P0 = closes[closes.length - 1]
  const priceAgo = n => (closes.length > n ? closes[closes.length - 1 - n] : null)
  const ret = n => {
    const p = priceAgo(n)
    return (p && p > 0) ? (P0 / p - 1) * 100 : null
  }
  const lr = logReturns(closes)
  const sigAnn = stdev(lr) ? stdev(lr) * Math.sqrt(ANNUAL) * 100 : null

  // Momentum: 3m return, mapped -20%..+20% -> 0..100
  const mom3m = ret(63)
  const momentum = isFiniteNum(mom3m) ? clamp01to100(50 + mom3m * 2.5) : 50
  // 6m momentum
  const mom6m = ret(126)
  const momentumLong = isFiniteNum(mom6m) ? clamp01to100(50 + mom6m * 1.5) : 50
  // Trend: close vs SMA50 & SMA200
  const sma = n => {
    if (closes.length < n) return null
    let s = 0; for (let k = closes.length - n; k < closes.length; k++) s += closes[k]
    return s / n
  }
  const s50 = sma(50), s200 = sma(200)
  let trend = 50
  if (isFiniteNum(s50) && isFiniteNum(s200) && s50 > 0 && s200 > 0) {
    const a = (P0 / s50 - 1) * 100, b = (P0 / s200 - 1) * 100
    trend = clamp01to100(50 + (a + b) * 2)
  } else if (isFiniteNum(s50) && s50 > 0) {
    trend = clamp01to100(50 + (P0 / s50 - 1) * 100 * 3)
  }
  // Low volatility: lower annualized vol -> higher score (25% vol == 50)
  const lowVol = isFiniteNum(sigAnn) ? clamp01to100(100 - sigAnn * 2) : 50
  // Stability: inverse of max drawdown magnitude
  const dd = drawdownSeries(chartData)
  const maxDD = dd.stats ? Math.abs(dd.stats.maxDDPct || 0) : null
  const stability = isFiniteNum(maxDD) ? clamp01to100(100 - maxDD * 1.5) : 50
  // Relative strength (optional, provided by caller as relPct vs SPY)
  const relPct = opts.relPct
  const relative = isFiniteNum(relPct) ? clamp01to100(50 + relPct * 1.5) : 50

  const factors = [
    { factor: 'Momentum 3M', score: fin(momentum) },
    { factor: 'Momentum 6M', score: fin(momentumLong) },
    { factor: 'Trend', score: fin(trend) },
    { factor: 'Low Vol', score: fin(lowVol) },
    { factor: 'Stability', score: fin(stability) },
    { factor: 'Rel. Strength', score: fin(relative) },
  ]
  return factors.filter(f => isFiniteNum(f.score))
}

// Map a sector name to a liquid sector ETF for relative-strength comparison.
const SECTOR_ETF = {
  'Technology': 'XLK',
  'Financial Services': 'XLF',
  'Financial': 'XLF',
  'Energy': 'XLE',
  'Healthcare': 'XLV',
  'Consumer Cyclical': 'XLY',
  'Consumer Defensive': 'XLP',
  'Industrials': 'XLI',
  'Basic Materials': 'XLB',
  'Real Estate': 'XLRE',
  'Utilities': 'XLU',
  'Communication Services': 'XLC',
}
export function sectorEtf(sector) {
  if (!sector || typeof sector !== 'string') return null
  return SECTOR_ETF[sector] || null
}
