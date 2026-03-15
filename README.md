# Epic Fury Stock Analyzer

Real-time stock analysis with technical indicators, trend detection, and performance tracking.

## Features
- Enter any stock ticker to get instant analysis
- Technical indicators: RSI, MACD, Bollinger Bands, Moving Averages
- Risk score and buy/sell signals
- Track prediction accuracy vs S&P 500, Nasdaq, Dow Jones
- All data from Yahoo Finance (nothing made up)

## Run Locally

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8080 --reload
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

## Deploy
Push to main triggers CodeBuild which builds a Docker image and deploys to App Runner.

Built by the Epic Fury team.
