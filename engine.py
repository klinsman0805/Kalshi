"""
engine.py — Kalshi 15-min Crypto Momentum Bot

AUTH:      RSA-PSS signed headers on every request
PRICES:    Integer cents (1–99)
ORDERBOOK: YES bids + NO bids only. Implied asks:
             YES ask = 100 - best_NO_bid
             NO  ask = 100 - best_YES_bid
WS URL:    wss://api.elections.kalshi.com/trade-api/ws/v2
"""

import os
import json
import time
import base64
import threading
import logging
from datetime import datetime, timezone
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
PROD_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_API_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_WS_URL   = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL   = "wss://demo-api.kalshi.co/trade-api/ws/v2"

USE_DEMO = os.getenv("KALSHI_DEMO", "true").lower() != "false"
DRY_RUN  = os.getenv("DRY_RUN",     "true").lower() != "false"
API_BASE = os.getenv("KALSHI_API_BASE", DEMO_API_BASE if USE_DEMO else PROD_API_BASE)
WS_URL   = os.getenv("KALSHI_WS_URL",   DEMO_WS_URL   if USE_DEMO else PROD_WS_URL)

if USE_DEMO:
    KALSHI_KEY_ID   = os.getenv("KALSHI_DEMO_KEY_ID",  os.getenv("KALSHI_KEY_ID",   ""))
    KALSHI_KEY_FILE = os.getenv("KALSHI_DEMO_KEY_FILE", os.getenv("KALSHI_KEY_FILE", "kalshi_demo.key"))
else:
    KALSHI_KEY_ID   = os.getenv("KALSHI_KEY_ID",   "")
    KALSHI_KEY_FILE = os.getenv("KALSHI_KEY_FILE",  "kalshi.key")

TRADE_SIZE_CONTRACTS   = int(os.getenv("TRADE_SIZE_CONTRACTS",   "5"))
MAX_POSITION_CONTRACTS = int(os.getenv("MAX_POSITION_CONTRACTS", "20"))
LOG_FILE               = Path(os.getenv("LOG_FILE", "kalshi_log.jsonl"))
REQUEST_TIMEOUT        = 8
ORDER_TIMEOUT          = 5

ASSETS     = ["BTC", "ETH", "SOL"]
SERIES_MAP = {
    "BTC": os.getenv("SERIES_BTC", "KXBTC15M"),
    "ETH": os.getenv("SERIES_ETH", "KXETH15M"),
    "SOL": os.getenv("SERIES_SOL", "KXSOL15M"),
}
WINDOW_SECS = 900

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
    key = _load_key()
    if key is None:
        return ""
    msg = f"{timestamp_ms}{method}{path.split('?')[0]}".encode("utf-8")
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")

def _auth_headers(method: str, path: str) -> dict:
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": _sign(ts, method, path),
        "Content-Type":            "application/json",
    }

def _ws_auth_headers() -> list:
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
SESSION  = requests.Session()
_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
SESSION.mount("https://", _adapter)
SESSION.headers.update({"Connection": "keep-alive"})

