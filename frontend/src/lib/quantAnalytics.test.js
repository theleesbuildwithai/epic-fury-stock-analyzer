// Unit tests for quantAnalytics.js — run with:  node --test src/lib/quantAnalytics.test.js
// Dependency-free (node:test + node:assert). Goal: prove every function is
// TOTALLY safe against empty/short/NaN/zero/negative input (never throws,
// never leaks NaN/Infinity) and correct on known inputs.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  isFiniteNum, fin, closesFrom, datesFrom, mean, stdev, dailyReturns, logReturns,
  normalCdf, gbmProjection, probReach, histogram, volCone, drawdownSeries,
  monthlySeasonality, classifyRegime, relativeSeries, earningsReactions,
  factorScores, sectorEtf,
} from './quantAnalytics.js'

// ── helpers to build synthetic data ──────────────────────────────────────────
function makeSeries(n, start = 100, dailyDrift = 0.0005, vol = 0.01, seed = 1) {
  // deterministic pseudo-random walk (no Math.random -> reproducible tests)
  let s = seed, price = start
  const out = []
  const base = new Date(Date.UTC(2020, 0, 1))
  for (let i = 0; i < n; i++) {
    s = (s * 1103515245 + 12345) & 0x7fffffff
    const u = (s / 0x7fffffff) * 2 - 1 // -1..1
    price = price * (1 + dailyDrift + vol * u)
    const d = new Date(base.getTime() + i * 86400000)
    out.push({ date: d.toISOString().slice(0, 10), close: Math.max(1, price), volume: 1e6 })
  }
  return out
}

const GOOD = makeSeries(600)
const GOOD_CLOSES = GOOD.map(r => r.close)

// Adversarial inputs every function must survive.
const ADVERSARIAL = [
  undefined, null, [], {}, 0, '', NaN, Infinity,
  [NaN, NaN, NaN], [0, 0, 0], [-1, -2, -3],
  [{ close: NaN }, { close: 0 }, { close: -5 }, { date: null, close: undefined }],
  [{ close: 100 }], // single point
  [{ date: 'bad', close: 'x' }],
]

// ── primitives ───────────────────────────────────────────────────────────────
test('isFiniteNum / fin scrub non-finite', () => {
  assert.equal(isFiniteNum(3), true)
  assert.equal(isFiniteNum(NaN), false)
  assert.equal(isFiniteNum(Infinity), false)
  assert.equal(isFiniteNum('3'), false)
  assert.equal(fin(NaN), null)
  assert.equal(fin(Infinity), null)
  assert.equal(fin(2.5), 2.5)
})

test('closesFrom / datesFrom filter garbage', () => {
  const rows = [{ close: 10, date: 'a' }, { close: NaN, date: 'b' }, { close: -1, date: 'c' }, { close: 0 }, {}]
  assert.deepEqual(closesFrom(rows), [10])
  assert.equal(closesFrom(null).length, 0)
  assert.equal(datesFrom(null).length, 0)
  assert.equal(datesFrom(rows).length, rows.length)
})

test('mean / stdev correctness + guards', () => {
  assert.equal(mean([2, 4, 6]), 4)
  assert.equal(mean([]), null)
  assert.equal(mean([NaN, 5]), 5)
  // stdev of [2,4,4,4,5,5,7,9] is 2 (classic example)
  assert.equal(Math.round(stdev([2, 4, 4, 4, 5, 5, 7, 9])), 2)
  assert.equal(stdev([5]), null)
  assert.equal(stdev([]), null)
})

test('dailyReturns / logReturns skip bad bars', () => {
  assert.deepEqual(dailyReturns([100, 110, 121]).map(x => +x.toFixed(4)), [0.1, 0.1])
  assert.equal(dailyReturns([100, 0, 50]).length, 1) // 0 anchor skipped
  assert.equal(logReturns([100, -5, 50]).length, 0) // negative skipped
  assert.ok(Math.abs(logReturns([100, Math.E * 100])[0] - 1) < 1e-9)
})

