"""
engine.py — Kalshi 15-min Crypto Market-Making Bot

PLATFORM DIFFERENCES FROM POLYMARKET
──────────────────────────────────────
• Auth       : RSA-PSS signed headers on every request (no wallet/proxy)
• Prices     : Integer cents (1–99), NOT floats. YES bid + NO bid <= 100.
• Orderbook  : Returns YES bids + NO bids only (no asks). Implied asks:
                 YES ask = 100 - best_NO_bid   (in cents)
                 NO  ask = 100 - best_YES_bid
• WS URL     : wss://api.kalshi.com/trade-api/ws/v2
• WS Auth    : Same RSA signed headers during WebSocket handshake
• WS msgs    : {"type": "orderbook_snapshot"|"orderbook_delta"|"ticker", "msg": {...}}
• Delta side : "yes" or "no" (not BUY/SELL)
• Delta field: "delta_fp" (signed float — add to level, remove if <= 0)

FEE STRUCTURE (confirmed from official docs + academic papers, April 2025+)
──────────────────────────────────────────────────────────────────────────────
  Taker fee per contract = 0.07  × P × (1 − P)   [P in dollars, e.g. 0.50]
  Maker fee per contract = 0.0175 × P × (1 − P)   [4× cheaper than taker]
  Max taker fee = 1.75¢ at P=0.50
  Max maker fee = 0.44¢ at P=0.50
  Both fees rounded UP to nearest cent on the TOTAL order.
  No fee to cancel a resting order.

STRATEGY: Market Making
───────────────────────
Post resting limit orders on BOTH YES and NO sides.
Collect the spread when others trade against us (we're the maker).
Skew quotes toward fair value using cross-market signal (Polymarket prices).
Cancel/replace quotes when:
  - Our position exceeds MAX_POSITION_CONTRACTS per side
  - Price moves > REQUOTE_THRESHOLD cents from our quotes
  - Window expires (new 15-min market opens)

MARKET TICKERS
───────────────
Kalshi 15-min crypto series: KXBTC15M, KXETH15M, KXSOL15M
where {ts} is the Unix timestamp of the window start (multiple of 900).
"""

import os
import json
import time
import base64
import hashlib
import threading
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, Callable

import requests
from requests.adapters import HTTPAdapter
import websocket

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("kalshi.engine")

# ── Config ────────────────────────────────────────────────────────────────────
# Kalshi API URLs
# REST: api.elections.kalshi.com serves ALL markets (not just elections) — confirmed in docs.
# WS:   same host, /trade-api/ws/v2
# Demo: demo-api.kalshi.co — separate credentials, used only for order execution testing.
#
# IMPORTANT: ticker WS channel REQUIRES authentication (confirmed from docs March 2026).
# There are no truly public WS channels — you must have valid credentials to connect.
PROD_API_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_API_BASE   = "https://demo-api.kalshi.co/trade-api/v2"
PROD_WS_URL     = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL     = "wss://demo-api.kalshi.co/trade-api/ws/v2"

USE_DEMO        = os.getenv("KALSHI_DEMO", "true").lower() != "false"
DRY_RUN         = os.getenv("DRY_RUN", "true").lower() != "false"

# Market discovery always hits production (kalshi_get_public uses PROD_API_BASE directly).
# Authenticated order/portfolio calls + WS follow the demo/live setting.
API_BASE        = os.getenv("KALSHI_API_BASE", DEMO_API_BASE if USE_DEMO else PROD_API_BASE)
WS_URL          = os.getenv("KALSHI_WS_URL",   DEMO_WS_URL   if USE_DEMO else PROD_WS_URL)

# When KALSHI_DEMO=true the bot looks for KALSHI_DEMO_KEY_ID / KALSHI_DEMO_KEY_FILE first,
# then falls back to the plain KALSHI_KEY_ID / KALSHI_KEY_FILE if demo-specific ones aren't set.
# This lets you store both credential pairs in .env and switch modes with one flag.
if USE_DEMO:
    KALSHI_KEY_ID   = os.getenv("KALSHI_DEMO_KEY_ID",   os.getenv("KALSHI_KEY_ID",   ""))
    KALSHI_KEY_FILE = os.getenv("KALSHI_DEMO_KEY_FILE",  os.getenv("KALSHI_KEY_FILE",  "kalshi_demo.key"))
else:
    KALSHI_KEY_ID   = os.getenv("KALSHI_KEY_ID",   "")
    KALSHI_KEY_FILE = os.getenv("KALSHI_KEY_FILE",  "kalshi.key")