def kalshi_get(path: str, params: dict = None) -> dict:
    full_path = f"/trade-api/v2{path}"
    headers   = _auth_headers("GET", full_path)
    r = SESSION.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def kalshi_get_public(path: str, params: dict = None) -> dict:
    r = SESSION.get(f"{PROD_API_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def pre_warm_connection():
    try:
        kalshi_get("/portfolio/balance")
        log.info("HTTP connection pre-warmed to %s", API_BASE)
    except Exception as e:
        log.warning("pre_warm_connection failed (non-fatal): %s", e)

# ── Market discovery ──────────────────────────────────────────────────────────
def _pick_best_market(mkts: list) -> Optional[tuple]:
    now = datetime.now(timezone.utc)
    best = best_ct = None
    for mkt in mkts:
        try:
            ct = datetime.fromisoformat(mkt.get("close_time", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now:
            continue
        if best_ct is None or ct < best_ct:
            best, best_ct = mkt, ct
    return (best, best_ct) if best else (None, None)

_next_market_opens: dict = {a: 0.0 for a in ASSETS}

def discover_market(asset: str, on_log: Callable) -> Optional[dict]:
    series = SERIES_MAP[asset]
    try:
        data      = kalshi_get_public("/markets", params={"series_ticker": series, "limit": 200})
        mkts      = data.get("markets", [])
        if not mkts:
            on_log("✗", f"{asset}: no markets found for series {series} — check SERIES_{asset} in .env")
            return None
        now       = datetime.now(timezone.utc)
        open_mkts = [m for m in mkts if m.get("status") == "active"]
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
                wait_hrs, wait_rem = wait_mins // 60, wait_mins % 60
                wait_str  = f"{wait_hrs}h {wait_rem}m" if wait_hrs else f"{wait_mins}m"
                on_log("~", (
                    f"{asset}: market closed — next window opens in {wait_str} "
                    f"at {next_open.strftime('%H:%M UTC')} ({next_mkt['ticker']})"
                ))
            else:
                on_log("✗", f"{asset}: no open or upcoming markets in {series}")
            return None
        _next_market_opens[asset] = 0.0
        best, best_ct = _pick_best_market(open_mkts)
        if best is None:
            on_log("✗", f"{asset}: open markets already past close_time")
            return None
        ticker    = best["ticker"]
        secs_left = max(0, int((best_ct - now).total_seconds()))
        yes_bid   = _parse_price(best.get("yes_bid_dollars"))
        no_bid    = _parse_price(best.get("no_bid_dollars"))
        result = {
            "asset":      asset,
            "ticker":     ticker,
            "series":     series,
            "close_time": best.get("close_time", ""),
            "open_time":  best.get("open_time",  ""),
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
    return None

def _parse_price(val) -> Optional[int]:
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
    YES bids + NO bids in cents. Implied asks:
      YES ask = 100 - best_NO_bid
      NO  ask = 100 - best_YES_bid
    """
    MAX_LEVELS = 200

    def __init__(self, ticker: str):
        self.ticker    = ticker
        self.yes_bids: dict = {}
        self.no_bids:  dict = {}
        self.ready     = False

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
        p     = _cents(msg.get("price_dollars"))
        delta = float(msg.get("delta_fp", "0"))
        side  = msg.get("side", "").lower()
        if p is None or not (1 <= p <= 99):
            return
        book = self.yes_bids if side == "yes" else self.no_bids
        new  = book.get(p, 0.0) + delta
        if new <= 0:
            book.pop(p, None)
        else:
            if p not in book and len(book) >= self.MAX_LEVELS:
                return
            book[p] = new

    def best_yes_bid(self) -> Optional[int]:
        return max(self.yes_bids.keys()) if self.yes_bids else None

    def best_no_bid(self) -> Optional[int]:
        return max(self.no_bids.keys()) if self.no_bids else None

    def best_yes_ask(self) -> Optional[int]:
        no_bid = self.best_no_bid()
        return (100 - no_bid) if no_bid is not None else None

    def best_no_ask(self) -> Optional[int]:
        yes_bid = self.best_yes_bid()
        return (100 - yes_bid) if yes_bid is not None else None

    def mid_cents(self) -> Optional[float]:
        yb, ya = self.best_yes_bid(), self.best_yes_ask()
        if yb is None or ya is None:
            return None
        return (yb + ya) / 2.0

def _cents(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None

# ── Market snapshot ───────────────────────────────────────────────────────────
class MarketSnapshot:
    def __init__(self, asset: str, ticker: str, book: LocalBook,
                 window_ts: int, secs_left: int):
        self.asset     = asset
        self.ticker    = ticker
        self.window_ts = window_ts
        self.secs_left = secs_left
        self.ts        = datetime.now(timezone.utc)
        self.yes_bid   = book.best_yes_bid()
        self.no_bid    = book.best_no_bid()
        self.yes_ask   = book.best_yes_ask()
        self.no_ask    = book.best_no_ask()
        self.mid       = book.mid_cents()

    def to_dict(self) -> dict:
        return {
            "asset":     self.asset,
            "ticker":    self.ticker,
            "ts":        self.ts.isoformat(),
            "secs_left": self.secs_left,
            "yes_bid":   self.yes_bid,
            "no_bid":    self.no_bid,
            "yes_ask":   self.yes_ask,
            "no_ask":    self.no_ask,
            "mid":       self.mid,
        }

# ── Bot engine ────────────────────────────────────────────────────────────────
class BotEngine:
    """
    Single WS connection to Kalshi. Maintains local orderbooks per market.
    Pushes MarketSnapshot to on_prices on every meaningful price change.
    """

    def __init__(self, on_log, on_prices, on_status):
        self.on_log    = on_log
        self.on_prices = on_prices
        self.on_status = on_status

        self._stop    = threading.Event()
        self._lock    = threading.Lock()

        self.markets:     dict = {a: None for a in ASSETS}
        self._books:      dict = {}
        self._ticker_map: dict = {}
        self._snapshots:  dict = {a: None for a in ASSETS}
        self._last_pushed: dict = {}

        self._ws_sid_ob  = None
        self._ws_sid_tk  = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_msg_id  = 0
        self.update_count = 0

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._ws_started = False
        self.on_log("→", f"Starting Kalshi engine  demo={USE_DEMO}  dry_run={DRY_RUN}")
        self.on_status("discovering")
        with self._lock:
            self.markets = discover_all(self.on_log)
            self._rebuild_maps()
        threading.Thread(target=self._expiry_loop, daemon=True, name="expiry").start()
        if not any(self.markets.values()):
            self.on_status("waiting")
            return
        self._ws_started = True
        threading.Thread(target=self._ws_loop,        daemon=True, name="ws").start()
        threading.Thread(target=self._rest_poll_loop,  daemon=True, name="rest-poll").start()

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

    # ── Internal ──────────────────────────────────────────────────────────────

    def _rebuild_maps(self):
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
        tickers   = self._all_tickers()
        if not tickers:
            self.on_log("✗", "No tickers to subscribe to")
            return
        has_creds = getattr(self, "_has_creds", False)
        channels  = ["ticker"]
        if has_creds:
            channels.append("orderbook_delta")
        ws.send(json.dumps({
            "id": self._next_id(), "cmd": "subscribe",
            "params": {"channels": channels, "market_tickers": tickers},
        }))
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
            self.on_log("✗", f"WS error {body.get('code')}: {body.get('msg', '')}")

    def _handle_snapshot(self, body: dict):
        ticker = body.get("market_ticker")
        with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return
            was_ready = book.ready
            book.apply_snapshot(body)
            asset = self._ticker_map.get(ticker)
        if asset:
            if not was_ready:
                self.on_log("📖", (
                    f"{asset} orderbook ready  "
                    f"yes_levels={len(book.yes_bids)}  no_levels={len(book.no_bids)}"
                ))
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
        ticker = body.get("market_ticker")
        with self._lock:
            book = self._books.get(ticker)
            if book is None:
                return
            yb = _cents(body.get("yes_bid_dollars"))
            ya = _cents(body.get("yes_ask_dollars"))
            nb = _cents(body.get("no_bid_dollars"))
            if nb is None and ya is not None:
                nb = 100 - ya
            if yb is not None and 1 <= yb <= 99:
                book.yes_bids = {yb: 1.0}
            if nb is not None and 1 <= nb <= 99:
                book.no_bids  = {nb: 1.0}
            if yb is not None or nb is not None:
                book.ready = True
            asset = self._ticker_map.get(ticker)
        self.update_count += 1
        if asset:
            self._compute_and_push(asset)

    def _compute_and_push(self, asset: str):
        with self._lock:
            mkt  = self.markets.get(asset)
            book = self._books.get(mkt["ticker"]) if mkt else None
            if not mkt or not book or not book.ready:
                return
            secs_left = max(0, int(
                (datetime.fromisoformat(mkt["close_time"].replace("Z", "+00:00"))
                 - datetime.now(timezone.utc)).total_seconds()
            ))
            snap = MarketSnapshot(asset, mkt["ticker"], book, mkt["window_ts"], secs_left)
            self._snapshots[asset] = snap
            prev    = self._last_pushed.get(asset)
            changed = prev is None or prev[0] != snap.yes_ask or prev[1] != snap.no_ask
            if changed:
                self._last_pushed[asset] = (snap.yes_ask, snap.no_ask)
                mkts_copy  = {a: m for a, m in self.markets.items()}
                snaps_copy = {a: (s.to_dict() if s else None) for a, s in self._snapshots.items()}
            else:
                mkts_copy = snaps_copy = None
        if mkts_copy is not None:
            self.on_prices(mkts_copy, snaps_copy)

    def _rest_poll_loop(self):
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
                except Exception as e:
                    log.debug("REST poll error %s: %s", asset, e)

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
        has_creds = bool(KALSHI_KEY_ID and Path(KALSHI_KEY_FILE).exists())
        self._has_creds = has_creds
        if not has_creds:
            self.on_log("✗", (
                "No credentials found — WS requires auth. "
                "Set KALSHI_KEY_ID and KALSHI_KEY_FILE in .env."
            ))
            return
        headers = _ws_auth_headers()
        self.on_log("→", f"Connecting to WS  url={WS_URL}")
        self._ws = websocket.WebSocketApp(
            WS_URL, header=headers,
            on_open=self._on_ws_open, on_message=self._on_ws_message,
            on_error=self._on_ws_error, on_close=self._on_ws_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _expiry_loop(self):
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break
            now = time.time()
            for asset in ASSETS:
                with self._lock:
                    mkt = self.markets.get(asset)
                if mkt is None:
                    next_open = _next_market_opens.get(asset, 0.0)
                    if next_open and now < next_open - 30:
                        continue
                    self._rediscover(asset)
                    continue
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
            old_mkt    = self.markets.get(asset)
            old_ticker = old_mkt["ticker"] if old_mkt else None
            self.markets[asset] = new_mkt
            self._snapshots[asset] = None
            self._rebuild_maps()
            self._last_pushed.pop(asset, None)
            if old_ticker:
                self._books.pop(old_ticker, None)
        if not getattr(self, "_ws_started", False):
            self._ws_started = True
            self.on_log("→", "Market found — starting WS and REST poll loops")
            self.on_status("connecting")
            threading.Thread(target=self._ws_loop,        daemon=True, name="ws").start()
            threading.Thread(target=self._rest_poll_loop,  daemon=True, name="rest-poll").start()
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
                        },
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
                    },
                }))
                self.on_log("→", f"{asset} subscribed new ticker {new_ticker}")
            except Exception as e:
                self.on_log("✗", f"{asset} subscribe failed: {e}")