test('normalCdf matches known values', () => {
  assert.ok(Math.abs(normalCdf(0) - 0.5) < 1e-6)
  assert.ok(Math.abs(normalCdf(1.96) - 0.975) < 1e-3)
  assert.ok(Math.abs(normalCdf(-1.96) - 0.025) < 1e-3)
  assert.equal(normalCdf(NaN), null)
})

// ── gbmProjection ────────────────────────────────────────────────────────────
test('gbmProjection produces ordered, finite bands', () => {
  const r = gbmProjection(GOOD_CLOSES, { horizon: 60 })
  assert.ok(r.stats, 'stats present')
  assert.equal(r.points.length, 61)
  for (const p of r.points) {
    const [lo90, hi90] = p.band90
    const [lo50, hi50] = p.band50
    // all finite
    for (const v of [lo90, hi90, lo50, hi50, p.median]) assert.ok(Number.isFinite(v))
    // ordering: p10 <= p25 <= median <= p75 <= p90
    assert.ok(lo90 <= lo50 + 1e-6)
    assert.ok(lo50 <= p.median + 1e-6)
    assert.ok(p.median <= hi50 + 1e-6)
    assert.ok(hi50 <= hi90 + 1e-6)
  }
  // day 0 collapses to spot
  assert.ok(Math.abs(r.points[0].median - GOOD_CLOSES.at(-1)) < 1e-6)
  // drift is damped within +/-40% annualized
  assert.ok(r.stats.muAnnPct <= 40 + 1e-6 && r.stats.muAnnPct >= -40 - 1e-6)
  // probUp is a valid percentage
  assert.ok(r.stats.probUpPct >= 0 && r.stats.probUpPct <= 100)
})

test('probReach returns sane probabilities', () => {
  const spot = GOOD_CLOSES.at(-1)
  const pHi = probReach(GOOD_CLOSES, spot * 1.2, 60)
  const pLo = probReach(GOOD_CLOSES, spot * 0.8, 60)
  assert.ok(pHi >= 0 && pHi <= 100)
  assert.ok(pLo >= 0 && pLo <= 100)
  assert.equal(probReach(GOOD_CLOSES, -5, 60), null)
})

// ── histogram / volCone ──────────────────────────────────────────────────────
test('histogram buckets sum to n', () => {
  const rets = dailyReturns(GOOD_CLOSES).map(x => x * 100)
  const h = histogram(rets, 21)
  assert.ok(h.stats)
  const total = h.bars.reduce((s, b) => s + b.count, 0)
  assert.equal(total, h.stats.n)
  for (const b of h.bars) assert.ok(Number.isFinite(b.x))
})

test('volCone windows are finite and ordered min<=current<=max', () => {
  const cone = volCone(GOOD_CLOSES)
  assert.ok(cone.length >= 1)
  for (const w of cone) {
    for (const v of [w.current, w.min, w.max, w.percentile]) assert.ok(Number.isFinite(v))
    assert.ok(w.min <= w.current + 1e-6)
    assert.ok(w.current <= w.max + 1e-6)
    assert.ok(w.percentile >= 0 && w.percentile <= 100)
  }
})

// ── drawdown ─────────────────────────────────────────────────────────────────
test('drawdownSeries: dd <= 0 always, known trough', () => {
  const dd = drawdownSeries(GOOD)
  assert.ok(dd.stats)
  for (const p of dd.series) assert.ok(p.dd <= 1e-9)
  assert.ok(dd.stats.maxDDPct <= 0)
  // explicit case: 100 -> 120 -> 90 -> 108 : max dd = (90/120-1)= -25%
  const simple = [{ date: 'a', close: 100 }, { date: 'b', close: 120 },
    { date: 'c', close: 90 }, { date: 'd', close: 108 }]
  const s = drawdownSeries(simple)
  assert.ok(Math.abs(s.stats.maxDDPct - (-25)) < 1e-6)
  assert.ok(Math.abs(s.stats.currentDDPct - (-10)) < 1e-6) // 108/120-1
})

