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
    } catch {
      setStatus(null)
    }
  }, [])

  const fetchPositions = useCallback(async () => {
    try {
      const res = await fetch('/api/ibkr/positions')
      const json = await res.json()
      setPositions(json.positions || [])
    } catch {
      setPositions([])
    }
  }, [])

  const fetchOrders = useCallback(async () => {
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
    await Promise.all([fetchStatus(), fetchPositions(), fetchOrders()])
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

  return (
    <div className="min-h-screen bg-gray-950 text-white p-4 md:p-8">
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
                mode === 'PAPER' ? 'bg-blue-900/50 text-blue-400 border border-blue-700'
                                 : 'bg-orange-900/50 text-orange-400 border border-orange-700'
              }`}>
                {mode}
              </span>
            </h1>
            <p className="text-gray-400 mt-1">Dual-track execution — paper trades always run alongside IBKR</p>
          </div>
          <button
            onClick={fetchAll}
            disabled={loading}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm border border-gray-700 transition"
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>

        {/* Action Message Banner */}
        {actionMessage && (
          <div className="mb-6 p-4 bg-yellow-900/30 border border-yellow-700 rounded-xl text-yellow-300 font-medium">
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
                : 'bg-gray-900 border-gray-700 hover:bg-gray-800'
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
                ? 'bg-orange-900/20 border-orange-700 hover:bg-orange-900/30 cursor-pointer'
                : 'bg-gray-900 border-gray-700 cursor-default'
            }`}
          >
            <div className="text-sm text-gray-400 mb-1">Trading Status</div>
            <div className={`text-2xl font-bold ${isHalted ? 'text-orange-400' : 'text-green-400'}`}>
              {isHalted ? 'HALTED' : 'ACTIVE'}
            </div>
            <div className="text-xs text-gray-500 mt-2">
              {isHalted ? 'Click to resume trading' : 'System operating normally'}
            </div>
          </button>
        </div>

        {/* Account Summary */}
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-8">
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

        {/* Safety Limits */}
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">Safety Limits</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-gray-800/50 rounded-lg p-4">
              <div className="text-sm text-gray-400">Max Per Position</div>
              <div className="text-lg font-bold text-yellow-400">
                ${(s.safety?.max_position_dollars || 5000).toLocaleString()}
              </div>
            </div>
            <div className="bg-gray-800/50 rounded-lg p-4">
              <div className="text-sm text-gray-400">Max Total Exposure</div>
              <div className="text-lg font-bold text-yellow-400">
                ${(s.safety?.max_total_exposure || 25000).toLocaleString()}
              </div>
            </div>
            <div className="bg-gray-800/50 rounded-lg p-4">
              <div className="text-sm text-gray-400">Daily Loss Limit</div>
              <div className="text-lg font-bold text-red-400">
                -${(s.safety?.daily_loss_limit || 500).toLocaleString()}
              </div>
            </div>
            <div className="bg-gray-800/50 rounded-lg p-4">
              <div className="text-sm text-gray-400">Max Orders/Day</div>
              <div className="text-lg font-bold text-blue-400">
                {s.safety?.max_orders_per_day || 50}
              </div>
            </div>
          </div>
        </div>

        {/* Positions */}
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-8">
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
                <thead className="text-gray-400 border-b border-gray-800">
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
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
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
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-6 mb-8">
          <h2 className="text-xl font-semibold mb-4">
            Open Orders ({orders.open_orders?.length || 0})
          </h2>
          {(!orders.open_orders || orders.open_orders.length === 0) ? (
            <div className="text-gray-500 text-center py-8">No open orders</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 border-b border-gray-800">
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
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
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
                        <span className="px-2 py-0.5 rounded text-xs bg-gray-800 text-gray-300">
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
        <div className="bg-gray-900 rounded-xl border border-gray-800 p-6">
          <h2 className="text-xl font-semibold mb-4">
            Order Log (Last {status?.recent_orders?.length || 0})
          </h2>
          {(!status?.recent_orders || status.recent_orders.length === 0) ? (
            <div className="text-gray-500 text-center py-8">No order events yet</div>
          ) : (
            <div className="overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="text-gray-400 border-b border-gray-800 sticky top-0 bg-gray-900">
                  <tr>
                    <th className="text-left py-3 px-2">Time</th>
                    <th className="text-left py-3 px-2">Action</th>
                    <th className="text-left py-3 px-2">Ticker</th>
                    <th className="text-left py-3 px-2">Details</th>
                  </tr>
                </thead>
                <tbody>
                  {[...status.recent_orders].reverse().map((event, i) => (
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                      <td className="py-2 px-2 text-gray-400 text-xs font-mono whitespace-nowrap">
                        {event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '—'}
                      </td>
                      <td className="py-2 px-2">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                          event.action === 'FILLED' ? 'bg-green-900/50 text-green-400' :
                          event.action === 'BLOCKED' ? 'bg-red-900/50 text-red-400' :
                          event.action === 'KILL_SWITCH' ? 'bg-red-800 text-red-300' :
                          event.action === 'ERROR' ? 'bg-orange-900/50 text-orange-400' :
                          'bg-gray-800 text-gray-300'
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

        {/* Setup Instructions */}
        {!isConnected && (
          <div className="mt-8 bg-gray-900 rounded-xl border border-gray-800 p-6">
            <h2 className="text-xl font-semibold mb-4">Setup Instructions</h2>
            <ol className="list-decimal list-inside space-y-3 text-gray-300">
              <li>Download <span className="text-blue-400">Trader Workstation (TWS)</span> or <span className="text-blue-400">IB Gateway</span> from IBKR</li>
              <li>Log in to your IBKR account (use Paper Trading account first)</li>
              <li>Go to <span className="font-mono text-yellow-400">Edit → Global Configuration → API → Settings</span></li>
              <li>Check <span className="text-green-400">"Enable ActiveX and Socket Clients"</span></li>
              <li>Set socket port to <span className="font-mono text-yellow-400">7497</span> (paper) or <span className="font-mono text-red-400">7496</span> (live)</li>
              <li>Add <span className="font-mono text-yellow-400">127.0.0.1</span> to trusted IPs</li>
              <li>Click the <span className="text-green-400">IBKR Execution</span> toggle above to connect</li>
            </ol>
            <div className="mt-4 p-4 bg-yellow-900/20 border border-yellow-800 rounded-lg text-yellow-300 text-sm">
              Always start with IBKR Paper Trading (port 7497) to verify everything works before using real money.
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