TRADE_SIZE_CONTRACTS = int(os.getenv("TRADE_SIZE_CONTRACTS", "5"))   # contracts per order leg
MAX_POSITION_CONTRACTS = int(os.getenv("MAX_POSITION_CONTRACTS", "20"))  # max open per side
SPREAD_TARGET_CENTS  = int(os.getenv("SPREAD_TARGET_CENTS", "3"))    # target bid/ask spread in cents
REQUOTE_THRESHOLD    = int(os.getenv("REQUOTE_THRESHOLD", "2"))       # reprice if fair value moves > N cents
MIN_GAP_CENTS   = float(os.getenv("MIN_GAP_CENTS", "0.0"))            # min taker gap (¢) to fire on_arb; 0 = fire on any profitable gap
MIN_PRICE_CENTS = int(os.getenv("MIN_PRICE_CENTS", "10"))             # ask below this → order book too thin to fill
MAX_PRICE_CENTS = int(os.getenv("MAX_PRICE_CENTS", "90"))             # ask above this → order book too thin to fill
# How many extra cents we're willing to pay per leg to sweep deeper into the book.
# Bidding yes_ask+SWEEP sweeps NO bids across SWEEP extra price levels, greatly
# improving fill probability when the top level is only 1–2 contracts deep.
# The cost (2×SWEEP cents per pair) comes out of the taker_gap, so we only apply
# as much sweep as the gap can absorb while keeping ≥ MIN_PROFIT_AFTER_SWEEP¢ net.
SWEEP_CENTS          = int(os.getenv("SWEEP_CENTS",          "3"))    # max per-leg sweep budget
MIN_PROFIT_AFTER_SWEEP = float(os.getenv("MIN_PROFIT_AFTER_SWEEP", "0.5"))  # minimum net cents after sweeping
LOG_FILE        = Path(os.getenv("LOG_FILE", "kalshi_log.jsonl"))
MONITOR_LOG_FILE = Path(os.getenv("MONITOR_LOG_FILE", "monitor_log.jsonl"))  # logs ALL raw gaps for monitoring
REQUEST_TIMEOUT = 8

ASSETS      = ["BTC", "ETH", "SOL"]
# Series tickers can be overridden in .env if Kalshi renames them.
# Defaults verified March 2026. To override: SERIES_BTC=KXBTCD-15M etc.
SERIES_MAP  = {
    "BTC": os.getenv("SERIES_BTC", "KXBTC15M"),
    "ETH": os.getenv("SERIES_ETH", "KXETH15M"),
    "SOL": os.getenv("SERIES_SOL", "KXSOL15M"),
}
WINDOW_SECS = 900   # 15-minute windows

# ── Fee formulas (official Kalshi, confirmed April 2025+) ─────────────────────
# Taker: 0.07 × P × (1-P)  per contract, rounded UP to nearest cent on total
# Maker: 0.0175 × P × (1-P) per contract, rounded UP to nearest cent on total
TAKER_COEF = Decimal("0.07")
MAKER_COEF = Decimal("0.0175")

def taker_fee_per_contract(price_cents: int) -> Decimal:
    """Taker fee per contract. price_cents: integer 1-99."""
    p = Decimal(price_cents) / 100
    return TAKER_COEF * p * (1 - p)

def maker_fee_per_contract(price_cents: int) -> Decimal:
    """Maker fee per contract. price_cents: integer 1-99."""
    p = Decimal(price_cents) / 100
    return MAKER_COEF * p * (1 - p)

def total_taker_fee(price_cents: int, n_contracts: int) -> Decimal:
    """Total taker fee rounded UP to nearest cent."""
    raw = taker_fee_per_contract(price_cents) * n_contracts
    return raw.quantize(Decimal("0.01"), rounding=ROUND_UP)

def total_maker_fee(price_cents: int, n_contracts: int) -> Decimal:
    """Total maker fee rounded UP to nearest cent."""
    raw = maker_fee_per_contract(price_cents) * n_contracts
    return raw.quantize(Decimal("0.01"), rounding=ROUND_UP)

def net_cost_cents(yes_price: int, no_price: int, n: int, maker: bool = True) -> Decimal:
    """
    Total cost in dollars to buy N YES + N NO contracts.
    Both sides are maker orders (resting limit) → lower fees.
    Returns net cost as Decimal dollars.
    """
    yes_cost = Decimal(yes_price) * n / 100
    no_cost  = Decimal(no_price)  * n / 100
    if maker:
        yes_fee = total_maker_fee(yes_price, n)
        no_fee  = total_maker_fee(no_price, n)
    else:
        yes_fee = total_taker_fee(yes_price, n)
        no_fee  = total_taker_fee(no_price, n)
    return yes_cost + no_cost + yes_fee + no_fee

def arb_gap_cents(yes_ask: int, no_ask: int, n: int, maker: bool = True) -> Decimal:
    """
    Gap in cents per contract pair after fees.
    Positive = profit per pair.
    yes_ask, no_ask: implied ask prices in cents.
    Implied YES ask = 100 - best_NO_bid
    """
    total = net_cost_cents(yes_ask, no_ask, n, maker)
    payout = Decimal(n)   # each pair pays out $1.00 = n dollars
    return (payout - total) * 100 / n   # cents per contract pair

# ── Auth ──────────────────────────────────────────────────────────────────────
_private_key = None

def _load_key() -> Optional[object]:
    global _private_key
    if _private_key:
        return _private_key
    if not _CRYPTO_AVAILABLE:
        log.error("cryptography package not installed — run: pip install cryptography")
        return None
    key_path = Path(KALSHI_KEY_FILE)
    if not key_path.exists():
        log.error("Kalshi key file not found: %s", key_path)
        return None
    try:
        with open(key_path, "rb") as f:
            _private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        log.info("Loaded RSA key from %s", key_path)
        return _private_key
    except Exception as e:
        log.error("Failed to load RSA key: %s", e)
        return None

def _sign(timestamp_ms: int, method: str, path: str) -> str:
    """
    Sign: str(timestamp_ms) + METHOD + path_without_query
    Returns base64-encoded PSS signature.
    """
    key = _load_key()
    if key is None:
        return ""
    # Strip query params before signing (per Kalshi docs)
    path_no_query = path.split("?")[0]
    msg = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
    sig = key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")

