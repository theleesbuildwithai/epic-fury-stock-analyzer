import { useState, useEffect } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, Cell, LineChart, Line, Area, AreaChart,
} from 'recharts'

// ============================================================
// System Intelligence — Institutional Analytics Dashboard
//
// 5 panels:
//   1. P&L Attribution Waterfall (per factor)
//   2. Factor Health Table (IC, Sharpe, Verdict)
//   3. Risk Decomposition (VaR, Beta, Gross/Net, Sector heatmap)
//   4. Correlation Heatmap (factor-to-factor)
//   5. Drawdown Timeline + Stress Tests
//
// Data source: /api/factor-analytics
// ============================================================

export default function SystemIntelligence() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let cancel = false
    fetch('/api/factor-analytics')
      .then(r => r.json())
      .then(d => { if (!cancel) { setData(d); setLoading(false) } })
      .catch(e => { if (!cancel) { setErr(String(e)); setLoading(false) } })
    return () => { cancel = true }
  }, [])

  if (loading) return <Loading />
  if (err) return <Error err={err} />
  if (!data || data.error) return <Error err={data?.error || 'No data'} />

  return (
    <div style={{padding: '20px', maxWidth: '1600px', margin: '0 auto', color: '#e0e0e0'}}>
      <Header data={data} />
      <SummaryStrip data={data} />
      <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px', marginTop: '24px'}}>
        <Panel title="P&L Attribution Waterfall" subtitle="Cumulative $ contribution per factor">
          <PnLAttributionWaterfall data={data} />
        </Panel>
        <Panel title="Risk Decomposition" subtitle="VaR / Beta / Exposure / HHI">
          <RiskPanel risk={data.portfolio_risk} />
        </Panel>
        <Panel title="Factor Health Table" subtitle="IC / Sharpe / Hit Rate / Verdict">
          <FactorHealthTable factors={data.factor_analytics} />
        </Panel>
        <Panel title="Sector Exposure" subtitle="Long $ / Short $ / Net">
          <SectorBars sectors={data.portfolio_risk?.sector_exposure || []} />
        </Panel>
        <Panel title="Correlation Heatmap" subtitle="Factor-to-factor Spearman ρ — finds collinear losers">
          <CorrelationHeatmap matrix={data.factor_correlation_matrix} />
        </Panel>
        <Panel title="Regime State" subtitle="Combined HMM + Vol + Trend + Breadth">
          <RegimePanel regime={data.regime_state} brake={data.drawdown_brake} />
        </Panel>
        <Panel title="Drawdown Timeline" subtitle="Underwater curve (30d)">
          <DrawdownChart series={data.portfolio_risk?.underwater_curve_30d || []} />
        </Panel>
        <Panel title="Stress Tests" subtitle="Replay crisis scenarios on current portfolio">
          <StressTable stress={data.stress_tests} />
        </Panel>
      </div>
    </div>
  )
}

// ============================================================
// Subcomponents
// ============================================================

function Header({data}) {
  return (
    <div style={{borderBottom: '1px solid #333', paddingBottom: '12px', marginBottom: '12px'}}>
      <h1 style={{color: '#fff', margin: 0, fontSize: '28px'}}>System Intelligence</h1>
      <div style={{display: 'flex', gap: '24px', marginTop: '8px', color: '#888', fontSize: '13px'}}>
        <span>As of: {data.as_of}</span>
        <span>Version: {data.version}</span>
        <span>NAV: ${(data.nav || 0).toLocaleString()}</span>
        <span>Trades analyzed: {data.closed_trades_analyzed}</span>
        <span>Open: {data.open_positions_count}</span>
      </div>
    </div>
  )
}

