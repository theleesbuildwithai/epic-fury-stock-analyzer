import { useState, useRef, useEffect, Component } from 'react'

// Error boundary to prevent crashes from resetting the whole page
class ChatErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  render() {
    if (this.state.hasError) {
      return <div className="text-red-400 text-sm p-3">Something went wrong rendering this message.</div>
    }
    return this.props.children
  }
}

// Safe markdown renderer - returns React elements, not HTML strings
function SafeMarkdown({ text }) {
  if (!text) return null
  const lines = text.split('\n')
  return (
    <div className="text-sm leading-relaxed space-y-1.5">
      {lines.map((line, i) => {
        if (!line && i > 0) return <div key={i} className="h-2" />
        if (line === '---') return <hr key={i} className="border-neutral-800 my-2" />
        // Bold text
        const parts = []
        let remaining = line
        let partKey = 0
        const boldRegex = /\*\*(.*?)\*\*/g
        let match
        let lastIndex = 0
        while ((match = boldRegex.exec(remaining)) !== null) {
          if (match.index > lastIndex) {
            parts.push(<span key={partKey++}>{remaining.slice(lastIndex, match.index)}</span>)
          }
          parts.push(<strong key={partKey++} className="text-white font-semibold">{match[1]}</strong>)
          lastIndex = match.index + match[0].length
        }
        if (lastIndex < remaining.length) {
          parts.push(<span key={partKey++}>{remaining.slice(lastIndex)}</span>)
        }
        if (parts.length === 0) parts.push(<span key={0}>{line}</span>)

        // Bullet points
        const trimmed = line.trimStart()
        if (trimmed.startsWith('- ') || trimmed.startsWith('• ')) {
          return <div key={i} className="ml-4 text-neutral-300">{parts}</div>
        }
        return <div key={i}>{parts}</div>
      })}
    </div>
  )
}

export default function AIAnalyst() {
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: "I'm your AI Stock Analyst — think of me as a senior quant at a hedge fund.\n\nAsk me anything:\n- \"Should I buy AAPL?\" — Buy/sell signals with reasoning\n- \"How long should I hold TSLA?\" — Hold duration\n- \"What's the price target for NVDA?\" — Forecasts\n- \"What are today's top picks?\" — Symbols to buy\n- \"Explain RSI\" — Trading education\n- \"How is the market?\" — Live sentiment\n\nI use live data from Yahoo Finance, CNN, and CNBC with EMA, RSI, MACD, and pivot point analysis.",
      ticker: null,
    }
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  const askQuestion = async (question) => {
    if (!question || loading) return
    const q = question.trim()
    if (!q) return

    setInput('')
    setLoading(true)
    setMessages(prev => [...prev, { role: 'user', content: q }])

    try {
      const res = await fetch('/api/ai-analyst?q=' + encodeURIComponent(q))
      if (!res.ok) throw new Error('Request failed')
      const data = await res.json()
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.answer || 'No response generated.',
        ticker: data.ticker || null,
        questionType: data.question_type || 'general',
        dataUsed: data.data_used || [],
      }])
    } catch (err) {
      console.error('AI Analyst error:', err)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: 'Sorry, I encountered an error. Please try again.',
        ticker: null,
      }])
    } finally {
      setLoading(false)
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    const q = input.trim()
    if (q && !loading) {
      askQuestion(q)
    }
  }

  const quickQuestions = [
    "What are today's top picks?",
    "How is the market doing?",
    "Should I buy NVDA?",
    "Explain RSI",
    "How long should I hold AAPL?",
    "What's the price target for TSLA?",
  ]

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex flex-col" style={{ height: 'calc(100vh - 120px)' }}>
      {/* Header */}
      <div className="mb-4">
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 bg-gradient-to-br from-green-500 to-emerald-600 rounded-xl flex items-center justify-center shadow-lg shadow-green-500/20">
            <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
          </div>
          <div>
            <h1 className="text-2xl font-bold text-white">AI Stock Analyst</h1>
            <p className="text-neutral-500 text-sm">Pro-level analysis powered by live market data</p>
          </div>
        </div>
      </div>

      {/* Quick questions - only show before any conversation */}
      {messages.length <= 1 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {quickQuestions.map((q, i) => (
            <button
              key={i}
              onClick={() => askQuestion(q)}
              disabled={loading}
              className="px-3 py-1.5 text-xs font-medium bg-neutral-900 border border-neutral-700 rounded-full
                         text-neutral-400 hover:text-white hover:border-neutral-500 transition-all
                         disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {q}
            </button>
          ))}
        </div>
      )}

      {/* Chat messages */}
      <div className="flex-1 overflow-y-auto space-y-4 mb-4 pr-1">
        {messages.map((msg, i) => (
          <div key={i} className={'flex ' + (msg.role === 'user' ? 'justify-end' : 'justify-start')}>
            <div className={'max-w-[85%] rounded-2xl px-5 py-3.5 ' + (
              msg.role === 'user'
                ? 'bg-white text-black rounded-br-sm'
                : 'bg-neutral-900 border border-neutral-800 text-neutral-200 rounded-bl-sm'
            )}>
              {msg.role === 'assistant' ? (
                <ChatErrorBoundary>
                  <SafeMarkdown text={msg.content} />
                  {msg.dataUsed && msg.dataUsed.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-neutral-800">
                      <span className="text-neutral-600 text-[10px] uppercase tracking-wider">
                        Data: {msg.dataUsed.join(' | ')}
                      </span>
                    </div>
                  )}
                </ChatErrorBoundary>
              ) : (
                <p className="text-sm">{msg.content}</p>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-neutral-900 border border-neutral-800 rounded-2xl rounded-bl-sm px-5 py-3.5">
              <div className="flex items-center gap-2">
                <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse"></div>
                <span className="text-neutral-400 text-sm">Analyzing with live market data...</span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input - using form for proper submit handling */}
      <form onSubmit={handleSubmit} className="bg-neutral-900 border border-neutral-700 rounded-2xl p-2 flex items-center gap-2">
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about any stock, strategy, or market concept..."
          className="flex-1 bg-transparent text-white text-sm px-3 py-2.5 focus:outline-none placeholder-neutral-500"
          disabled={loading}
          autoComplete="off"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="px-5 py-2.5 bg-white text-black font-semibold text-sm rounded-xl
                     hover:bg-neutral-200 disabled:bg-neutral-800 disabled:text-neutral-600 transition-all shrink-0"
        >
          {loading ? (
            <div className="w-4 h-4 border-2 border-neutral-600 border-t-neutral-400 rounded-full animate-spin"></div>
          ) : (
            'Ask'
          )}
        </button>
      </form>

      <p className="text-neutral-700 text-[10px] mt-2 text-center">
        Uses live Yahoo Finance data, CNN, CNBC news. Not financial advice. Always do your own research.
      </p>
    </div>
  )
}