def _auth_headers(method: str, path: str) -> dict:
    """Return signed auth headers for a REST request."""
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": _sign(ts, method, path),
        "Content-Type":            "application/json",
    }

def _ws_auth_headers() -> list:
    """
    Auth headers for WebSocket handshake.
    websocket-client requires headers as a LIST of "Key: Value" strings.
    Timestamp generated here (fresh) — must be called immediately before
    opening the connection, not before, to avoid stale timestamp 401s.
    Returns empty list if no credentials configured (for public-only channels).
    """
    if not KALSHI_KEY_ID:
        return []
    path = "/trade-api/ws/v2"
    ts   = int(time.time() * 1000)
    sig  = _sign(ts, "GET", path)
    if not sig:
        return []
    return [
        f"KALSHI-ACCESS-KEY: {KALSHI_KEY_ID}",
        f"KALSHI-ACCESS-TIMESTAMP: {ts}",
        f"KALSHI-ACCESS-SIGNATURE: {sig}",
    ]

# ── HTTP helpers ──────────────────────────────────────────────────────────────
# Persistent session: TCP+TLS connection is reused across requests, eliminating
# the ~50-150 ms handshake cost on every order.
SESSION = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=2,   # one pool per unique host (prod + demo)
    pool_maxsize=4,       # up to 4 concurrent keep-alive connections per host
    max_retries=0,        # no automatic retries — order logic handles failures explicitly
)
SESSION.mount("https://", _adapter)
SESSION.headers.update({"Connection": "keep-alive"})

ORDER_TIMEOUT = 5   # seconds — tight enough for latency-sensitive arb, not so tight we drop valid responses