function SummaryStrip({data}) {
  const r = data.portfolio_risk || {}
  const stats = [
    {label: 'VaR 95% (1d)', val: fmtPct(r.var_95_historical_pct), color: '#ff6b6b'},
    {label: 'VaR 99% (1d)', val: fmtPct(r.var_99_historical_pct), color: '#ff4444'},
    {label: 'Expected Shortfall', val: fmtPct(r.es_95_pct), color: '#ff8800'},
    {label: 'Sharpe (ann)', val: fmtNum(r.sharpe_annualized, 2), color: '#4ade80'},
    {label: 'Sortino (ann)', val: fmtNum(r.sortino_annualized, 2), color: '#4ade80'},
    {label: 'Max DD', val: fmtPct(r.max_drawdown_pct), color: '#ff6b6b'},
    {label: 'Beta to SPY', val: fmtNum(r.beta_to_spy_60d, 2), color: '#fbbf24'},
    {label: 'Gross Exposure', val: fmtPct(r.exposure?.gross_pct_nav), color: '#60a5fa'},
    {label: 'Net Exposure', val: fmtPct(r.exposure?.net_pct_nav), color: '#60a5fa'},
    {label: 'HHI', val: fmtNum(r.concentration_hhi, 3), color: '#fbbf24'},
    {label: 'Realized Vol', val: fmtPct(r.realized_vol_annualized * 100), color: '#fbbf24'},
    {label: 'Kurtosis', val: fmtNum(r.kurtosis_excess, 2), color: '#fbbf24'},
  ]
  return (
    <div style={{display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '8px', marginTop: '12px'}}>
      {stats.map(s => (
        <div key={s.label} style={{
          background: '#1a1a1a', padding: '10px 12px', borderRadius: '6px',
          borderLeft: `3px solid ${s.color}`,
        }}>
          <div style={{color: '#888', fontSize: '11px'}}>{s.label}</div>
          <div style={{color: s.color, fontSize: '18px', fontWeight: 600}}>{s.val ?? 'N/A'}</div>
        </div>
      ))}
    </div>
  )
}

function Panel({title, subtitle, children}) {
  return (
    <div style={{
      background: '#1a1a1a', borderRadius: '8px', padding: '16px',
      border: '1px solid #2a2a2a',
    }}>
      <h3 style={{margin: 0, color: '#fff', fontSize: '15px'}}>{title}</h3>
      <div style={{color: '#888', fontSize: '12px', marginBottom: '12px'}}>{subtitle}</div>
      {children}
    </div>
  )
}

