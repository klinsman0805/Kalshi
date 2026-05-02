"""
trader.py — Kalshi Order Execution

ORDER BODY (POST /portfolio/orders)
─────────────────────────────────────
  {
    "ticker":          "KXBTCD-15MIN-1234567890",
    "side":            "yes" | "no",
    "action":          "buy" | "sell",
    "count":           5,              # integer contracts
    "yes_price":       48,             # cents, integer 1-99 (for YES side)
    "no_price":        52,             # cents, integer 1-99 (for NO side)
    "time_in_force":   "immediate_or_cancel",
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
    MIN_PRICE_CENTS, MAX_PRICE_CENTS,
    SWEEP_CENTS, MIN_PROFIT_AFTER_SWEEP,
    DRY_RUN, USE_DEMO, ASSETS, WINDOW_SECS,
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

# ── Momentum config ────────────────────────────────────────────────────────────
# Entry:  buy whichever side has bid >= MOMENTUM_ENTRY_THRESHOLD in the 14th minute.
# TP:     IOC sell when bid >= MOMENTUM_TAKE_PROFIT in the last 60 s.
# Hedge:  IOC buy opposite side when entry side dropped >= REVERSAL_DROP AND
#         opposite ask <= (100 - entry_price), keeping combined cost <= 100¢.
MOMENTUM_ENTRY_THRESHOLD = int(os.getenv("MOMENTUM_ENTRY_THRESHOLD", "85"))   # ¢ bid to trigger entry
MOMENTUM_TAKE_PROFIT     = int(os.getenv("MOMENTUM_TAKE_PROFIT",     "95"))   # ¢ bid at which to sell
MOMENTUM_ENTRY_START     = int(os.getenv("MOMENTUM_ENTRY_START",     "780"))  # window elapsed secs (13:00)
MOMENTUM_ENTRY_END       = int(os.getenv("MOMENTUM_ENTRY_END",       "840"))  # window elapsed secs (14:00)
MOMENTUM_REVERSAL_DROP   = int(os.getenv("MOMENTUM_REVERSAL_DROP",   "10"))   # ¢ drop from entry to begin watching for hedge
MOMENTUM_HEDGE_MIN_GAP   = int(os.getenv("MOMENTUM_HEDGE_MIN_GAP",    "5"))   # ¢ minimum net profit after both legs (entry + hedge < 100 - gap)
MOMENTUM_TP_COOLDOWN     = float(os.getenv("MOMENTUM_TP_COOLDOWN",   "3.0"))  # s between TP retries
MOMENTUM_HEDGE_COOLDOWN  = float(os.getenv("MOMENTUM_HEDGE_COOLDOWN","3.0"))  # s between hedge retries

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
    if not r.ok:
        log.error("ORDER body sent: %s", json.dumps(body))
        log.error("ORDER error response: %s", r.text)
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

# ── Trade log ─────────────────────────────────────────────────────────────────
def _log_trade(entry: dict):
    try:
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


# ── Momentum strategy ──────────────────────────────────────────────────────────
class MomentumTrader:
    """
    Near-expiry momentum strategy for Kalshi 15-min markets.

    Phase 1 — Entry [780–840 s elapsed / 13:00–14:00 into window]:
      IOC buy whichever side (YES or NO) has bid >= MOMENTUM_ENTRY_THRESHOLD (85¢).
      If both qualify, take the higher-priced side.  One attempt per window only.

    Phase 2 — Take profit [last 60 s, secs_left <= 60]:
      IOC sell when current bid for held side >= MOMENTUM_TAKE_PROFIT (95¢).
      Retries every MOMENTUM_TP_COOLDOWN seconds until filled or market expires.

    Phase 3 — Reversal hedge [last 60 s, only while holding]:
      Triggered when entry-side bid drops >= MOMENTUM_REVERSAL_DROP (10¢) from
      entry price.  Buys the opposite side via IOC only if its ask is <=
      (100 - entry_price), keeping combined cost <= 100¢ (arb guarantee).
      Uses the entry price — not the current price — for the threshold so the
      total outlay can never exceed what was paid for the initial leg.

    If neither TP nor hedge fires before expiry the position is held to
    resolution; Kalshi settles at 100¢ (win) or 0¢ (loss).
    """

    def __init__(self, asset: str, on_log=None, on_update=None):
        self.asset      = asset
        self._lock      = threading.Lock()
        self._on_log    = on_log    or (lambda ic, msg: None)
        self._on_update = on_update or (lambda asset, pos: None)

        self._position: Optional[dict] = None
        self._entry_attempted = False
        self._tp_last_ts      = 0.0
        self._hedge_last_ts   = 0.0
        self._hedge_attempted = False
        self._current_ticker: Optional[str] = None

    # ── Public ────────────────────────────────────────────────────────────────

    def update(self, snap, mkt: dict):
        """
        Called on every price update from the WS handler.
        Checks all three phases sequentially; places IOC orders inline (blocks
        briefly — acceptable since each phase fires at most once per window).
        """
        elapsed = WINDOW_SECS - snap.secs_left

        with self._lock:
            if self._current_ticker and self._current_ticker != snap.ticker:
                self._reset_window()
            self._current_ticker = snap.ticker

            now = time.time()
            pos = self._position

            # Phase 1: entry window
            if pos is None and not self._entry_attempted:
                if MOMENTUM_ENTRY_START <= elapsed <= MOMENTUM_ENTRY_END:
                    self._try_entry(snap)

            # Phase 2: take profit (refresh pos reference after possible entry)
            pos = self._position
            if (pos is not None and pos["phase"] == "holding"
                    and snap.secs_left <= 60
                    and now - self._tp_last_ts >= MOMENTUM_TP_COOLDOWN):
                self._try_take_profit(snap)

            # Phase 3: reversal hedge
            pos = self._position
            if (pos is not None and pos["phase"] == "holding"
                    and snap.secs_left <= 60
                    and not self._hedge_attempted
                    and now - self._hedge_last_ts >= MOMENTUM_HEDGE_COOLDOWN):
                self._check_reversal_hedge(snap)

    def get_position(self) -> Optional[dict]:
        with self._lock:
            return dict(self._position) if self._position else None

    def on_market_expire(self, ticker: str):
        """Call when the market window closes so held positions are logged."""
        with self._lock:
            if self._current_ticker == ticker and self._position:
                p = self._position
                if p["phase"] == "holding":
                    self._on_log("⏰", (
                        f"MOMENTUM {self.asset} held to resolution: "
                        f"{p['side'].upper()} {p['entry_price']}¢ × {p['count']} — "
                        f"Kalshi settles at 100¢ (win) or 0¢ (loss)"
                    ))
            self._reset_window()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reset_window(self):
        """Clear per-window state. Must hold _lock."""
        self._position        = None
        self._entry_attempted = False
        self._tp_last_ts      = 0.0
        self._hedge_last_ts   = 0.0
        self._hedge_attempted = False

    def _notify(self):
        """Push position update to app layer. Must hold _lock."""
        self._on_update(self.asset, dict(self._position) if self._position else None)

    def _try_entry(self, snap):
        """IOC buy at best qualifying bid. Must hold _lock."""
        yes_ok = snap.yes_bid is not None and snap.yes_bid >= MOMENTUM_ENTRY_THRESHOLD
        no_ok  = snap.no_bid  is not None and snap.no_bid  >= MOMENTUM_ENTRY_THRESHOLD
        if not yes_ok and not no_ok:
            return  # no qualifying side yet; will retry next tick within window

        self._entry_attempted = True  # one attempt per window regardless of outcome

        if yes_ok and no_ok:
            side = "yes" if snap.yes_bid >= snap.no_bid else "no"
        elif yes_ok:
            side = "yes"
        else:
            side = "no"

        price = snap.yes_bid if side == "yes" else snap.no_bid
        n     = TRADE_SIZE_CONTRACTS
        cid   = _make_client_id(self.asset, f"mom-{side}")

        self._on_log("🎯", (
            f"MOMENTUM ENTRY {self.asset} {side.upper()} at {price}¢  "
            f"(elapsed={WINDOW_SECS - snap.secs_left}s  secs_left={snap.secs_left})"
        ))

        if DRY_RUN:
            self._position = {
                "ticker":      snap.ticker,
                "side":        side,
                "entry_price": price,
                "count":       n,
                "entry_ts":    datetime.now(timezone.utc).isoformat(),
                "phase":       "holding",
                "hedge_price": None,
                "hedge_count": None,
            }
            self._on_log("📋", f"[DRY RUN] MOMENTUM {self.asset} {side.upper()} {price}¢ × {n}")
            self._notify()
            return

        body = {
            "ticker":          snap.ticker,
            "side":            side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if side == "yes" else "no_price"] = price

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._position = {
                    "ticker":      snap.ticker,
                    "side":        side,
                    "entry_price": price,
                    "count":       filled,
                    "entry_ts":    datetime.now(timezone.utc).isoformat(),
                    "phase":       "holding",
                    "hedge_price": None,
                    "hedge_count": None,
                }
                POSITIONS.record_fill(snap.ticker, side, price, filled, cid)
                _log_trade({
                    "ts": self._position["entry_ts"], "type": "momentum_entry",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "price": price, "count": filled,
                })
                self._on_log("✅", (
                    f"MOMENTUM ENTRY FILLED {self.asset} {side.upper()} {price}¢ × {filled}"
                ))
                self._notify()
            else:
                self._on_log("⏸", (
                    f"MOMENTUM ENTRY {self.asset} {side.upper()} {price}¢ — IOC no fill "
                    f"(book moved; no retry this window)"
                ))
        except Exception as e:
            self._on_log("✗", f"MOMENTUM ENTRY error {self.asset}: {e}")

    def _try_take_profit(self, snap):
        """IOC sell at current bid when bid >= TAKE_PROFIT target. Must hold _lock."""
        self._tp_last_ts = time.time()
        pos  = self._position
        side = pos["side"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or current_bid < MOMENTUM_TAKE_PROFIT:
            return  # target not reached yet; will retry after cooldown

        n          = pos["count"]
        sell_price = current_bid
        cid        = _make_client_id(self.asset, f"mom-tp-{side}")
        profit_est = (sell_price - pos["entry_price"]) * n / 100

        self._on_log("💰", (
            f"MOMENTUM TP {self.asset} {side.upper()}  "
            f"sell={sell_price}¢  entry={pos['entry_price']}¢  est_pnl=+${profit_est:.4f}"
        ))

        if DRY_RUN:
            POSITIONS.realise_pnl(profit_est)
            self._position["phase"] = "closed"
            self._on_log("📋", f"[DRY RUN] MOMENTUM TP {self.asset} {sell_price}¢ × {n}  pnl=+${profit_est:.4f}")
            self._notify()
            return

        body = {
            "ticker":          snap.ticker,
            "side":            side,
            "action":          "sell",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if side == "yes" else "no_price"] = sell_price

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                pnl = (sell_price - pos["entry_price"]) * filled / 100
                POSITIONS.realise_pnl(pnl)
                self._position["phase"] = "closed"
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_tp",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "sell_price": sell_price,
                    "entry_price": pos["entry_price"], "count": filled, "pnl": pnl,
                })
                self._on_log("✅", (
                    f"MOMENTUM TP SOLD {self.asset} {side.upper()} {sell_price}¢ × {filled}  "
                    f"pnl=+${pnl:.4f}"
                ))
                self._notify()
            else:
                self._on_log("⏸", f"MOMENTUM TP {self.asset} {side.upper()} {sell_price}¢ — no fill, retrying")
        except Exception as e:
            self._on_log("✗", f"MOMENTUM TP error {self.asset}: {e}")

    def _check_reversal_hedge(self, snap):
        """
        Buy the opposite side when:
          - Entry side bid has dropped >= REVERSAL_DROP from entry price
          - Opposite ask <= (100 - entry_price)  →  combined cost <= 100¢
        Uses entry_price for the threshold (not current price) so the hedge
        only fires when there is a genuine arb guarantee.
        Must hold _lock.
        """
        self._hedge_last_ts = time.time()
        pos         = self._position
        side        = pos["side"]
        entry_price = pos["entry_price"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or entry_price - current_bid < MOMENTUM_REVERSAL_DROP:
            return  # reversal not large enough yet

        hedge_side      = "no"  if side == "yes" else "yes"
        hedge_ask       = snap.no_ask if side == "yes" else snap.yes_ask
        # Require at least MOMENTUM_HEDGE_MIN_GAP¢ net profit so combined cost
        # is strictly less than (100 - gap)¢ — e.g. entry=85¢ + hedge<10¢ = <95¢
        # guaranteeing profit even after taker fees on the hedge leg.
        hedge_threshold = 100 - entry_price - MOMENTUM_HEDGE_MIN_GAP

        if hedge_ask is None or hedge_ask >= hedge_threshold:
            # Gap not wide enough (or no book); mark attempted to skip costly retries.
            self._hedge_attempted = True
            return

        self._hedge_attempted = True
        n          = pos["count"]
        cid        = _make_client_id(self.asset, f"mom-hedge-{hedge_side}")
        total_cost = entry_price + hedge_ask
        locked_gap = 100 - total_cost

        self._on_log("🛡", (
            f"MOMENTUM HEDGE {self.asset} buy {hedge_side.upper()} at {hedge_ask}¢  "
            f"(entry={entry_price}¢  max_hedge={hedge_threshold - 1}¢  "
            f"combined={total_cost}¢  locked_gap=+{locked_gap}¢)"
        ))

        if DRY_RUN:
            self._position["phase"]       = "hedged"
            self._position["hedge_price"] = hedge_ask
            self._position["hedge_count"] = n
            self._on_log("📋", (
                f"[DRY RUN] MOMENTUM HEDGE {self.asset} {hedge_side.upper()} {hedge_ask}¢ × {n}"
            ))
            self._notify()
            return

        body = {
            "ticker":          snap.ticker,
            "side":            hedge_side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if hedge_side == "yes" else "no_price"] = hedge_ask

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                POSITIONS.record_fill(snap.ticker, hedge_side, hedge_ask, filled, cid)
                self._position["phase"]       = "hedged"
                self._position["hedge_price"] = hedge_ask
                self._position["hedge_count"] = filled
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_hedge",
                    "asset": self.asset, "ticker": snap.ticker,
                    "hedge_side": hedge_side, "hedge_price": hedge_ask,
                    "entry_side": side, "entry_price": entry_price,
                    "count": filled, "total_cost": total_cost,
                })
                self._on_log("✅", (
                    f"MOMENTUM HEDGE FILLED {self.asset} {hedge_side.upper()} "
                    f"{hedge_ask}¢ × {filled}  combined={total_cost}¢  "
                    f"locked_gap=+{locked_gap}¢"
                ))
                self._notify()
            else:
                self._hedge_attempted = False  # allow one retry — gap may persist
                self._on_log("⏸", (
                    f"MOMENTUM HEDGE {self.asset} {hedge_side.upper()} {hedge_ask}¢ — no fill"
                ))
        except Exception as e:
            self._hedge_attempted = False
            self._on_log("✗", f"MOMENTUM HEDGE error {self.asset}: {e}")
