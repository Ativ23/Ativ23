"""
=============================================================
  CRYPTO TRADING BOT  —  Multi-Indicator Signal Engine
  Supports: Binance, Coinbase Advanced, Kraken, and more
  Strategy: RSI + MACD + Bollinger Bands + Volume filter
=============================================================
  ⚠️  RISK WARNING: Live trading with real money carries
  significant risk of loss. Start with paper trading.
  Never invest more than you can afford to lose.
=============================================================
"""

import ccxt
import pandas as pd
import numpy as np
import time
import json
import logging
import os
from datetime import datetime
from typing import Optional, Dict, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("bot_log.txt"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_config(path: str = "config.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file '{path}' not found. "
            "Copy config.example.json to config.json and fill in your API keys."
        )
    with open(path) as f:
        return json.load(f)


def connect_exchange(cfg: dict) -> ccxt.Exchange:
    exchange_id = cfg["exchange"].lower()
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        "apiKey": cfg["api_key"],
        "secret": cfg["api_secret"],
        "enableRateLimit": True,
        "options": {"defaultType": cfg.get("market_type", "spot")},
    })
    if cfg.get("paper_trading", False):
        exchange.set_sandbox_mode(True)
        log.info("PAPER TRADING MODE - no real orders will be placed")
    else:
        log.warning("LIVE TRADING MODE - real money is at risk")
    exchange.load_markets()
    log.info(f"Connected to {exchange_id.upper()}")
    return exchange


def fetch_ohlcv(exchange, symbol, timeframe="1h", limit=200):
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def compute_bollinger(series, period=20, std_dev=2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    return mid + std_dev*std, mid, mid - std_dev*std


def add_indicators(df):
    df = df.copy()
    df["rsi"] = compute_rsi(df["close"])
    df["macd"], df["macd_signal"], df["macd_hist"] = compute_macd(df["close"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = compute_bollinger(df["close"])
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema_200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"]
    return df


def generate_signal(df, cfg):
    if len(df) < 2:
        return "HOLD"
    last, prev = df.iloc[-1], df.iloc[-2]
    rsi_oversold = cfg.get("rsi_oversold", 35)
    rsi_overbought = cfg.get("rsi_overbought", 70)
    if (prev["macd_hist"] < 0 and last["macd_hist"] >= 0
            and last["rsi"] < rsi_oversold + 10
            and last["bb_pct"] < 0.35
            and last["close"] > last["ema_50"]
            and last["vol_ratio"] > 1.1):
        return "BUY"
    if last["rsi"] > rsi_overbought or (prev["macd_hist"] > 0 and last["macd_hist"] <= 0) or last["bb_pct"] > 0.80:
        return "SELL"
    return "HOLD"


class RiskManager:
    def __init__(self, cfg):
        self.max_position_pct = cfg.get("max_position_pct", 0.95)
        self.stop_loss_pct = cfg.get("stop_loss_pct", 0.02)
        self.take_profit_pct = cfg.get("take_profit_pct", 0.04)
        self.max_daily_loss_pct = cfg.get("max_daily_loss_pct", 0.05)
        self.daily_start_balance = None
        self.daily_date = None

    def check_daily_loss_limit(self, balance):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_date != today:
            self.daily_date = today
            self.daily_start_balance = balance
        if self.daily_start_balance and self.daily_start_balance > 0:
            if (balance - self.daily_start_balance) / self.daily_start_balance < -self.max_daily_loss_pct:
                log.warning("Daily loss limit hit. Halting.")
                return False
        return True

    def position_size(self, balance, price):
        return (balance * self.max_position_pct) / price

    def stop_loss_price(self, entry):
        return entry * (1 - self.stop_loss_pct)

    def take_profit_price(self, entry):
        return entry * (1 + self.take_profit_pct)


class OrderManager:
    def __init__(self, exchange, symbol, paper=False):
        self.exchange = exchange
        self.symbol = symbol
        self.paper = paper
        self.position = None

    def get_balance(self, currency="USDT"):
        return float(self.exchange.fetch_balance()["free"].get(currency, 0))

    def has_open_position(self):
        return self.position is not None

    def open_long(self, price, qty, stop_loss, take_profit):
        log.info(f"BUY {qty:.6f} {self.symbol} @ ${price:.4f}  SL=${stop_loss:.4f}  TP=${take_profit:.4f}")
        if not self.paper:
            order = self.exchange.create_market_buy_order(self.symbol, qty)
            price = float(order.get("average", price))
        self.position = {"entry_price": price, "qty": qty, "stop_loss": stop_loss, "take_profit": take_profit, "opened_at": datetime.now().isoformat()}

    def close_long(self, price, reason="signal"):
        if not self.position:
            return
        pnl = (price - self.position["entry_price"]) * self.position["qty"]
        log.info(f"SELL {self.position['qty']:.6f} {self.symbol} @ ${price:.4f}  Reason: {reason}  PnL: ${pnl:.4f}")
        if not self.paper:
            self.exchange.create_market_sell_order(self.symbol, self.position["qty"])
        self.position = None

    def check_exit_conditions(self, price):
        if not self.position:
            return None
        if price <= self.position["stop_loss"]:
            return "stop_loss"
        if price >= self.position["take_profit"]:
            return "take_profit"
        return None


class PerformanceTracker:
    def __init__(self):
        self.trades = []

    def record_trade(self, entry, exit_price, qty, reason):
        self.trades.append({"time": datetime.now().isoformat(), "entry": entry, "exit": exit_price, "qty": qty, "pnl": (exit_price-entry)*qty, "reason": reason})

    def print_summary(self, balance):
        if not self.trades:
            return
        wins = [t for t in self.trades if t["pnl"] > 0]
        log.info(f"Balance: ${balance:.2f} | Trades: {len(self.trades)} | Win Rate: {len(wins)/len(self.trades)*100:.1f}% | PnL: ${sum(t['pnl'] for t in self.trades):.4f}")


def run_bot():
    cfg = load_config()
    exchange = connect_exchange(cfg)
    symbol = cfg.get("symbol", "BTC/USDT")
    timeframe = cfg.get("timeframe", "1h")
    poll_interval = cfg.get("poll_interval_seconds", 60)
    quote_currency = symbol.split("/")[1]
    risk = RiskManager(cfg)
    orders = OrderManager(exchange, symbol, paper=cfg.get("paper_trading", True))
    tracker = PerformanceTracker()
    log.info(f"Bot started | {symbol} | {timeframe}")
    cycle = 0
    while True:
        try:
            cycle += 1
            balance = orders.get_balance(quote_currency)
            if not risk.check_daily_loss_limit(balance):
                time.sleep(3600)
                continue
            df = add_indicators(fetch_ohlcv(exchange, symbol, timeframe))
            price = float(df["close"].iloc[-1])
            log.info(f"[Cycle {cycle}] {symbol} ${price:.4f} RSI={df['rsi'].iloc[-1]:.1f} Balance=${balance:.2f}")
            if orders.has_open_position():
                reason = orders.check_exit_conditions(price)
                if reason:
                    entry = orders.position["entry_price"]; qty = orders.position["qty"]
                    orders.close_long(price, reason); tracker.record_trade(entry, price, qty, reason)
                    tracker.print_summary(balance); time.sleep(poll_interval); continue
                if generate_signal(df, cfg) == "SELL":
                    entry = orders.position["entry_price"]; qty = orders.position["qty"]
                    orders.close_long(price, "signal_sell"); tracker.record_trade(entry, price, qty, "signal_sell")
                    tracker.print_summary(balance)
            elif balance > 10 and generate_signal(df, cfg) == "BUY":
                orders.open_long(price, risk.position_size(balance, price), risk.stop_loss_price(price), risk.take_profit_price(price))
            time.sleep(poll_interval)
        except ccxt.NetworkError as e:
            log.error(f"Network error: {e}"); time.sleep(30)
        except ccxt.ExchangeError as e:
            log.error(f"Exchange error: {e}"); time.sleep(60)
        except KeyboardInterrupt:
            log.info("Bot stopped."); tracker.print_summary(orders.get_balance(quote_currency)); break
        except Exception as e:
            log.exception(f"Error: {e}"); time.sleep(30)


if __name__ == "__main__":
    run_bot()