// ── seasonality ──────────────────────────────────────────────────────────────
test('monthlySeasonality returns 12 cells', () => {
  const s = monthlySeasonality(GOOD)
  assert.equal(s.cells.length, 12)
  assert.equal(s.hasData, true)
  for (const c of s.cells) {
    assert.ok(c.avg === null || Number.isFinite(c.avg))
    assert.ok(c.count >= 0)
  }
})

// ── regime ───────────────────────────────────────────────────────────────────
test('classifyRegime labels only bull/bear/sideways', () => {
  const r = classifyRegime(GOOD)
  assert.ok(r.series.length === GOOD.length)
  const labels = new Set(['bull', 'bear', 'sideways', null])
  for (const p of r.series) assert.ok(labels.has(p.regime))
  assert.ok(['bull', 'bear', 'sideways', null].includes(r.current))
})

// ── relative strength ────────────────────────────────────────────────────────
test('relativeSeries aligns by date and normalizes to 100', () => {
  const bench = makeSeries(600, 400, 0.0003, 0.008, 7)
  const rel = relativeSeries(GOOD, bench, 'SPY')
  assert.ok(rel.stats)
  assert.ok(Math.abs(rel.series[0].stock - 100) < 1e-6)
  assert.ok(Math.abs(rel.series[0].bench - 100) < 1e-6)
  for (const p of rel.series) { assert.ok(Number.isFinite(p.stock)); assert.ok(Number.isFinite(p.bench)) }
  // mismatched dates -> too few aligned -> empty (safe)
  const shifted = GOOD.map(r => ({ date: r.date + 'X', close: r.close }))
  assert.equal(relativeSeries(GOOD, shifted).series.length, 0)
})

// ── earnings reactions ───────────────────────────────────────────────────────
test('earningsReactions maps event dates onto series', () => {
  const events = [{ date: GOOD[100].date, surprise_pct: 5 }, { date: GOOD[200].date }]
  const rx = earningsReactions(GOOD, events)
  assert.equal(rx.length, 2)
  for (const e of rx) assert.ok(Number.isFinite(e.reactionPct))
  assert.equal(rx[0].surprisePct, 5)
  assert.equal(rx[1].surprisePct, null)
  // event before series start -> skipped safely
  assert.equal(earningsReactions(GOOD, [{ date: '1990-01-01' }]).length, 0)
})

// ── factor radar ─────────────────────────────────────────────────────────────
test('factorScores are all within 0..100', () => {
  const f = factorScores(GOOD, { relPct: 5 })
  assert.ok(f.length >= 4)
  for (const x of f) { assert.ok(x.score >= 0 && x.score <= 100); assert.ok(typeof x.factor === 'string') }
})

test('sectorEtf mapping', () => {
  assert.equal(sectorEtf('Technology'), 'XLK')
  assert.equal(sectorEtf('Consumer Defensive'), 'XLP')
  assert.equal(sectorEtf('Nonexistent'), null)
  assert.equal(sectorEtf(null), null)
})

// ── THE BIG ONE: nothing throws on adversarial input, nothing leaks NaN ───────
test('every function survives adversarial input without throwing', () => {
  const fns = [
    a => closesFrom(a), a => datesFrom(a), a => mean(a), a => stdev(a),
    a => dailyReturns(a), a => logReturns(a), a => gbmProjection(a),
    a => probReach(a, 100), a => histogram(a), a => volCone(a),
    a => drawdownSeries(a), a => monthlySeasonality(a), a => classifyRegime(a),
    a => relativeSeries(a, a), a => earningsReactions(a, a), a => factorScores(a),
  ]
  for (const fn of fns) {
    for (const inp of ADVERSARIAL) {
      assert.doesNotThrow(() => fn(inp), `threw on input ${JSON.stringify(inp)}`)
    }
  }
})

test('gbmProjection on short/garbage returns empty (never partial NaN)', () => {
  for (const inp of ADVERSARIAL) {
    const r = gbmProjection(inp)
    assert.deepEqual(r, { points: [], stats: null })
  }
  // exactly at the 30-point minimum boundary
  assert.deepEqual(gbmProjection(GOOD_CLOSES.slice(0, 10)), { points: [], stats: null })
})