function PnLAttributionWaterfall({data}) {
  const items = (data.factor_pnl_attribution || {})
  const arr = Object.entries(items)
    .map(([factor, pnl]) => ({factor, pnl: Number(pnl) || 0}))
    .sort((a, b) => b.pnl - a.pnl)
    .slice(0, 22)
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={arr}>
        <CartesianGrid stroke="#2a2a2a" strokeDasharray="3 3" />
        <XAxis dataKey="factor" stroke="#666" angle={-45} textAnchor="end" height={80} fontSize={10} />
        <YAxis stroke="#666" fontSize={11} />
        <Tooltip contentStyle={{background: '#000', border: '1px solid #444'}} />
        <Bar dataKey="pnl">
          {arr.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? '#4ade80' : '#ff6b6b'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function FactorHealthTable({factors}) {
  if (!factors || !factors.length) return <div style={{color: '#888'}}>No factor data</div>
  return (
    <div style={{maxHeight: 360, overflowY: 'auto'}}>
      <table style={{width: '100%', fontSize: '12px', color: '#ddd'}}>
        <thead style={{position: 'sticky', top: 0, background: '#1a1a1a'}}>
          <tr style={{textAlign: 'left', borderBottom: '1px solid #333'}}>
            <th style={{padding: '6px 4px'}}>Factor</th>
            <th style={{padding: '6px 4px'}}>IC</th>
            <th style={{padding: '6px 4px'}}>Sharpe</th>
            <th style={{padding: '6px 4px'}}>Hit%</th>
            <th style={{padding: '6px 4px'}}>N</th>
            <th style={{padding: '6px 4px'}}>Verdict</th>
            <th style={{padding: '6px 4px'}}>Wt</th>
          </tr>
        </thead>
        <tbody>
          {factors.map(f => (
            <tr key={f.factor} style={{borderBottom: '1px solid #222'}}>
              <td style={{padding: '6px 4px', fontWeight: 600}}>{f.factor}</td>
              <td style={{padding: '6px 4px', color: f.ic_60d_spearman > 0 ? '#4ade80' : '#ff6b6b'}}>
                {fmtNum(f.ic_60d_spearman, 3)}
              </td>
              <td style={{padding: '6px 4px', color: f.sharpe_60d_annualized > 0 ? '#4ade80' : '#ff6b6b'}}>
                {fmtNum(f.sharpe_60d_annualized, 2)}
              </td>
              <td style={{padding: '6px 4px'}}>{fmtNum(f.hit_rate_60d_pct, 1)}</td>
              <td style={{padding: '6px 4px', color: '#888'}}>{f.total_trades}</td>
              <td style={{padding: '6px 4px'}}>
                <span style={{
                  padding: '2px 6px', borderRadius: '3px', fontSize: '10px',
                  background: verdictColor(f.verdict).bg, color: verdictColor(f.verdict).fg,
                }}>{f.verdict}</span>
              </td>
              <td style={{padding: '6px 4px'}}>
                {fmtNum(f.current_weight, 3)}
                {f.proposed_weight !== f.current_weight && (
                  <span style={{color: '#60a5fa', fontSize: '10px'}}> → {fmtNum(f.proposed_weight, 3)}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RiskPanel({risk}) {
  if (!risk) return <div style={{color: '#888'}}>No risk data</div>
  const ex = risk.exposure || {}
  return (
    <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '12px'}}>
      <KV label="VaR 95% hist" val={fmtPct(risk.var_95_historical_pct)} />
      <KV label="VaR 95% param" val={fmtPct(risk.var_95_parametric_pct)} />
      <KV label="VaR 99%" val={fmtPct(risk.var_99_historical_pct)} />
      <KV label="ES 95%" val={fmtPct(risk.es_95_pct)} />
      <KV label="Beta SPY 60d" val={fmtNum(risk.beta_to_spy_60d, 2)} />
      <KV label="Long $" val={`$${fmtN(ex.long_dollars)}`} />
      <KV label="Short $" val={`$${fmtN(ex.short_dollars)}`} />
      <KV label="Gross %NAV" val={fmtPct(ex.gross_pct_nav)} />
      <KV label="Net %NAV" val={fmtPct(ex.net_pct_nav)} />
      <KV label="Beta-adj %NAV" val={fmtPct(ex.beta_adjusted_pct_nav)} />
      <KV label="Concentration HHI" val={fmtNum(risk.concentration_hhi, 3)} />
      <KV label="Top 5 weight %" val={fmtPct(risk.top5_weight_pct)} />
      <KV label="Realized Vol" val={fmtPct(risk.realized_vol_annualized * 100)} />
      <KV label="Vol of Vol" val={fmtNum(risk.vol_of_vol, 4)} />
      <KV label="Kurtosis (excess)" val={fmtNum(risk.kurtosis_excess, 2)} />
      <KV label="Max Drawdown" val={fmtPct(risk.max_drawdown_pct)} />
      <KV label="Current DD" val={fmtPct(risk.current_drawdown_pct)} />
      <KV label="Days since peak" val={risk.days_since_peak} />
      <KV label="Sortino" val={fmtNum(risk.sortino_annualized, 2)} />
      <KV label="Calmar" val={fmtNum(risk.calmar, 2)} />
      <KV label="Omega" val={fmtNum(risk.omega, 2)} />
    </div>
  )
}

function SectorBars({sectors}) {
  if (!sectors.length) return <div style={{color: '#888'}}>No sector exposure</div>
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={sectors} layout="horizontal">
        <CartesianGrid stroke="#2a2a2a" strokeDasharray="3 3" />
        <XAxis dataKey="sector" stroke="#666" angle={-30} textAnchor="end" height={60} fontSize={10} />
        <YAxis stroke="#666" fontSize={11} />
        <Tooltip contentStyle={{background: '#000', border: '1px solid #444'}} />
        <Legend />
        <Bar dataKey="long_dollars" stackId="a" fill="#4ade80" name="Long $" />
        <Bar dataKey="short_dollars" stackId="a" fill="#ff6b6b" name="Short $" />
      </BarChart>
    </ResponsiveContainer>
  )
}

function CorrelationHeatmap({matrix}) {
  if (!matrix || !matrix.labels || !matrix.matrix) return <div style={{color: '#888'}}>No correlation data</div>
  const cells = []
  matrix.matrix.forEach((row, i) => {
    row.forEach((v, j) => {
      cells.push({i, j, label_i: matrix.labels[i], label_j: matrix.labels[j], val: v})
    })
  })
  const n = matrix.labels.length
  return (
    <div style={{overflowX: 'auto', maxHeight: 360}}>
      <table style={{borderCollapse: 'collapse', fontSize: '9px'}}>
        <tbody>
          <tr>
            <td></td>
            {matrix.labels.map(l => (
              <td key={l} style={{padding: '2px', writingMode: 'vertical-rl', textOrientation: 'mixed', color: '#888', maxWidth: 14}}>
                {l.substring(0, 8)}
              </td>
            ))}
          </tr>
          {matrix.labels.map((row_l, i) => (
            <tr key={row_l}>
              <td style={{padding: '2px 6px', color: '#888', whiteSpace: 'nowrap'}}>{row_l.substring(0, 12)}</td>
              {matrix.matrix[i].map((v, j) => (
                <td key={j} title={`${row_l} vs ${matrix.labels[j]}: ${(v || 0).toFixed(3)}`} style={{
                  width: 14, height: 14, background: corrColor(v),
                  border: '1px solid #0a0a0a',
                }} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RegimePanel({regime, brake}) {
  if (!regime) return <div style={{color: '#888'}}>No regime data</div>
  return (
    <div>
      <div style={{display: 'flex', gap: '12px', marginBottom: '12px'}}>
        <div style={{flex: 1, padding: '12px', background: '#0a0a0a', borderRadius: 4}}>
          <div style={{color: '#888', fontSize: 11}}>Combined Regime</div>
          <div style={{color: regimeColor(regime.regime), fontSize: 24, fontWeight: 700}}>{regime.regime}</div>
          <div style={{color: '#888', fontSize: 11}}>{regime.confidence_pct}% confidence</div>
        </div>
        <div style={{flex: 1, padding: '12px', background: '#0a0a0a', borderRadius: 4}}>
          <div style={{color: '#888', fontSize: 11}}>Drawdown Brake</div>
          <div style={{color: brakeColor(brake?.status), fontSize: 20, fontWeight: 700}}>
            {brake?.status || 'N/A'}
          </div>
          <div style={{color: '#888', fontSize: 11}}>
            Exp mult: {brake?.exposure_multiplier ?? 'N/A'}x · DD: {fmtPct(brake?.drawdown_pct)}
          </div>
        </div>
      </div>
      <div style={{fontSize: 11, color: '#aaa'}}>
        Components — HMM: <b>{regime.components?.hmm?.regime}</b>{', '}
        Vol: <b>{regime.components?.volatility?.regime}</b>{', '}
        Trend: <b>{regime.components?.trend?.regime}</b>{', '}
        Breadth: <b>{regime.components?.breadth?.regime}</b>
      </div>
      {regime.components?.hmm?.transition_probs && (
        <div style={{fontSize: 11, marginTop: 8, color: '#aaa'}}>
          Transition probs 5d — BULL: {regime.components.hmm.transition_probs.BULL_5d}{', '}
          BEAR: {regime.components.hmm.transition_probs.BEAR_5d}{', '}
          SIDEWAYS: {regime.components.hmm.transition_probs.SIDEWAYS_5d}
        </div>
      )}
    </div>
  )
}

function DrawdownChart({series}) {
  if (!series.length) return <div style={{color: '#888'}}>No drawdown series</div>
  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={series}>
        <CartesianGrid stroke="#2a2a2a" strokeDasharray="3 3" />
        <XAxis dataKey="day" stroke="#666" />
        <YAxis stroke="#666" />
        <Tooltip contentStyle={{background: '#000', border: '1px solid #444'}} />
        <Area type="monotone" dataKey="drawdown_pct" stroke="#ff6b6b" fill="#ff6b6b22" />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function StressTable({stress}) {
  if (!stress || !stress.by_scenario) return <div style={{color: '#888'}}>No stress data</div>
  return (
    <div style={{fontSize: 12, color: '#ddd'}}>
      <table style={{width: '100%'}}>
        <thead style={{color: '#888'}}>
          <tr>
            <th style={{textAlign: 'left', padding: '4px'}}>Scenario</th>
            <th style={{textAlign: 'right', padding: '4px'}}>SPY DD</th>
            <th style={{textAlign: 'right', padding: '4px'}}>Est Loss $</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(stress.by_scenario).map(([name, s]) => (
            <tr key={name} style={{borderBottom: '1px solid #222'}}>
              <td style={{padding: '6px 4px'}}>{name}</td>
              <td style={{padding: '6px 4px', textAlign: 'right', color: '#ff6b6b'}}>
                {fmtPct(s.spy_drawdown_pct)}
              </td>
              <td style={{padding: '6px 4px', textAlign: 'right', color: '#ff6b6b'}}>
                ${fmtN(s.stress_result?.expected_portfolio_loss_dollars)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {stress.worst_case_scenario && (
        <div style={{marginTop: 8, padding: '8px', background: '#330000', borderRadius: 4, fontSize: 11}}>
          <b>Worst case:</b> {stress.worst_case_scenario}{' → '}
          <span style={{color: '#ff6b6b'}}>${fmtN(stress.worst_case_loss_dollars)} loss</span>
        </div>
      )}
    </div>
  )
}

// ============================================================
// Helpers
// ============================================================

function KV({label, val}) {
  return (
    <div style={{padding: '6px 0', borderBottom: '1px solid #2a2a2a'}}>
      <div style={{color: '#888', fontSize: '10px'}}>{label}</div>
      <div style={{color: '#fff', fontSize: '13px', fontWeight: 600}}>{val ?? 'N/A'}</div>
    </div>
  )
}

function Loading() {
  return <div style={{padding: 40, color: '#888', textAlign: 'center'}}>Loading analytics...</div>
}

function Error({err}) {
  return <div style={{padding: 40, color: '#ff6b6b'}}>Error: {err}</div>
}

function fmtPct(v) {
  if (v === null || v === undefined || isNaN(v)) return null
  return `${Number(v).toFixed(2)}%`
}

function fmtNum(v, decimals = 2) {
  if (v === null || v === undefined || isNaN(v)) return null
  return Number(v).toFixed(decimals)
}

function fmtN(v) {
  if (v === null || v === undefined || isNaN(v)) return 'N/A'
  return Math.abs(v).toLocaleString()
}

function verdictColor(v) {
  if (v === 'UPGRADE') return {bg: '#16a34a', fg: '#fff'}
  if (v === 'KILL') return {bg: '#dc2626', fg: '#fff'}
  if (v === 'DOWNGRADE') return {bg: '#f59e0b', fg: '#000'}
  return {bg: '#334155', fg: '#fff'}
}

function regimeColor(r) {
  if (r === 'BULL') return '#4ade80'
  if (r === 'BEAR') return '#ff6b6b'
  if (r === 'SIDEWAYS') return '#fbbf24'
  return '#888'
}

function brakeColor(s) {
  if (s === 'OK') return '#4ade80'
  if (s === 'WATCH') return '#fbbf24'
  if (s === 'BRAKE_ENGAGED') return '#f97316'
  if (s === 'DEFENSIVE' || s === 'CRITICAL') return '#ff6b6b'
  return '#888'
}

function corrColor(v) {
  // Red for negative, green for positive, gray for ~0
  const n = Number(v) || 0
  if (n > 0.7) return '#16a34a'
  if (n > 0.3) return '#16a34a88'
  if (n > -0.3) return '#33333322'
  if (n > -0.7) return '#ff6b6b88'
  return '#dc2626'
}
