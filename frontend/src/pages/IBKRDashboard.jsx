import { useState, useEffect, useCallback } from 'react'

export default function IBKRDashboard() {
  const [status, setStatus] = useState(null)
  const [positions, setPositions] = useState([])
  const [orders, setOrders] = useState({ open_orders: [], order_log: [] })
  const [loading, setLoading] = useState(true)
  const [killConfirm, setKillConfirm] = useState(false)
  const [actionMessage, setActionMessage] = useState(null)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/ibkr/status')
      const json = await res.json()
      setStatus(json)
      // If status came from S3 snapshot, it includes positions + orders.
      // Use those instead of separate API calls (App Runner has no Gateway).
      if (json?.data_source === 's3_snapshot') {
        if (Array.isArray(json.positions)) setPositions(json.positions)
        if (Array.isArray(json.open_orders) || Array.isArray(json.recent_orders)) {
          setOrders({
            open_orders: json.open_orders || [],
            order_log: json.recent_orders || [],
          })
        }
      }
      return json
    } catch {
      setStatus(null)
      return null
    }
  }, [])

  const fetchPositions = useCallback(async (skip = false) => {
    if (skip) return
    try {
      const res = await fetch('/api/ibkr/positions')
      const json = await res.json()
      setPositions(json.positions || [])
    } catch {
      setPositions([])
    }
  }, [])

  const fetchOrders = useCallback(async (skip = false) => {
    if (skip) return
    try {
      const res = await fetch('/api/ibkr/orders')
      const json = await res.json()
      setOrders(json)
    } catch {
      setOrders({ open_orders: [], order_log: [] })
    }
  }, [])

  const fetchAll = useCallback(async () => {
    setLoading(true)
    // Fetch status first — it tells us if we're in snapshot mode (data already included)
    const statusJson = await fetchStatus()
    const usingSnapshot = statusJson?.data_source === 's3_snapshot'
    // Skip separate position/order calls when snapshot already has them
    await Promise.all([fetchPositions(usingSnapshot), fetchOrders(usingSnapshot)])
    setLoading(false)
  }, [fetchStatus, fetchPositions, fetchOrders])

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 15000) // Refresh every 15s
    return () => clearInterval(interval)
  }, [fetchAll])

  const handleToggle = async () => {
    try {
      const res = await fetch('/api/ibkr/toggle', { method: 'POST' })
      const json = await res.json()
      setActionMessage(`IBKR ${json.enabled ? 'ENABLED' : 'DISABLED'}`)
      setTimeout(() => setActionMessage(null), 3000)
      fetchAll()
    } catch (e) {
      setActionMessage(`Error: ${e.message}`)
    }
  }

  const handleKillSwitch = async () => {
    if (!killConfirm) {
      setKillConfirm(true)
      setTimeout(() => setKillConfirm(false), 5000)
      return
    }
    try {
      const res = await fetch('/api/ibkr/kill-switch', { method: 'POST' })
      const json = await res.json()
      setActionMessage(`KILL SWITCH: Closed ${json.closed?.length || 0} positions`)
      setKillConfirm(false)
      setTimeout(() => setActionMessage(null), 5000)
      fetchAll()
    } catch (e) {
      setActionMessage(`Kill switch error: ${e.message}`)
    }
  }

  const handleUnhalt = async () => {
    try {
      const res = await fetch('/api/ibkr/unhalt', { method: 'POST' })
      const json = await res.json()
      setActionMessage(json.message || 'Trading resumed')
      setTimeout(() => setActionMessage(null), 3000)
      fetchAll()
    } catch (e) {
      setActionMessage(`Error: ${e.message}`)
    }
  }

  const s = status?.status || {}
  const acc = status?.account || {}
  const isConnected = s.connected
  const isEnabled = s.enabled
  const isHalted = s.trading_halted
  const mode = s.mode || 'PAPER'
  const dataSource = status?.data_source
  const fromSnapshot = dataSource === 's3_snapshot'
  const snapshotAge = status?.snapshot_age_seconds
  const snapshotStale = status?.snapshot_stale

  return (
    <div className="min-h-screen bg-neutral-950 text-white p-4 md:p-8">
      <div className="max-w-7xl mx-auto">

        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold flex items-center gap-3">
              Interactive Brokers
              <span className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium ${
                isConnected ? 'bg-green-900/50 text-green-400 border border-green-700'
                            : 'bg-red-900/50 text-red-400 border border-red-700'
              }`}>
                <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'}`}></span>
                {isConnected ? 'Connected' : 'Disconnected'}
              </span>
              <span className={`px-3 py-1 rounded-full text-sm font-medium ${
                mode === 'PAPER' ? 'bg-white/50 text-white border border-neutral-700'
                                 : 'bg-red-900/50 text-red-400 border border-red-700'
              }`}>
                {mode}
              </span>
            </h1>
            <p className="text-gray-400 mt-1">Dual-track execution — paper trades always run alongside IBKR</p>
            {dataSource && (
              <p className="text-xs mt-2">
                {dataSource === 'live_gateway' && (
                  <span className="text-green-400">Live data — direct Gateway connection</span>
                )}
                {dataSource === 's3_snapshot' && !snapshotStale && (
                  <span className="text-green-400">
                    Live mirror via S3 snapshot ({snapshotAge != null ? `${snapshotAge}s old` : 'fresh'})
                  </span>
                )}
                {dataSource === 's3_snapshot' && snapshotStale && (
                  <span className="text-red-400">
                    Stale snapshot ({snapshotAge}s old) — EC2 pusher may be down
                  </span>
                )}
                {dataSource === 'no_connection' && (
                  <span className="text-gray-500">
                    No IBKR connection here. Set up EC2 with IBKR_PUSH_SNAPSHOT=true to mirror.
                  </span>
                )}
                {dataSource === 'error' && (
                  <span className="text-red-400">Error fetching IBKR data</span>
                )}
              </p>
            )}
          </div>
          <button
            onClick={fetchAll}
            disabled={loading}
            className="px-4 py-2 bg-neutral-800 hover:bg-neutral-700 rounded-lg text-sm border border-neutral-700 transition"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>

        {/* Action Message Banner */}
        {actionMessage && (
          <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-xl text-red-300 font-medium">
            {actionMessage}
          </div>
        )}

        {/* Safety Controls */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
          {/* Toggle */}
          <button
            onClick={handleToggle}
            className={`p-6 rounded-xl border-2 transition text-left ${
              isEnabled
                ? 'bg-green-900/20 border-green-700 hover:bg-green-900/30'
                : 'bg-neutral-900 border-neutral-700 hover:bg-neutral-800'
            }`}
          >
            <div className="text-sm text-gray-400 mb-1">IBKR Execution</div>
            <div className={`text-2xl font-bold ${isEnabled ? 'text-green-400' : 'text-gray-500'}`}>
              {isEnabled ? 'ENABLED' : 'DISABLED'}
            </div>
            <div className="text-xs text-gray-500 mt-2">Click to toggle — paper trades always run</div>
          </button>

          {/* Kill Switch */}
          <button
            onClick={handleKillSwitch}
            className={`p-6 rounded-xl border-2 transition text-left ${
              killConfirm
                ? 'bg-red-800 border-red-500 animate-pulse'
                : 'bg-red-900/20 border-red-800 hover:bg-red-900/40'
            }`}
          >
            <div className="text-sm text-gray-400 mb-1">Emergency</div>
            <div className="text-2xl font-bold text-red-400">
              {killConfirm ? 'CLICK AGAIN TO CONFIRM' : 'KILL SWITCH'}
            </div>
            <div className="text-xs text-gray-500 mt-2">
              {killConfirm ? 'This will close ALL positions!' : 'Flatten all positions immediately'}
            </div>
          </button>

          {/* Halt Status */}
          <button
            onClick={isHalted ? handleUnhalt : undefined}
            className={`p-6 rounded-xl border-2 transition text-left ${
              isHalted
                ? 'bg-red-900/20 border-red-700 hover:bg-red-900/30 cursor-pointer'
                : 'bg-neutral-900 border-neutral-700 cursor-default'
            }`}
          >
            <div className="text-sm text-gray-400 mb-1">Trading Status</div>
            <div className={`text-2xl font-bold ${isHalted ? 'text-red-400' : 'text-green-400'}`}>
              {isHalted ? 'HALTED' : 'ACTIVE'}
            </div>
            <div className="text-xs text-gray-500 mt-2">
              {isHalted ? 'Click to resume trading' : 'System operating normally'}
            </div>
          </button>
        </div>

        {/* Account Summary */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">Account Summary</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
            <div>
              <div className="text-sm text-gray-400">Net Liquidation</div>
              <div className="text-xl font-bold text-white">
                ${(acc.net_liquidation || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </div>
            </div>
            <div>
              <div className="text-sm text-gray-400">Buying Power</div>
              <div className="text-xl font-bold text-white">
                ${(acc.buying_power || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </div>
            </div>
            <div>
              <div className="text-sm text-gray-400">Cash</div>
              <div className="text-xl font-bold text-white">
                ${(acc.cash || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </div>
            </div>
            <div>
              <div className="text-sm text-gray-400">Daily P&L</div>
              <div className={`text-xl font-bold ${(acc.daily_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                ${(acc.daily_pnl || 0).toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-sm text-gray-400">Unrealized P&L</div>
              <div className={`text-xl font-bold ${(acc.unrealized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                ${(acc.unrealized_pnl || 0).toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-sm text-gray-400">Orders Today</div>
              <div className="text-xl font-bold text-white">
                {acc.orders_today || 0}
              </div>
            </div>
          </div>
        </div>

        {/* Positions */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">
            IBKR Positions ({positions.length})
          </h2>
          {positions.length === 0 ? (
            <div className="text-gray-500 text-center py-8">
              {isConnected ? 'No open positions' : 'Connect to IBKR to view positions'}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 border-b border-neutral-800">
                  <tr>
                    <th className="text-left py-3 px-2">Ticker</th>
                    <th className="text-left py-3 px-2">Direction</th>
                    <th className="text-right py-3 px-2">Shares</th>
                    <th className="text-right py-3 px-2">Avg Cost</th>
                    <th className="text-right py-3 px-2">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((pos, i) => (
                    <tr key={i} className="border-b border-neutral-800/50 hover:bg-neutral-800/30">
                      <td className="py-3 px-2 font-medium text-white">{pos.ticker}</td>
                      <td className="py-3 px-2">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                          pos.direction === 'long' ? 'bg-green-900/50 text-green-400' : 'bg-red-900/50 text-red-400'
                        }`}>
                          {pos.direction?.toUpperCase()}
                        </span>
                      </td>
                      <td className="py-3 px-2 text-right">{Math.abs(pos.shares)}</td>
                      <td className="py-3 px-2 text-right">${pos.avg_cost?.toFixed(2)}</td>
                      <td className="py-3 px-2 text-right">${pos.value?.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Open Orders */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">
            Open Orders ({orders.open_orders?.length || 0})
          </h2>
          {(!orders.open_orders || orders.open_orders.length === 0) ? (
            <div className="text-gray-500 text-center py-8">No open orders</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 border-b border-neutral-800">
                  <tr>
                    <th className="text-left py-3 px-2">Order ID</th>
                    <th className="text-left py-3 px-2">Ticker</th>
                    <th className="text-left py-3 px-2">Action</th>
                    <th className="text-right py-3 px-2">Qty</th>
                    <th className="text-left py-3 px-2">Type</th>
                    <th className="text-right py-3 px-2">Limit</th>
                    <th className="text-right py-3 px-2">Stop</th>
                    <th className="text-left py-3 px-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.open_orders.map((o, i) => (
                    <tr key={i} className="border-b border-neutral-800/50 hover:bg-neutral-800/30">
                      <td className="py-3 px-2 text-gray-400">{o.order_id}</td>
                      <td className="py-3 px-2 font-medium text-white">{o.ticker}</td>
                      <td className="py-3 px-2">
                        <span className={o.action === 'BUY' ? 'text-green-400' : 'text-red-400'}>
                          {o.action}
                        </span>
                      </td>
                      <td className="py-3 px-2 text-right">{o.quantity}</td>
                      <td className="py-3 px-2 text-gray-400">{o.order_type}</td>
                      <td className="py-3 px-2 text-right">{o.limit_price ? `$${o.limit_price.toFixed(2)}` : '—'}</td>
                      <td className="py-3 px-2 text-right">{o.stop_price ? `$${o.stop_price.toFixed(2)}` : '—'}</td>
                      <td className="py-3 px-2">
                        <span className="px-2 py-0.5 rounded text-xs bg-neutral-800 text-gray-300">
                          {o.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Order Log */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6">
          <h2 className="text-xl font-semibold mb-4">
            Order Log (Last {status?.recent_orders?.length || 0})
          </h2>
          {(!status?.recent_orders || status.recent_orders.length === 0) ? (
            <div className="text-gray-500 text-center py-8">No order events yet</div>
          ) : (
            <div className="overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 border-b border-neutral-800 sticky top-0 bg-neutral-900">
                  <tr>
                    <th className="text-left py-3 px-2">Time</th>
                    <th className="text-left py-3 px-2">Action</th>
                    <th className="text-left py-3 px-2">Ticker</th>
                    <th className="text-left py-3 px-2">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {[...status.recent_orders].reverse().map((event, i) => (
                    <tr key={i} className="border-b border-neutral-800/50 hover:bg-neutral-800/30">
                      <td className="py-2 px-2 text-gray-400 text-xs font-mono whitespace-nowrap">
                        {event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '—'}
                      </td>
                      <td className="py-2 px-2">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                          event.action === 'FILLED' ? 'bg-green-900/50 text-green-400' :
                          event.action === 'BLOCKED' ? 'bg-red-900/50 text-red-400' :
                          event.action === 'KILL_SWITCH' ? 'bg-red-800 text-red-300' :
                          event.action === 'ERROR' ? 'bg-red-900/50 text-red-400' :
                          'bg-neutral-800 text-gray-300'
                        }`}>
                          {event.action}
                        </span>
                      </td>
                      <td className="py-2 px-2 font-medium text-white">
                        {event.ticker || '—'}
                      </td>
                      <td className="py-2 px-2 text-gray-400 text-xs">
                        {event.reason || event.error ||
                         (event.shares ? `${event.direction} ${event.shares} @ $${event.fill_price || event.price || '?'}` : '') ||
                         event.status || JSON.stringify(event).slice(0, 80)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>


        {/* Execution Analytics */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">Execution Analytics</h2>
          {(() => {
            const log = status?.recent_orders || []
            const fills = log.filter(e => e.action === 'FILLED' || e.action === 'EXIT_FILLED')
            const blocked = log.filter(e => e.action === 'BLOCKED')
            const errors = log.filter(e => e.action === 'ERROR' || e.action === 'TIMEOUT')
            const entries = log.filter(e => e.action === 'FILLED')
            const exits = log.filter(e => e.action === 'EXIT_FILLED')
            const longs = entries.filter(e => e.direction === 'long')
            const shorts = entries.filter(e => e.direction === 'short')
            const totalVolume = fills.reduce((s, e) => s + (Math.abs(e.shares || 0) * (e.fill_price || e.price || 0)), 0)
            const avgFillTime = fills.length > 0 ? 'instant' : '—'
            const fillRate = (fills.length + blocked.length + errors.length) > 0
              ? ((fills.length / (fills.length + blocked.length + errors.length)) * 100).toFixed(1)
              : '—'

            return (
              <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
                <div className="bg-neutral-800/50 rounded-lg p-4">
                  <div className="text-sm text-gray-400">Total Fills</div>
                  <div className="text-2xl font-bold text-green-400">{fills.length}</div>
                </div>
                <div className="bg-neutral-800/50 rounded-lg p-4">
                  <div className="text-sm text-gray-400">Fill Rate</div>
                  <div className="text-2xl font-bold text-white">{fillRate}%</div>
                </div>
                <div className="bg-neutral-800/50 rounded-lg p-4">
                  <div className="text-sm text-gray-400">Entries / Exits</div>
                  <div className="text-2xl font-bold text-white">{entries.length} / {exits.length}</div>
                </div>
                <div className="bg-neutral-800/50 rounded-lg p-4">
                  <div className="text-sm text-gray-400">Long / Short</div>
                  <div className="text-2xl font-bold">
                    <span className="text-green-400">{longs.length}</span>
                    <span className="text-gray-600"> / </span>
                    <span className="text-red-400">{shorts.length}</span>
                  </div>
                </div>
                <div className="bg-neutral-800/50 rounded-lg p-4">
                  <div className="text-sm text-gray-400">Volume Traded</div>
                  <div className="text-2xl font-bold text-white">${totalVolume.toLocaleString('en-US', { maximumFractionDigits: 0 })}</div>
                </div>
              </div>
            )
          })()}
        </div>

        {/* Risk Monitor */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">Risk Monitor</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {/* Exposure Bar */}
            <div>
              <div className="flex justify-between text-sm mb-2">
                <span className="text-gray-400">Total Exposure</span>
                <span className="text-white">
                  ${(acc.gross_position_value || 0).toLocaleString()} / ${(s.safety?.max_total_exposure || 25000).toLocaleString()}
                </span>
              </div>
              <div className="w-full bg-neutral-800 rounded-full h-3">
                <div
                  className={`h-3 rounded-full transition-all ${
                    ((acc.gross_position_value || 0) / (s.safety?.max_total_exposure || 25000)) > 0.8
                      ? 'bg-red-500' : ((acc.gross_position_value || 0) / (s.safety?.max_total_exposure || 25000)) > 0.5
                      ? 'bg-red-500' : 'bg-green-500'
                  }`}
                  style={{ width: `${Math.min(100, ((acc.gross_position_value || 0) / (s.safety?.max_total_exposure || 25000)) * 100)}%` }}
                ></div>
              </div>
            </div>

            {/* Daily Loss Bar */}
            <div>
              <div className="flex justify-between text-sm mb-2">
                <span className="text-gray-400">Daily Loss Used</span>
                <span className={`${(acc.daily_pnl || 0) < 0 ? 'text-red-400' : 'text-green-400'}`}>
                  ${Math.abs(acc.daily_pnl || 0).toFixed(2)} / ${(s.safety?.daily_loss_limit || 500).toLocaleString()}
                </span>
              </div>
              <div className="w-full bg-neutral-800 rounded-full h-3">
                <div
                  className={`h-3 rounded-full transition-all ${
                    (Math.abs(acc.daily_pnl || 0) / (s.safety?.daily_loss_limit || 500)) > 0.8
                      ? 'bg-red-500 animate-pulse' : 'bg-green-500'
                  }`}
                  style={{ width: `${Math.min(100, (Math.abs(Math.min(0, acc.daily_pnl || 0)) / (s.safety?.daily_loss_limit || 500)) * 100)}%` }}
                ></div>
              </div>
            </div>

            {/* Position Count */}
            <div>
              <div className="flex justify-between text-sm mb-2">
                <span className="text-gray-400">Open Positions</span>
                <span className="text-white">{positions.length}</span>
              </div>
              <div className="w-full bg-neutral-800 rounded-full h-3">
                <div
                  className="h-3 rounded-full bg-white transition-all"
                  style={{ width: `${Math.min(100, (positions.length / 10) * 100)}%` }}
                ></div>
              </div>
            </div>
          </div>

          {/* Risk Metrics Row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
            <div className="bg-neutral-800/30 rounded-lg p-3 text-center">
              <div className="text-xs text-gray-500">Unrealized P&L</div>
              <div className={`text-lg font-bold ${(acc.unrealized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                ${(acc.unrealized_pnl || 0).toFixed(2)}
              </div>
            </div>
            <div className="bg-neutral-800/30 rounded-lg p-3 text-center">
              <div className="text-xs text-gray-500">Realized P&L</div>
              <div className={`text-lg font-bold ${(acc.realized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                ${(acc.realized_pnl || 0).toFixed(2)}
              </div>
            </div>
            <div className="bg-neutral-800/30 rounded-lg p-3 text-center">
              <div className="text-xs text-gray-500">Buying Power</div>
              <div className="text-lg font-bold text-white">
                ${(acc.buying_power || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
            <div className="bg-neutral-800/30 rounded-lg p-3 text-center">
              <div className="text-xs text-gray-500">Net Liquidation</div>
              <div className="text-lg font-bold text-white">
                ${(acc.net_liquidation || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
          </div>
        </div>

        {/* System Health */}
        <div className="bg-neutral-900 rounded-xl border border-neutral-800 p-6">
          <h2 className="text-xl font-semibold mb-4">System Health</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="flex items-center gap-3 bg-neutral-800/30 rounded-lg p-4">
              <div className={`w-3 h-3 rounded-full ${isConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'}`}></div>
              <div>
                <div className="text-sm text-gray-400">Connection</div>
                <div className="text-sm font-medium text-white">{isConnected ? 'Healthy' : 'Disconnected'}</div>
              </div>
            </div>
            <div className="flex items-center gap-3 bg-neutral-800/30 rounded-lg p-4">
              <div className={`w-3 h-3 rounded-full ${!isHalted ? 'bg-green-400' : 'bg-red-400 animate-pulse'}`}></div>
              <div>
                <div className="text-sm text-gray-400">Trading Engine</div>
                <div className="text-sm font-medium text-white">{isHalted ? 'HALTED' : 'Running'}</div>
              </div>
            </div>
            <div className="flex items-center gap-3 bg-neutral-800/30 rounded-lg p-4">
              <div className={`w-3 h-3 rounded-full ${mode === 'PAPER' ? 'bg-white' : 'bg-red-400 animate-pulse'}`}></div>
              <div>
                <div className="text-sm text-gray-400">Trading Mode</div>
                <div className="text-sm font-medium text-white">{mode} (Port {s.port || '7497'})</div>
              </div>
            </div>
            <div className="flex items-center gap-3 bg-neutral-800/30 rounded-lg p-4">
              <div className="w-3 h-3 rounded-full bg-green-400"></div>
              <div>
                <div className="text-sm text-gray-400">Paper Trader</div>
                <div className="text-sm font-medium text-white">Always Active</div>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  )
}