def kalshi_get(path: str, params: dict = None) -> dict:
    """Authenticated GET. path is relative to API_BASE."""
    full_path = f"/trade-api/v2{path}"
    headers   = _auth_headers("GET", full_path)
    url       = f"{API_BASE}{path}"
    r = SESSION.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def kalshi_get_public(path: str, params: dict = None) -> dict:
    """GET for public market data — uses production API (elections.kalshi.com hosts all markets)."""
    r = SESSION.get(f"{PROD_API_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def pre_warm_connection():
    """
    Open and cache the TCP+TLS connection to the trading API host before any arb
    fires.  Without this, the first order pays the full handshake cost (~50-150 ms).
    A cheap authenticated GET is enough to establish the connection in the pool.
    """
    try:
        kalshi_get("/portfolio/balance")
        log.info("HTTP connection pre-warmed to %s", API_BASE)
    except Exception as e:
        log.warning("pre_warm_connection failed (non-fatal): %s", e)

# ── Market discovery ──────────────────────────────────────────────────────────
def _current_window_ts() -> int:
    return (int(time.time()) // WINDOW_SECS) * WINDOW_SECS

def _market_ticker(asset: str, ts: int) -> str:
    """Legacy helper — kept for tests. Discovery now uses series_ticker query."""
    return f"{SERIES_MAP[asset]}-{ts}"

def _pick_best_market(mkts: list) -> Optional[tuple]:
    """Return (best_market, best_close_time) — the non-expired market closing soonest."""
    now = datetime.now(timezone.utc)
    best = None
    best_ct = None
    for mkt in mkts:
        close_time = mkt.get("close_time", "")
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now:
            continue
        if best_ct is None or ct < best_ct:
            best    = mkt
            best_ct = ct
    return (best, best_ct) if best else (None, None)


# Per-asset timestamp of when the next market window opens (set by discover_market).
# Expiry loop reads this to avoid polling every 30s when markets are hours away.
_next_market_opens: dict = {a: 0.0 for a in ["BTC", "ETH", "SOL"]}


def discover_market(asset: str, on_log: Callable) -> Optional[dict]:
    """
    Find the currently-open 15-min market for an asset.
    Returns None (and writes _next_market_opens[asset]) when the series exists
    but all markets are 'initialized' (upcoming) rather than 'open'.
    """
    series = SERIES_MAP[asset]
    try:
        data = kalshi_get_public("/markets", params={
            "series_ticker": series,
            "limit":         10,
        })
        mkts = data.get("markets", [])

        if not mkts:
            on_log("✗", (
                f"{asset}: no markets found for series {series} — "
                f"series may have been renamed. "
                f"Check kalshi.com/markets and set SERIES_{asset}=<new_ticker> in .env"
            ))
            return None

        now = datetime.now(timezone.utc)
        open_mkts = [m for m in mkts if m.get("status") == "open"]
        init_mkts = [m for m in mkts if m.get("status") == "initialized"]

        if not open_mkts:
            if init_mkts:
                def _open_ts(m):
                    try:
                        return datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
                    except Exception:
                        return datetime.max.replace(tzinfo=timezone.utc)
                next_mkt  = min(init_mkts, key=_open_ts)
                next_open = _open_ts(next_mkt)
                _next_market_opens[asset] = next_open.timestamp()
                wait_mins = max(0, int((next_open - now).total_seconds() // 60))
                wait_hrs  = wait_mins // 60
                wait_rem  = wait_mins % 60
                wait_str  = (f"{wait_hrs}h {wait_rem}m" if wait_hrs else f"{wait_mins}m")
                on_log("~", (
                    f"{asset}: market closed — next window opens in {wait_str} "
                    f"at {next_open.strftime('%H:%M UTC')} ({next_mkt['ticker']})"
                ))
            else:
                on_log("✗", (
                    f"{asset}: no open or upcoming markets in {series} "
                    f"(statuses: {sorted({m.get('status','?') for m in mkts})})"
                ))
            return None

        _next_market_opens[asset] = 0.0   # clear — market is open
        best, best_ct = _pick_best_market(open_mkts)
        if best is None:
            on_log("✗", f"{asset}: open markets in {series} are already past close_time")
            return None

        ticker     = best["ticker"]
        close_time = best.get("close_time", "")
        secs_left  = max(0, int((best_ct - now).total_seconds()))
        yes_bid    = _parse_price(best.get("yes_bid_dollars"))
        no_bid     = _parse_price(best.get("no_bid_dollars"))

        result = {
            "asset":      asset,
            "ticker":     ticker,
            "series":     series,
            "close_time": close_time,
            "open_time":  best.get("open_time", ""),
            "secs_left":  secs_left,
            "yes_bid":    yes_bid,
            "no_bid":     no_bid,
            "yes_ask":    (100 - no_bid)  if no_bid  is not None else None,
            "no_ask":     (100 - yes_bid) if yes_bid is not None else None,
            "tick_size":  best.get("tick_size", 1),
            "window_ts":  int(best_ct.timestamp()) - WINDOW_SECS,
        }
        on_log("✓", f"{asset} → {ticker}  ({secs_left}s left)  YES_bid={yes_bid}c  NO_bid={no_bid}c")
        return result

    except requests.HTTPError as e:
        on_log("!", f"{asset}: HTTP {e.response.status_code} querying series {series}: {e}")
    except Exception as e:
        on_log("!", f"{asset}: discover error: {e}")

    on_log("✗", f"{asset}: no active 15-min market found")
    return None

def _parse_price(val) -> Optional[int]:
    """Parse a price field (dollars string like '0.5600') to integer cents."""
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None

def discover_all(on_log: Callable) -> dict:
    on_log("→", "Discovering 15-min Kalshi markets…")
    return {a: discover_market(a, on_log) for a in ASSETS}

# ── Local orderbook ───────────────────────────────────────────────────────────
class LocalBook:
    """
    Maintains YES bids + NO bids in cents (integer keys).
    Kalshi orderbook only has bids. Implied asks:
      YES ask = 100 - best_NO_bid
      NO  ask = 100 - best_YES_bid
    delta_fp is a SIGNED quantity — positive = add, negative = reduce.
    """
    MAX_LEVELS = 200

    def __init__(self, ticker: str):
        self.ticker  = ticker
        self.yes_bids: dict = {}   # price_cents → count (float)
        self.no_bids:  dict = {}
        self.ready   = False
        self.seq     = -1

    def apply_snapshot(self, msg: dict):
        ob = msg.get("orderbook_fp") or msg
        self.yes_bids = {}
        self.no_bids  = {}
        for price_str, count_str in (ob.get("yes_dollars") or []):
            p = _cents(price_str)
            if p and 1 <= p <= 99:
                self.yes_bids[p] = float(count_str)
        for price_str, count_str in (ob.get("no_dollars") or []):
            p = _cents(price_str)
            if p and 1 <= p <= 99:
                self.no_bids[p] = float(count_str)
        self.ready = True

    def apply_delta(self, msg: dict):
        """
        WS delta: {"market_ticker":..., "price_dollars":"0.960",
                   "delta_fp":"-54.00", "side":"yes", "ts":...}
        """
        price_str = msg.get("price_dollars")
        delta_str = msg.get("delta_fp", "0")
        side      = msg.get("side", "").lower()
        p = _cents(price_str)
        if p is None or not (1 <= p <= 99):
            return
        delta = float(delta_str)
        book  = self.yes_bids if side == "yes" else self.no_bids
        cur   = book.get(p, 0.0)
        new   = cur + delta
        if new <= 0:
            book.pop(p, None)
        else:
            if p not in book and len(book) >= self.MAX_LEVELS:
                return   # cap memory
            book[p] = new

    def depth_for_yes_buy(self, yes_price: int) -> int:
        """
        How many contracts can a BUY YES IOC at yes_price fill?
        Buying YES at yes_price crosses with NO bids at >= (100 - yes_price).
        Returns cumulative NO bid depth across all qualifying levels.
        """
        min_no_price = 100 - yes_price
        return int(sum(v for p, v in self.no_bids.items() if p >= min_no_price))

    def depth_for_no_buy(self, no_price: int) -> int:
        """
        How many contracts can a BUY NO IOC at no_price fill?
        Buying NO at no_price crosses with YES bids at >= (100 - no_price).
        """
        min_yes_price = 100 - no_price
        return int(sum(v for p, v in self.yes_bids.items() if p >= min_yes_price))

    def best_yes_bid(self) -> Optional[int]:
        return max(self.yes_bids.keys()) if self.yes_bids else None

    def best_no_bid(self) -> Optional[int]:
        return max(self.no_bids.keys()) if self.no_bids else None

    def best_yes_ask(self) -> Optional[int]:
        """Implied YES ask = 100 - best_NO_bid."""
        no_bid = self.best_no_bid()
        return (100 - no_bid) if no_bid is not None else None

    def best_no_ask(self) -> Optional[int]:
        """Implied NO ask = 100 - best_YES_bid."""
        yes_bid = self.best_yes_bid()
        return (100 - yes_bid) if yes_bid is not None else None

    def spread_cents(self) -> Optional[int]:
        """YES ask - YES bid (the raw spread)."""
        ya = self.best_yes_ask()
        yb = self.best_yes_bid()
        if ya is None or yb is None:
            return None
        return ya - yb

    def mid_cents(self) -> Optional[float]:
        yb = self.best_yes_bid()
        ya = self.best_yes_ask()
        if yb is None or ya is None:
            return None
        return (yb + ya) / 2.0

def _cents(val) -> Optional[int]:
    """Convert dollar string '0.4800' to integer cents 48."""
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None

# ── Market snapshot ───────────────────────────────────────────────────────────
class MarketSnapshot:
    """Current state of one market including derived quote targets."""
    def __init__(self, asset: str, ticker: str, book: LocalBook,
                 window_ts: int, secs_left: int):
        self.asset      = asset
        self.ticker     = ticker
        self.window_ts  = window_ts
        self.secs_left  = secs_left
        self.ts         = datetime.now(timezone.utc)

        self.yes_bid    = book.best_yes_bid()
        self.no_bid     = book.best_no_bid()
        self.yes_ask    = book.best_yes_ask()    # 100 - no_bid
        self.no_ask     = book.best_no_ask()     # 100 - yes_bid
        self.mid        = book.mid_cents()
        self.spread     = book.spread_cents()

        # Sweep depth: cumulative contracts available when bidding up to SWEEP_CENTS
        # above the best ask.  Sweeping means we cross multiple book levels, making
        # fill far more likely when the top level is only 1-2 contracts deep.
        self.yes_ask_depth = book.depth_for_yes_buy(self.yes_ask + SWEEP_CENTS) if self.yes_ask else 0
        self.no_ask_depth  = book.depth_for_no_buy( self.no_ask  + SWEEP_CENTS) if self.no_ask  else 0

        # Best single-level depth (no sweep) — kept for reference / monitoring.
        self.yes_ask_depth_exact = int(book.no_bids.get(self.no_bid, 0)) if self.no_bid else 0
        self.no_ask_depth_exact  = int(book.yes_bids.get(self.yes_bid, 0)) if self.yes_bid else 0

        # Arb check: if yes_ask + no_ask < 100, a guaranteed profit exists.
        # is_arb is only True when prices are also within the liquid range —
        # near-binary prices (< MIN_PRICE_CENTS or > MAX_PRICE_CENTS) have
        # books too thin for IOC fills to reliably complete.
        if self.yes_ask is not None and self.no_ask is not None:
            self.raw_sum = self.yes_ask + self.no_ask
            self.gap_cents = Decimal(100 - self.raw_sum)
            n = TRADE_SIZE_CONTRACTS
            self.maker_gap = arb_gap_cents(self.yes_ask, self.no_ask, n, maker=True)
            self.taker_gap = arb_gap_cents(self.yes_ask, self.no_ask, n, maker=False)
            _liquid = (MIN_PRICE_CENTS <= self.yes_ask <= MAX_PRICE_CENTS
                       and MIN_PRICE_CENTS <= self.no_ask  <= MAX_PRICE_CENTS)
            self.is_arb = self.taker_gap > 0 and _liquid
        else:
            self.raw_sum = self.gap_cents = None
            self.maker_gap = self.taker_gap = None
            self.is_arb = False

    def to_dict(self) -> dict:
        def _r(v): return float(v) if v is not None else None
        return {
            "asset": self.asset, "ticker": self.ticker,
            "ts": self.ts.isoformat(), "secs_left": self.secs_left,
            "yes_bid": self.yes_bid, "no_bid": self.no_bid,
            "yes_ask": self.yes_ask, "no_ask": self.no_ask,
            "yes_ask_depth": self.yes_ask_depth, "no_ask_depth": self.no_ask_depth,
            "yes_ask_depth_exact": self.yes_ask_depth_exact,
            "no_ask_depth_exact":  self.no_ask_depth_exact,
            "mid": _r(self.mid), "spread": self.spread,
            "raw_sum": self.raw_sum,
            "gap_cents": _r(self.gap_cents),
            "maker_gap": _r(self.maker_gap),
            "taker_gap": _r(self.taker_gap),
            "is_arb": self.is_arb,
        }

# ── Arb monitoring statistics ─────────────────────────────────────────────────
class ArbStats:
    """
    Per-asset rolling statistics for all raw arb gaps (combined ask < 100¢).
    Tracked continuously so the monitoring dashboard can show signal quality
    before switching to live trading.
    """
    def __init__(self, asset: str):
        self.asset            = asset
        self.raw_gap_count    = 0      # all events where combined ask < 100¢
        self.profitable_count = 0      # events where taker_gap > 0
        self._total_raw_gap   = 0.0
        self.max_gap          = 0.0
        self.last_gap: Optional[float] = None
        self.last_ts:  Optional[float] = None
        self._started         = time.time()

    def record(self, raw_gap: float, taker_gap: float):
        self.raw_gap_count  += 1
        self._total_raw_gap += raw_gap
        if raw_gap > self.max_gap:
            self.max_gap = raw_gap
        if taker_gap > 0:
            self.profitable_count += 1
        self.last_gap = raw_gap
        self.last_ts  = time.time()

    @property
    def avg_gap(self) -> float:
        return self._total_raw_gap / self.raw_gap_count if self.raw_gap_count else 0.0

    @property
    def gaps_per_hour(self) -> float:
        elapsed = max(1.0, time.time() - self._started)
        return self.raw_gap_count / elapsed * 3600

    @property
    def capture_rate(self) -> float:
        return (self.profitable_count / self.raw_gap_count * 100) if self.raw_gap_count else 0.0

    def to_dict(self) -> dict:
        return {
            "asset":            self.asset,
            "raw_gap_count":    self.raw_gap_count,
            "profitable_count": self.profitable_count,
            "avg_gap":          round(self.avg_gap, 3),
            "max_gap":          round(self.max_gap, 3),
            "last_gap":         round(self.last_gap, 3) if self.last_gap is not None else None,
            "last_ts":          self.last_ts,
            "gaps_per_hour":    round(self.gaps_per_hour, 1),
            "capture_rate":     round(self.capture_rate, 1),
        }


# ── Bot engine ────────────────────────────────────────────────────────────────
class BotEngine:
    """
    Single WS connection to Kalshi. Maintains local orderbooks per market.
    Pushes MarketSnapshot to on_prices on every meaningful update.
    Fires on_arb when a taker-profitable gap is detected.
    """

    def __init__(self, on_log, on_prices, on_arb, on_status, on_feed=None):
        self.on_log    = on_log
        self.on_prices = on_prices
        self.on_arb    = on_arb
        self.on_status = on_status
        self.on_feed   = on_feed or (lambda a, role, s: None)

        self._stop     = threading.Event()
        self._lock     = threading.Lock()

        self.markets:   dict = {a: None for a in ASSETS}
        self._books:    dict = {}   # ticker → LocalBook
        self._ticker_map: dict = {} # ticker → asset
        self._snapshots: dict = {a: None for a in ASSETS}
        self._last_pushed: dict = {}
        self._last_arb_key: dict = {}

        self.update_count = 0
        self.arb_count    = 0
        self._arb_stats: dict = {a: ArbStats(a) for a in ASSETS}
        self._ws_sid_ob   = None   # subscription ID for orderbook_delta
        self._ws_sid_tk   = None   # subscription ID for ticker
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_msg_id   = 0

    # ── Public ────────────────────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        self._ws_started = False
        self.on_log("→", f"Starting Kalshi engine  demo={USE_DEMO}  dry_run={DRY_RUN}")
        self.on_status("discovering")

        with self._lock:
            self.markets = discover_all(self.on_log)
            self._rebuild_maps()

        # Expiry loop always runs — handles both normal expiry and waiting for market open
        threading.Thread(target=self._expiry_loop, daemon=True, name="expiry").start()

        if not any(self.markets.values()):
            self.on_status("waiting")
            return

        self._ws_started = True
        threading.Thread(target=self._ws_loop,       daemon=True, name="ws").start()
        threading.Thread(target=self._rest_poll_loop, daemon=True, name="rest-poll").start()

    def stop(self):
        self._stop.set()
        if self._ws:
            try: self._ws.close()
            except: pass

    def is_running(self) -> bool:
        return not self._stop.is_set()

    def get_snapshot(self, asset: str) -> Optional[MarketSnapshot]:
        with self._lock:
            return self._snapshots.get(asset)

    def get_arb_stats(self) -> dict:
        with self._lock:
            return {a: s.to_dict() for a, s in self._arb_stats.items()}

    # ── Internal ──────────────────────────────────────────────────────────────
    def _rebuild_maps(self):
        """Must be called with _lock held."""
        self._ticker_map = {}
        new_books = {}
        for asset, mkt in self.markets.items():
            if not mkt:
                continue
            ticker = mkt["ticker"]
            self._ticker_map[ticker] = asset
            new_books[ticker] = self._books.get(ticker, LocalBook(ticker))
        self._books = new_books

    def _all_tickers(self) -> list:
        with self._lock:
            return list(self._ticker_map.keys())

    def _next_id(self) -> int:
        self._ws_msg_id += 1
        return self._ws_msg_id

    # ── WS handlers ───────────────────────────────────────────────────────────
    def _on_ws_open(self, ws):
        self.on_log("→", "WebSocket connected")
        self.on_status("connected")
        tickers = self._all_tickers()
        if not tickers:
            self.on_log("✗", "No tickers to subscribe to")
            return

        has_creds = getattr(self, "_has_creds", False)
        channels  = ["ticker"]
        if has_creds:
            channels.append("orderbook_delta")

        sub_msg = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            }
        }
        ws.send(json.dumps(sub_msg))
        self.on_log("→", f"Subscribed to {len(tickers)} markets  channels={channels}")
        self.on_status("monitoring")

    def _on_ws_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        mtype = msg.get("type")
        body  = msg.get("msg", {})

        if mtype == "subscribed":
            chan = body.get("channel", "")
            sid  = body.get("sid")
            if "orderbook" in chan:
                self._ws_sid_ob = sid
            elif "ticker" in chan:
                self._ws_sid_tk = sid
            self.on_log("→", f"Subscribed sid={sid}  channel={chan}")

        elif mtype == "orderbook_snapshot":
            self._handle_snapshot(body)

        elif mtype == "orderbook_delta":
            self._handle_delta(body)

        elif mtype == "ticker":
            self._handle_ticker(body)

        elif mtype == "error":
            code = body.get("code")
            emsg = body.get("msg", "")
            self.on_log("✗", f"WS error {code}: {emsg}")

    def _handle_snapshot(self, body: dict):
        ticker = body.get("market_ticker")
        with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return
            book.apply_snapshot(body)
            asset = self._ticker_map.get(ticker)
        if asset:
            self._compute_and_push(asset)

    def _handle_delta(self, body: dict):
        ticker = body.get("market_ticker")
        with self._lock:
            book = self._books.get(ticker)
            if book and book.ready:
                book.apply_delta(body)
            asset = self._ticker_map.get(ticker)
        self.update_count += 1
        if asset:
            self._compute_and_push(asset)

    def _handle_ticker(self, body: dict):
        """
        Ticker update per docs:
          {"market_ticker":..., "yes_bid_dollars":"0.48", "yes_ask_dollars":"0.51",
           "no_bid_dollars":"0.49", "no_ask_dollars":"0.52", ...}

        This is the PRIMARY price source when using ticker channel only (no auth).
        We derive the book's best bid levels directly from the ticker so that
        _compute_and_push can produce a valid MarketSnapshot.

        Kalshi implied pricing:
          YES ask = yes_ask_dollars field  (also = 100 - no_bid)
          NO  ask = no_ask_dollars field   (also = 100 - yes_bid)
        We keep both yes_bids and no_bids in sync from the ticker.
        """
        ticker = body.get("market_ticker")
        with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return

            # Parse all four price fields from ticker
            yb  = _cents(body.get("yes_bid_dollars"))
            ya  = _cents(body.get("yes_ask_dollars"))
            nb  = _cents(body.get("no_bid_dollars"))
            # Derive no_bid from yes_ask if not provided directly
            if nb is None and ya is not None:
                nb = 100 - ya

            # Replace best bid levels with what the ticker reports.
            # Wipe stale levels first so we don't accumulate garbage.
            if yb is not None and 1 <= yb <= 99:
                book.yes_bids = {yb: 1.0}
            if nb is not None and 1 <= nb <= 99:
                book.no_bids  = {nb: 1.0}

            # Mark book ready — ticker alone is sufficient for price display
            if yb is not None or nb is not None:
                book.ready = True

            asset = self._ticker_map.get(ticker)

        self.update_count += 1
        if asset:
            self._compute_and_push(asset)

    def _compute_and_push(self, asset: str):
        """Compute snapshot, check for arb, push to UI. Called outside lock."""
        with self._lock:
            mkt  = self.markets.get(asset)
            book = self._books.get(mkt["ticker"]) if mkt else None
            if not mkt or not book or not book.ready:
                return
            secs_left = max(0, int(
                (datetime.fromisoformat(
                    mkt["close_time"].replace("Z", "+00:00")
                ) - datetime.now(timezone.utc)).total_seconds()
            ))
            snap = MarketSnapshot(asset, mkt["ticker"], book, mkt["window_ts"], secs_left)
            self._snapshots[asset] = snap

            # Dedup: only push if yes_ask or no_ask changed
            prev = self._last_pushed.get(asset)
            changed = (
                prev is None
                or prev[0] != snap.yes_ask
                or prev[1] != snap.no_ask
            )
            if changed:
                self._last_pushed[asset] = (snap.yes_ask, snap.no_ask)
                mkts_copy = {a: m for a, m in self.markets.items()}
                snaps_copy = {a: (s.to_dict() if s else None)
                              for a, s in self._snapshots.items()}
            else:
                mkts_copy = snaps_copy = None

            # Monitoring stats: record every price-changed event where combined ask < 100¢
            monitor_entry = None
            if changed and snap.raw_sum is not None and snap.raw_sum < 100:
                raw_gap   = float(100 - snap.raw_sum)
                taker_gap = float(snap.taker_gap) if snap.taker_gap is not None else 0.0
                self._arb_stats[asset].record(raw_gap, taker_gap)
                monitor_entry = {**snap.to_dict(), "event": "raw_gap", "raw_gap": raw_gap}

            # Arb dedup — only fire on_arb when taker_gap >= MIN_GAP_CENTS
            arb_snap = None
            if snap.is_arb and float(snap.taker_gap) >= MIN_GAP_CENTS:
                key = (snap.yes_ask, snap.no_ask)
                if self._last_arb_key.get(asset) != key:
                    self._last_arb_key[asset] = key
                    self.arb_count += 1
                    arb_snap = snap
            else:
                self._last_arb_key.pop(asset, None)

        if monitor_entry:
            _write_monitor_log(monitor_entry)

        if mkts_copy is not None:
            self.on_prices(mkts_copy, snaps_copy)

        if arb_snap:
            self.on_log("⚡", (
                f"{asset}  YES_ask={arb_snap.yes_ask}¢  NO_ask={arb_snap.no_ask}¢  "
                f"taker_gap=+{float(arb_snap.taker_gap):.3f}¢  "
                f"maker_gap=+{float(arb_snap.maker_gap):.3f}¢"
            ))
            _write_log(arb_snap.to_dict())
            self.on_arb(arb_snap)

    def _rest_poll_loop(self):
        """
        REST polling fallback — refreshes best bid/ask from the markets REST endpoint
        every 5 seconds. Runs alongside WS; if WS is working this is redundant but
        harmless. Critical when WS is unavailable (no credentials).
        """
        while not self._stop.is_set():
            self._stop.wait(5)
            if self._stop.is_set():
                break
            for asset in ASSETS:
                with self._lock:
                    mkt = self.markets.get(asset)
                if not mkt:
                    continue
                try:
                    data = kalshi_get_public(f"/markets/{mkt['ticker']}")
                    m    = data.get("market", {})
                    if not m:
                        continue
                    yb = _parse_price(m.get("yes_bid_dollars"))
                    nb = _parse_price(m.get("no_bid_dollars"))
                    if yb is None and nb is None:
                        continue
                    with self._lock:
                        book = self._books.get(mkt["ticker"])
                        if book is None:
                            continue
                        if yb is not None and 1 <= yb <= 99:
                            book.yes_bids = {yb: 1.0}
                        if nb is not None and 1 <= nb <= 99:
                            book.no_bids  = {nb: 1.0}
                        book.ready = True
                    self.update_count += 1
                    self._compute_and_push(asset)
                except Exception:
                    pass

    def _on_ws_error(self, ws, error):
        self.on_log("✗", f"WS error: {error}")

    def _on_ws_close(self, ws, code, msg):
        if not self._stop.is_set():
            self.on_log("!", f"WS closed (code={code}) — reconnecting in 3s…")
            self.on_status("reconnecting")
            def _r():
                time.sleep(3)
                if not self._stop.is_set():
                    self._ws_loop()
            threading.Thread(target=_r, daemon=True, name="ws-reconnect").start()

    def _ws_loop(self):
        tickers = self._all_tickers()
        if not tickers:
            self.on_log("✗", "No tickers — cannot open WebSocket")
            return

        # ALL Kalshi WS channels require authentication (ticker is NOT public).
        # Generate headers fresh here — timestamp must be current at connection time.
        # websocket-client requires headers as a list of "Key: Value" strings.
        has_creds = bool(KALSHI_KEY_ID and Path(KALSHI_KEY_FILE).exists())
        self._has_creds = has_creds

        if not has_creds:
            self.on_log("✗", (
                "No credentials found — WS requires auth. "
                "Set KALSHI_KEY_ID and KALSHI_KEY_FILE in .env. "
                "Market discovery (REST) will still run."
            ))
            return

        headers = _ws_auth_headers()
        self.on_log("→", f"Connecting to WS  url={WS_URL}")

        self._ws = websocket.WebSocketApp(
            WS_URL,
            header=headers,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _expiry_loop(self):
        """Check for expired markets every 30s; skips assets whose next open is far away."""
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break
            now = time.time()
            for asset in ASSETS:
                with self._lock:
                    mkt = self.markets.get(asset)
                if mkt is None:
                    # Respect backoff: don't retry until 30s before next open
                    next_open = _next_market_opens.get(asset, 0.0)
                    if next_open and now < next_open - 30:
                        continue
                    self._rediscover(asset)
                    continue
                # Check if close_time has passed
                try:
                    ct = datetime.fromisoformat(mkt["close_time"].replace("Z", "+00:00"))
                    if ct <= datetime.now(timezone.utc):
                        self.on_log("→", f"{asset} market expired — rediscovering…")
                        self._rediscover(asset)
                except Exception:
                    pass

    def _rediscover(self, asset: str):
        new_mkt = discover_market(asset, self.on_log)
        if not new_mkt:
            return
        with self._lock:
            old_mkt = self.markets.get(asset)
            old_ticker = old_mkt["ticker"] if old_mkt else None
            self.markets[asset] = new_mkt
            self._snapshots[asset] = None
            self._rebuild_maps()
            self._last_arb_key.pop(asset, None)
            self._last_pushed.pop(asset, None)
            self._arb_stats[asset] = ArbStats(asset)
            if old_ticker:
                self._books.pop(old_ticker, None)

        # If WS/REST loops weren't started (bot launched while markets were closed),
        # start them now that the first market has been found.
        if not getattr(self, '_ws_started', False):
            self._ws_started = True
            self.on_log("→", f"Market found — starting WS and REST poll loops")
            self.on_status("connecting")
            threading.Thread(target=self._ws_loop,       daemon=True, name="ws").start()
            threading.Thread(target=self._rest_poll_loop, daemon=True, name="rest-poll").start()
            return

        ws = self._ws
        if ws:
            new_ticker = new_mkt["ticker"]
            if old_ticker and old_ticker != new_ticker:
                try:
                    ws.send(json.dumps({
                        "id": self._next_id(), "cmd": "update_subscription",
                        "params": {
                            "sids": [self._ws_sid_ob],
                            "market_tickers": [old_ticker],
                            "action": "delete_markets",
                        }
                    }))
                except Exception:
                    pass
            try:
                ws.send(json.dumps({
                    "id": self._next_id(), "cmd": "update_subscription",
                    "params": {
                        "sids": [self._ws_sid_ob],
                        "market_tickers": [new_ticker],
                        "action": "add_markets",
                    }
                }))
                self.on_log("→", f"{asset} subscribed new ticker {new_ticker}")
            except Exception as e:
                self.on_log("✗", f"{asset} subscribe failed: {e}")

# ── Logging ───────────────────────────────────────────────────────────────────
def _write_log(entry: dict):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass

def _write_monitor_log(entry: dict):
    """Logs every raw gap event (combined ask < 100¢) for monitoring/analysis."""
    try:
        with open(MONITOR_LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass