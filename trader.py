"""
trader.py — Kalshi Market Making Order Execution

ORDER TYPES USED
─────────────────
  Maker (resting limit, post_only=True):
    - Sits in the book, gets filled when someone crosses
    - Maker fee: 0.0175 × P × (1-P) per contract (4× cheaper than taker)
    - No charge until filled; no charge if cancelled
    - This is our PRIMARY order type

  Taker (immediate IOC, time_in_force="immediate_or_cancel"):
    - Fills immediately against resting orders or cancels
    - Taker fee: 0.07 × P × (1-P) per contract
    - Used only for arb execution when a gap > fees exists

ORDER BODY (POST /portfolio/orders)
─────────────────────────────────────
  {
    "ticker":          "KXBTCD-15MIN-1234567890",
    "side":            "yes" | "no",
    "action":          "buy",
    "count":           5,              # integer contracts
    "yes_price":       48,             # cents, integer 1-99 (for YES side)
    "no_price":        52,             # cents, integer 1-99 (for NO side)
    "time_in_force":   "gtc",                    # for maker
    "post_only":       true,           # reject if would take (ensures maker)
    "client_order_id": "mm-btc-yes-1234567890", # for tracking
  }

NOTE: You specify EITHER yes_price (when buying YES) OR no_price (when buying NO).
      The API infers the other side. Prices are integers in cents.

POSITION TRACKING
──────────────────
  positions.json schema:
  {
    "open_orders": {
      "<client_order_id>": {
        "asset", "ticker", "side", "price_cents", "count", "ts", "order_id"
      }
    },
    "positions": {
      "<ticker>": {
        "yes_contracts": 5,    # net YES held
        "no_contracts":  5,    # net NO held
        "avg_yes_cost":  0.48, # average cost in dollars
        "avg_no_cost":   0.52,
      }
    },
    "realised_pnl": 0.0,
    "total_fills": 0
  }
"""

import os
import json
import time
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from engine import (
    kalshi_get, _auth_headers, API_BASE, SESSION, ORDER_TIMEOUT,
    total_maker_fee, total_taker_fee, arb_gap_cents,
    TRADE_SIZE_CONTRACTS, MAX_POSITION_CONTRACTS,
    SPREAD_TARGET_CENTS, REQUOTE_THRESHOLD,
    MIN_PRICE_CENTS, MAX_PRICE_CENTS,
    SWEEP_CENTS, MIN_PROFIT_AFTER_SWEEP,
    DRY_RUN, USE_DEMO, ASSETS,
)

log = logging.getLogger("kalshi.trader")

# ── Config ────────────────────────────────────────────────────────────────────
POSITIONS_FILE        = Path(os.getenv("POSITIONS_FILE", "positions.json"))
TRADES_FILE           = Path(os.getenv("TRADES_FILE",    "trades.jsonl"))
CANCEL_ON_EXIT        = os.getenv("CANCEL_ON_EXIT", "true").lower() != "false"

ARB_COOLDOWN_SECS     = float(os.getenv("ARB_COOLDOWN_SECS",     "10.0"))
MAX_CONCURRENT_ORDERS = int(os.getenv("MAX_CONCURRENT_ORDERS",   "1"))
MAX_DAILY_LOSS_USD    = float(os.getenv("MAX_DAILY_LOSS_USD",     "10.0"))
MIN_SECS_LEFT         = int(os.getenv("MIN_SECS_LEFT",           "60"))

# ── Position book ─────────────────────────────────────────────────────────────
class PositionBook:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "open_orders": {},
            "positions": {},
            "realised_pnl": 0.0,
            "total_fills": 0,
        }
        self._load()

    def _load(self):
        if POSITIONS_FILE.exists():
            try:
                self._data = json.loads(POSITIONS_FILE.read_text())
                log.info("Loaded positions from %s", POSITIONS_FILE)
            except Exception as e:
                log.warning("Could not load positions: %s", e)

    def _save(self):
        try:
            POSITIONS_FILE.write_text(json.dumps(self._data, indent=2, default=str))
        except Exception as e:
            log.warning("Could not save positions: %s", e)

    def yes_position(self, ticker: str) -> int:
        with self._lock:
            return self._data["positions"].get(ticker, {}).get("yes_contracts", 0)

    def no_position(self, ticker: str) -> int:
        with self._lock:
            return self._data["positions"].get(ticker, {}).get("no_contracts", 0)

    def total_exposure(self, ticker: str) -> int:
        """Total contracts held (YES + NO) for this ticker."""
        return self.yes_position(ticker) + self.no_position(ticker)

    def record_fill(self, ticker: str, side: str, price_cents: int,
                    count: int, client_order_id: str):
        """Called when an order fill is confirmed."""
        with self._lock:
            pos = self._data["positions"].setdefault(ticker, {
                "yes_contracts": 0, "no_contracts": 0,
                "avg_yes_cost": 0.0, "avg_no_cost": 0.0,
            })
            key   = f"{side}_contracts"
            avg_k = f"avg_{side}_cost"
            prev_n   = pos[key]
            prev_avg = pos[avg_k]
            new_n    = prev_n + count
            # Weighted average cost
            if new_n > 0:
                pos[avg_k] = (prev_avg * prev_n + (price_cents / 100) * count) / new_n
            pos[key] = new_n
            self._data["total_fills"] += 1
            # Remove from open orders
            self._data["open_orders"].pop(client_order_id, None)
            self._save()

    def record_open_order(self, client_order_id: str, info: dict):
        with self._lock:
            self._data["open_orders"][client_order_id] = info
            self._save()

    def remove_open_order(self, client_order_id: str):
        with self._lock:
            self._data["open_orders"].pop(client_order_id, None)
            self._save()

    def open_orders_for(self, ticker: str) -> list:
        with self._lock:
            return [
                {"client_order_id": cid, **info}
                for cid, info in self._data["open_orders"].items()
                if info.get("ticker") == ticker
            ]

    def realise_pnl(self, amount: float):
        with self._lock:
            self._data["realised_pnl"] += amount
            self._save()

POSITIONS = PositionBook()

# ── Arb execution guardrails ──────────────────────────────────────────────────
_ARB_LOCK            = threading.Semaphore(MAX_CONCURRENT_ORDERS)
_cooldown_lock       = threading.Lock()
_asset_cooldowns:    dict = {}              # asset → timestamp of last execution start
_session_start_pnl:  Optional[float] = None
_halt_trading        = threading.Event()


def _check_cooldown(asset: str) -> Optional[str]:
    """Returns None if execution is allowed; returns skip-reason string otherwise."""
    if _halt_trading.is_set():
        return "circuit_breaker"
    now = time.time()
    with _cooldown_lock:
        last = _asset_cooldowns.get(asset, 0.0)
        if now - last < ARB_COOLDOWN_SECS:
            log.debug("ARB cooldown  %s  %.1fs remaining", asset,
                      ARB_COOLDOWN_SECS - (now - last))
            return "cooldown"
    return None


def _arm_cooldown(asset: str):
    """Record execution start time so subsequent calls observe the cooldown."""
    with _cooldown_lock:
        _asset_cooldowns[asset] = time.time()


def _check_session_loss() -> bool:
    """Returns True (and sets halt flag) when session loss exceeds the daily limit."""
    if DRY_RUN:
        return False
    global _session_start_pnl
    with _cooldown_lock:
        if _session_start_pnl is None:
            _session_start_pnl = POSITIONS._data.get("realised_pnl", 0.0)
        session_pnl = POSITIONS._data.get("realised_pnl", 0.0) - _session_start_pnl
        if session_pnl < -MAX_DAILY_LOSS_USD:
            _halt_trading.set()
            log.error("CIRCUIT BREAKER: session loss $%.2f exceeds $%.2f limit — halting arb",
                      abs(session_pnl), MAX_DAILY_LOSS_USD)
            return True
    return False


def get_guardrail_status() -> dict:
    """Returns current guardrail state for dashboard display."""
    now = time.time()
    cooldowns = {}
    with _cooldown_lock:
        for asset, last in _asset_cooldowns.items():
            remaining = max(0.0, ARB_COOLDOWN_SECS - (now - last))
            if remaining > 0:
                cooldowns[asset] = round(remaining, 1)
    return {
        "halted":         _halt_trading.is_set(),
        "cooldowns":      cooldowns,
        "cooldown_secs":  ARB_COOLDOWN_SECS,
        "max_concurrent": MAX_CONCURRENT_ORDERS,
        "max_daily_loss": MAX_DAILY_LOSS_USD,
    }


def reset_halt():
    """Manually clear the circuit-breaker halt (call after investigating losses)."""
    global _session_start_pnl
    _halt_trading.clear()
    with _cooldown_lock:
        _session_start_pnl = None    # reset daily loss baseline after manual clear
    log.info("Circuit breaker reset — arb execution re-enabled")


# ── REST order functions ───────────────────────────────────────────────────────
def _post_order(body: dict) -> dict:
    """POST /portfolio/orders — uses the shared persistent session."""
    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = f"{API_BASE}/portfolio/orders"
    t0 = time.perf_counter()
    r  = SESSION.post(url, json=body, headers=headers, timeout=ORDER_TIMEOUT)
    ms = (time.perf_counter() - t0) * 1000
    log.info("ORDER rtt=%.0fms  side=%s  status=%d", ms, body.get("side","?"), r.status_code)
    r.raise_for_status()
    return r.json()

def _cancel_order(order_id: str) -> dict:
    """DELETE /portfolio/orders/{order_id} — uses the shared persistent session."""
    path    = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = _auth_headers("DELETE", path)
    url     = f"{API_BASE}/portfolio/orders/{order_id}"
    r = SESSION.delete(url, headers=headers, timeout=ORDER_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _make_client_id(asset: str, side: str) -> str:
    return f"mm-{asset.lower()}-{side}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

# ── Market making ─────────────────────────────────────────────────────────────
def place_maker_quote(asset: str, ticker: str, side: str, price_cents: int,
                      n_contracts: int) -> dict:
    """
    Place a resting limit order (maker, post_only=True).
    side: "yes" or "no"
    price_cents: integer 1-99
    Returns result dict.
    """
    cid = _make_client_id(asset, side)
    fee = total_maker_fee(price_cents, n_contracts)

    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "asset": asset, "ticker": ticker,
        "side": side, "price_cents": price_cents,
        "n_contracts": n_contracts,
        "maker_fee_dollars": float(fee),
        "client_order_id": cid,
        "dry_run": DRY_RUN,
        "status": "pending",
        "order_id": None,
        "error": None,
    }

    if DRY_RUN:
        result["status"] = "dry_run"
        log.info("[DRY RUN] MAKER %s  %s  %d¢ × %d  fee=$%.4f  cid=%s",
                 asset, side.upper(), price_cents, n_contracts, fee, cid)
        _log_trade(result)
        return result

    body = {
        "ticker":          ticker,
        "side":            side,
        "action":          "buy",
        "count":           n_contracts,
        "time_in_force":   "gtc",
        "post_only":       True,
        "client_order_id": cid,
    }
    # Set price field: yes_price for YES side, no_price for NO side
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    try:
        resp = _post_order(body)
        order = resp.get("order", {})
        oid   = order.get("order_id")
        status = order.get("status", "unknown")
        result.update({"status": status, "order_id": oid})
        POSITIONS.record_open_order(cid, {
            "asset": asset, "ticker": ticker,
            "side": side, "price_cents": price_cents,
            "count": n_contracts, "order_id": oid,
            "ts": result["ts"],
        })
        log.info("MAKER placed  %s %s  %d¢×%d  id=%s  status=%s",
                 asset, side.upper(), price_cents, n_contracts, oid, status)
    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)
        log.error("MAKER order failed: %s", e)

    _log_trade(result)
    return result

def cancel_quote(client_order_id: str, order_id: str) -> bool:
    """Cancel a resting maker order. Returns True if cancelled."""
    if DRY_RUN:
        log.info("[DRY RUN] CANCEL  cid=%s  oid=%s", client_order_id, order_id)
        POSITIONS.remove_open_order(client_order_id)
        return True
    try:
        _cancel_order(order_id)
        POSITIONS.remove_open_order(client_order_id)
        log.info("CANCELLED  oid=%s", order_id)
        return True
    except Exception as e:
        log.warning("Cancel failed for %s: %s", order_id, e)
        return False

def cancel_all_for_ticker(ticker: str):
    """Cancel all open maker orders for a given ticker."""
    orders = POSITIONS.open_orders_for(ticker)
    for o in orders:
        cancel_quote(o["client_order_id"], o.get("order_id", ""))

# ── Arb execution helpers ─────────────────────────────────────────────────────
def _unwind(asset: str, ticker: str, snap, side: str, count: int, entry_price: int) -> bool:
    """
    Sell back an unhedged leg via IOC with three escalating price attempts.

    Attempt 1: bid − SWEEP_CENTS          (best-effort, preserves most value)
    Attempt 2: bid − SWEEP_CENTS * 4      (wider sweep if book moved)
    Attempt 3: price = 1¢                 (nuclear — accepts any fill to close the position)

    Using stale bid from the snapshot is intentional for attempt 1; attempts 2/3
    are aggressive enough to overcome any reasonable post-order book movement.
    """
    if side == "yes":
        ref_bid     = snap.yes_bid or entry_price
        price_field = "yes_price"
    else:
        ref_bid     = snap.no_bid or entry_price
        price_field = "no_price"

    remaining = count
    # Prices for each attempt: escalate to 1¢ (nuclear) on the last try
    for attempt, price in enumerate([
        max(1, ref_bid - SWEEP_CENTS),
        max(1, ref_bid - SWEEP_CENTS * 4),
        1,
    ]):
        if remaining <= 0:
            break
        cid = _make_client_id(asset, f"{side}-unwind-{attempt}")
        try:
            resp   = _post_order({
                "ticker":          ticker,
                "side":            side,
                "action":          "sell",
                "count":           remaining,
                price_field:       price,
                "time_in_force":   "immediate_or_cancel",
                "client_order_id": cid,
            })
            filled    = int(float(resp.get("order", {}).get("fill_count_fp", "0") or "0"))
            remaining -= filled
            log.warning("↩ unwind attempt %d  %s %s  price=%d¢  filled=%d/%d  left=%d",
                        attempt + 1, asset, side.upper(), price, filled, count, remaining)
        except Exception as e:
            log.error("↩ unwind attempt %d  %s %s  error: %s", attempt + 1, asset, side.upper(), e)

    if remaining > 0:
        # All three attempts exhausted — record the remaining contracts as an open position
        log.error("UNHEDGED %s %s: %d/%d contracts not unwound — recorded in positions",
                  asset, side.upper(), remaining, count)
        POSITIONS.record_fill(ticker, side, entry_price, remaining,
                              _make_client_id(asset, f"{side}-unhedged"))
        return False

    log.warning("↩ %s %s fully unwound (%d contracts)", asset, side.upper(), count)
    return True


# ── Arb execution (taker orders) ──────────────────────────────────────────────
def execute_arb(snap, mkt: dict, bot=None) -> Optional[dict]:
    """
    Execute an arb trade: buy YES + NO simultaneously using IOC taker orders.
    Only called when taker_gap > 0 (profit after taker fees).

    snap: MarketSnapshot
    mkt:  market dict from engine.markets[asset]
    """
    asset  = snap.asset
    ticker = snap.ticker
    # Cap order size to available book depth on both sides.
    # yes_ask_depth = NO bids at best_no_bid (the counterpart for buying YES).
    # no_ask_depth  = YES bids at best_yes_bid (the counterpart for buying NO).
    n = min(
        TRADE_SIZE_CONTRACTS,
        snap.yes_ask_depth,
        snap.no_ask_depth,
    )

    result = {
        "ts":        datetime.now(timezone.utc).isoformat(),
        "asset":     asset,
        "ticker":    ticker,
        "yes_ask":   snap.yes_ask,
        "no_ask":    snap.no_ask,
        "taker_gap": float(snap.taker_gap) if snap.taker_gap else None,
        "n":         n,
        "dry_run":   DRY_RUN,
        "status":    "pending",
        "error":     None,
        "yes_order_id": None,
        "no_order_id":  None,
    }

    if snap.yes_ask is None or snap.no_ask is None:
        result["status"] = "skipped_no_price"
        return result

    if not snap.is_arb:
        result["status"] = "skipped_no_gap"
        return result

    # Skip near-expiry windows — order books are thin and IOC fills rarely succeed
    if snap.secs_left < MIN_SECS_LEFT:
        result["status"] = "skipped_near_expiry"
        result["error"]  = f"only {snap.secs_left}s left in window (min {MIN_SECS_LEFT}s)"
        return result

    # Skip if there is no depth at the ask level on either side
    if n < 1:
        result["status"] = "skipped_no_depth"
        result["error"]  = (f"yes_depth={snap.yes_ask_depth} "
                            f"no_depth={snap.no_ask_depth} — book too thin to fill")
        return result

    # Position limits (use depth-capped n)
    if POSITIONS.total_exposure(ticker) + n * 2 > MAX_POSITION_CONTRACTS:
        result["status"] = "skipped_position_limit"
        result["error"]  = f"exposure {POSITIONS.total_exposure(ticker)} + {n*2} > max {MAX_POSITION_CONTRACTS}"
        return result

    # ── Guardrail 1: per-asset cooldown + circuit breaker ─────────────────────
    skip = _check_cooldown(asset)
    if skip:
        result["status"] = f"skipped_{skip}"
        return result

    # ── Guardrail 2: global concurrency limit ─────────────────────────────────
    if not _ARB_LOCK.acquire(blocking=False):
        result["status"] = "skipped_max_concurrent"
        return result

    # Arm cooldown now that we hold the execution slot
    _arm_cooldown(asset)
    try:
        # ── Guardrail 3: session loss circuit breaker (live only) ─────────────
        if _check_session_loss():
            result["status"] = "skipped_circuit_breaker"
            _log_trade(result)
            return result

        if DRY_RUN:
            yes_fee = total_taker_fee(snap.yes_ask, n)
            no_fee  = total_taker_fee(snap.no_ask, n)
            result.update({
                "status":    "dry_run",
                "yes_fee":   float(yes_fee),
                "no_fee":    float(no_fee),
                "total_cost": float(Decimal(snap.yes_ask * n) / 100 +
                                   Decimal(snap.no_ask  * n) / 100 + yes_fee + no_fee),
                "est_profit": float(snap.taker_gap * n / 100),
            })
            log.info("[DRY RUN] ARB  %s  YES=%d¢  NO=%d¢  gap=+%.3f¢  n=%d",
                     asset, snap.yes_ask, snap.no_ask, float(snap.taker_gap), n)
            _log_trade(result)
            return result

        # Compute per-leg sweep: bid above the ask to cross multiple book levels.
        # Limit sweep so that (yes_ask + sweep) + (no_ask + sweep) stays profitable.
        gap_cents = float(snap.taker_gap)
        max_sweep = max(0.0, (gap_cents - MIN_PROFIT_AFTER_SWEEP) / 2)
        sweep     = min(SWEEP_CENTS, int(max_sweep))

        yes_order_price = snap.yes_ask + sweep
        no_order_price  = snap.no_ask  + sweep

        result["sweep"] = sweep   # record for logging

        # Live: fire YES + NO simultaneously via two threads, then reconcile fills.
        yes_cid = _make_client_id(asset, "yes")
        no_cid  = _make_client_id(asset, "no")

        yes_body = {
            "ticker":          ticker,
            "side":            "yes",
            "action":          "buy",
            "count":           n,
            "yes_price":       yes_order_price,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": yes_cid,
        }
        no_body = {
            "ticker":          ticker,
            "side":            "no",
            "action":          "buy",
            "count":           n,
            "no_price":        no_order_price,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": no_cid,
        }

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                yes_f = pool.submit(_post_order, yes_body)
                no_f  = pool.submit(_post_order, no_body)
            # Both futures are resolved after the context manager exits.

            yes_resp = yes_exc = None
            no_resp  = no_exc  = None
            try:
                yes_resp = yes_f.result()
            except Exception as e:
                yes_exc = e
                log.error("YES order API error: %s", e)
            try:
                no_resp = no_f.result()
            except Exception as e:
                no_exc = e
                log.error("NO order API error: %s", e)

            yes_order = (yes_resp or {}).get("order", {})
            no_order  = (no_resp  or {}).get("order", {})
            result["yes_order_id"] = yes_order.get("order_id")
            result["no_order_id"]  = no_order.get("order_id")

            yes_filled = int(float(yes_order.get("fill_count_fp", "0") or "0"))
            no_filled  = int(float(no_order.get("fill_count_fp",  "0") or "0"))
            log.info("ARB concurrent  %s  YES %d/%d  NO %d/%d  (sweep=%d¢)",
                     asset, yes_filled, n, no_filled, n, sweep)

            hedged     = min(yes_filled, no_filled)
            yes_excess = yes_filled - hedged
            no_excess  = no_filled  - hedged

            # Unwind any unhedged surplus on either leg; track whether all excess was cleared
            all_unwound = True
            had_excess  = (yes_excess > 0 or no_excess > 0)
            if yes_excess > 0:
                if not _unwind(asset, ticker, snap, "yes", yes_excess, yes_order_price):
                    all_unwound = False
            if no_excess > 0:
                if not _unwind(asset, ticker, snap, "no",  no_excess,  no_order_price):
                    all_unwound = False

            if hedged < 1:
                if had_excess and all_unwound:
                    result["status"] = "unwound_partial"
                    result["error"]  = (
                        f"No hedged pairs: YES={yes_filled}/{n} NO={no_filled}/{n}; "
                        f"excess positions unwound cleanly"
                    )
                    log.warning("ARB no hedged pairs — unwound cleanly for %s", asset)
                elif had_excess:
                    result["status"] = "partial_unhedged"
                    result["error"]  = (
                        f"Excess positions not fully unwound: "
                        f"YES={yes_filled}/{n} NO={no_filled}/{n}"
                    )
                    log.error("UNHEDGED POSITION on %s: %s", asset, result["error"])
                else:
                    result["status"] = "error"
                    result["error"]  = (
                        f"Both legs unfilled: YES={yes_filled}/{n} NO={no_filled}/{n} "
                        f"(book moved before orders arrived)"
                    )
                _log_trade(result)
                return result

            result.update({
                "status":         "filled",
                "n":              hedged,
                "yes_price_paid": yes_order_price,
                "no_price_paid":  no_order_price,
            })
            POSITIONS.record_fill(ticker, "yes", yes_order_price, hedged, yes_cid)
            POSITIONS.record_fill(ticker, "no",  no_order_price,  hedged, no_cid)
            actual_gap = 100 - yes_order_price - no_order_price - float(
                total_taker_fee(yes_order_price, hedged) +
                total_taker_fee(no_order_price,  hedged)
            ) * 100 / hedged
            POSITIONS.realise_pnl(actual_gap * hedged / 100)
            log.info("✓ ARB FILLED  %s  YES=%d¢×%d  NO=%d¢×%d  sweep=%d¢  net=+%.3f¢",
                     asset, yes_order_price, hedged, no_order_price, hedged, sweep, actual_gap)

        except Exception as e:
            result["status"] = "error"
            result["error"]  = str(e)
            log.exception("ARB execution error: %s", e)

        _log_trade(result)
        return result

    finally:
        _ARB_LOCK.release()

# ── Quote manager ──────────────────────────────────────────────────────────────
class QuoteManager:
    """
    Manages resting maker quotes for one asset.
    Strategy:
      - Post YES bid at  mid - spread/2
      - Post NO  bid at  (100 - mid) - spread/2
      - Cancel + replace when mid moves > REQUOTE_THRESHOLD cents
      - Cancel all when market expires
    """
    def __init__(self, asset: str):
        self.asset            = asset
        self._lock            = threading.Lock()
        self._yes_quote: Optional[dict] = None   # active YES bid order
        self._no_quote:  Optional[dict] = None   # active NO  bid order
        self._last_mid:  Optional[float] = None
        self._current_ticker: Optional[str] = None

    def update(self, snap, mkt: dict):
        """
        Called on every price update. Decides whether to place/replace quotes.
        snap: MarketSnapshot
        mkt:  market dict
        """
        if snap.mid is None:
            return

        with self._lock:
            # Market rolled to a new ticker — clear local quote state without
            # trying to cancel (the old market is already expired on Kalshi's side).
            if self._current_ticker and self._current_ticker != snap.ticker:
                for q in [self._yes_quote, self._no_quote]:
                    if q:
                        POSITIONS.remove_open_order(q.get("client_order_id", ""))
                self._yes_quote = None
                self._no_quote  = None
                self._last_mid  = None
            self._current_ticker = snap.ticker

            mid = snap.mid
            half_spread = SPREAD_TARGET_CENTS / 2

            # Our desired quotes
            yes_bid_target = max(1, round(mid - half_spread))
            no_bid_target  = max(1, round((100 - mid) - half_spread))

            # Clamp to valid range
            yes_bid_target = min(yes_bid_target, 99)
            no_bid_target  = min(no_bid_target,  99)

            need_requote = (
                self._last_mid is None
                or abs(mid - self._last_mid) >= REQUOTE_THRESHOLD
                or self._yes_quote is None
                or self._no_quote  is None
            )

            if not need_requote:
                return

            # Check position limits before placing
            ticker = snap.ticker
            yes_pos = POSITIONS.yes_position(ticker)
            no_pos  = POSITIONS.no_position(ticker)

            # Cancel existing quotes before replacing
            if self._yes_quote and self._yes_quote.get("order_id"):
                cancel_quote(
                    self._yes_quote["client_order_id"],
                    self._yes_quote["order_id"]
                )
                self._yes_quote = None

            if self._no_quote and self._no_quote.get("order_id"):
                cancel_quote(
                    self._no_quote["client_order_id"],
                    self._no_quote["order_id"]
                )
                self._no_quote = None

            self._last_mid = mid

            # Only place YES quote if under position limit
            if yes_pos < MAX_POSITION_CONTRACTS:
                r = place_maker_quote(
                    self.asset, ticker, "yes",
                    yes_bid_target, TRADE_SIZE_CONTRACTS
                )
                if r.get("status") not in ("error",):
                    self._yes_quote = r

            # Only place NO quote if under position limit
            if no_pos < MAX_POSITION_CONTRACTS:
                r = place_maker_quote(
                    self.asset, ticker, "no",
                    no_bid_target, TRADE_SIZE_CONTRACTS
                )
                if r.get("status") not in ("error",):
                    self._no_quote = r

    def cancel_all(self, ticker: str):
        """Cancel all quotes (call on market expiry)."""
        with self._lock:
            for q in [self._yes_quote, self._no_quote]:
                if q and q.get("order_id"):
                    cancel_quote(q["client_order_id"], q["order_id"])
            self._yes_quote = None
            self._no_quote  = None
            self._last_mid  = None

# ── Trade log ─────────────────────────────────────────────────────────────────
def _log_trade(entry: dict):
    try:
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass
