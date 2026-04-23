# Kalshi 15-min Crypto Market-Making Bot

## Files
- `engine.py`          — Market discovery, WebSocket feed, orderbook, arb detection
- `trader.py`          — Order execution, quote manager, position tracking
- `test_kalshi_bot.py` — 52-test suite (52/52 passing)
- `.env.example`       — Config template

## Install
```bash
pip install requests websocket-client cryptography python-dotenv
```

## Setup
1. Go to https://kalshi.com/account/profile → API Keys → Create New API Key
2. Save the downloaded `.key` file as `kalshi.key` in this folder
3. Copy `.env.example` to `.env` and fill in your `KALSHI_KEY_ID`
4. Start on DEMO with `KALSHI_DEMO=true` and `DRY_RUN=true`

## Run tests
```bash
python -m pytest test_kalshi_bot.py -v
# or without pytest:
python -m unittest test_kalshi_bot -v
```

## Usage (integrate into your app)

```python
from engine import BotEngine
from trader import QuoteManager, execute_arb

quote_managers = {a: QuoteManager(a) for a in ["BTC", "ETH", "SOL"]}

def on_prices(markets, snapshots):
    for asset, snap_dict in snapshots.items():
        if snap_dict:
            mkt = markets.get(asset)
            snap = ...  # your snapshot object
            quote_managers[asset].update(snap, mkt)

def on_arb(snap):
    # Fires when taker-profitable gap detected
    mkt = bot.markets[snap.asset]
    execute_arb(snap, mkt, bot)

def on_log(icon, msg):
    print(f"{icon} {msg}")

bot = BotEngine(
    on_log=on_log,
    on_prices=on_prices,
    on_arb=on_arb,
    on_status=lambda s: print(f"Status: {s}"),
)
bot.start()
```

## Key design decisions vs Polymarket bot

| | Polymarket | Kalshi |
|---|---|---|
| Price format | Float 0–1 | Integer cents 1–99 |
| Orderbook | YES asks + NO asks | YES bids + NO bids only |
| Implied ask | Direct from book | `100 - best_opposite_bid` |
| Auth | Wallet private key | RSA-PSS signed headers |
| WS keepalive | Manual text "PING" | Standard WS ping frames |
| Fee formula | `0.25 × p × (1-p)` | `0.07 × p × (1-p)` taker |
| Maker fee | Same as taker | `0.0175 × p × (1-p)` (4× cheaper) |
| Primary strategy | FOK arb taker orders | Resting maker quotes |
| Window length | 5 minutes | 15 minutes |

## Fee reference
- Taker: `0.07 × P × (1-P)` per contract — max **1.75¢** at P=0.50
- Maker: `0.0175 × P × (1-P)` per contract — max **0.4375¢** at P=0.50
- Total rounded UP to nearest cent on the full order
- No fee to cancel a resting order
