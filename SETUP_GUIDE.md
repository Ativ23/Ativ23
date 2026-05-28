# Crypto Trading Bot - Setup Guide

## Step 1 - Install Dependencies
pip install ccxt pandas numpy

## Step 2 - Configure
Copy config.example.json to config.json and fill in your API keys.
Keep paper_trading set to true to start.

## Step 3 - Run
python trading_bot.py

## Settings
- symbol: BTC/USDT (trading pair)
- timeframe: 1h (candle size)
- stop_loss_pct: 0.02 (2% stop loss)
- take_profit_pct: 0.04 (4% take profit)
- max_daily_loss_pct: 0.05 (halt if down 5% today)

## Security Checklist
- API key has NO withdrawal permissions
- API key is IP-restricted to your machine
- config.json is NOT committed to git
- Tested in paper trading mode first
