# Plan: New Quant Features for Week 2 Profit Boost

## What's Already Implemented (16 factors + overlays)
- Momentum, Value, Quality, Low Vol, RSI(2), Volume, Smart Money, Relative Strength
- BB Squeeze, VWAP, Hurst, Autocorrelation, Stat Arb Z-Score, Kurtosis, Vol Compression, MTF Alignment
- Market regime, macro overlay, overnight intelligence, cross-asset signals, geopolitical risk
- Kelly sizing, ATR stops, trailing profit, win-lock, drawdown protection

## New Features to Add (6 high-impact, all use yfinance data)

### 1. POST-EARNINGS DRIFT (Factor #17)
**Why**: One of the most documented anomalies in finance. Stocks that beat earnings keep drifting up for 60 days. Stocks that miss keep falling. Academic alpha: 2-4% per quarter.
- Check if stock reported earnings in last 10 days
- Compare current price vs pre-earnings price
- Beat (gap up >3%): +3.0 score boost (drift continues)
- Miss (gap down >3%): -3.0 score boost (drift continues)
- Use yfinance earnings_dates + price history

### 2. OPTIONS FLOW SENTIMENT (Factor #18)
**Why**: Put/call ratio reveals what institutional money is betting. Extreme put buying = fear (contrarian buy). Extreme call buying = greed (contrarian caution).
- Pull options chain from yfinance for top picks
- Calculate put/call volume ratio
- Ratio > 1.5: Extreme fear → contrarian buy signal (+2.0)
- Ratio < 0.5: Extreme greed → caution signal (-1.0)
- IV Rank: current IV vs 52-week range → identifies cheap/expensive options

### 3. SECTOR ROTATION ENGINE
**Why**: Professional hedge funds rotate into strongest sectors and out of weakest. 1-month sector momentum predicts next month's returns.
- Track 20-day returns for each sector ETF (XLK, XLF, XLV, XLE, etc.)
- Top 3 sectors: +1.5 confidence boost to stocks in those sectors
- Bottom 3 sectors: -1.5 confidence penalty
- Rotation signal changes weekly (not daily — avoids whipsaw)

### 4. VOLUME PROFILE / VPOC (Point of Control)
**Why**: Identifies key support/resistance levels where most trading occurred. Stocks near VPOC tend to bounce; breakouts above VPOC run hard.
- Calculate 20-day volume-weighted price distribution
- VPOC = price level with highest volume
- Price near VPOC (within 1%): strong support → +1.5 for longs
- Price breaking above VPOC after being below: breakout signal +2.0
- Price falling below VPOC: breakdown signal -2.0

### 5. ICHIMOKU CLOUD SIGNALS
**Why**: 5-component Japanese trend system used by institutional traders worldwide. Captures trend, momentum, support/resistance in one indicator.
- Tenkan (9-period high-low midpoint), Kijun (26-period)
- Senkou A & B (cloud boundaries)
- Price above cloud + Tenkan > Kijun: strong bullish (+2.0)
- Price below cloud + Tenkan < Kijun: strong bearish (-2.0)
- Price inside cloud: neutral/choppy (0)
- Cloud twist (Senkou A crosses B): trend change signal

### 6. DYNAMIC HEDGING ENGINE
**Why**: Automatically reduces portfolio risk during dangerous markets by scaling exposure based on multiple risk signals.
- Composite risk score from: VIX level + VIX term structure + breadth + regime
- LOW risk: 100% exposure (normal trading)
- MODERATE: 80% exposure (reduce new positions)
- HIGH: 60% exposure + tighten all stops by 20%
- EXTREME: 40% exposure + close weakest positions + no new longs
- Displayed on dashboard as "Risk Shield" status

## Frontend Changes
- Add new factors to the Quant Picks display (earnings drift, options flow, Ichimoku)
- Add Sector Rotation heatmap widget
- Add Risk Shield status indicator
- Add Volume Profile level to stock cards

## Implementation Order
1. Post-earnings drift + options flow (backend, ~200 lines)
2. Sector rotation engine (backend, ~100 lines)
3. Volume profile + Ichimoku (backend, ~150 lines)
4. Dynamic hedging engine (backend, ~100 lines)
5. Frontend display updates (~100 lines)
6. Test locally, commit, deploy

## Risk: Yahoo Finance API Rate Limits
- Options chains are heavier API calls — only fetch for top 20 picks (not all 260)
- Cache sector rotation scores (update every 4 hours, not every request)
- Ichimoku uses existing price data (no new API calls)
