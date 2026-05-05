"""
trader.py — Kalshi Momentum Order Execution

ORDER BODY (POST /portfolio/orders)
  {
    "ticker":          "KXBTCD-15MIN-1234567890",
    "side":            "yes" | "no",
    "action":          "buy" | "sell",
    "count":           5,
    "yes_price":       87,             # cents, integer 1-99
    "time_in_force":   "immediate_or_cancel",
    "client_order_id": "mm-btc-mom-yes-1234567890",
  }
"""

import os
import json
import time
import uuid
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from engine import (
    kalshi_get, _auth_headers, API_BASE, SESSION, ORDER_TIMEOUT,
    TRADE_SIZE_CONTRACTS, DRY_RUN, ASSETS, WINDOW_SECS,
)

log = logging.getLogger("kalshi.trader")

# ── Config ────────────────────────────────────────────────────────────────────
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))
TRADES_FILE    = Path(os.getenv("TRADES_FILE",    "trades.jsonl"))

# ── Momentum config ───────────────────────────────────────────────────────────
MOMENTUM_ENTRY_THRESHOLD      = int(os.getenv("MOMENTUM_ENTRY_THRESHOLD",      "85"))
MOMENTUM_ENTRY_MAX            = int(os.getenv("MOMENTUM_ENTRY_MAX",            "95"))
MOMENTUM_TAKE_PROFIT          = int(os.getenv("MOMENTUM_TAKE_PROFIT",          "95"))
MOMENTUM_ENTRY_START          = int(os.getenv("MOMENTUM_ENTRY_START",          "780"))
MOMENTUM_ENTRY_END            = int(os.getenv("MOMENTUM_ENTRY_END",            "840"))
MOMENTUM_REVERSAL_DROP        = int(os.getenv("MOMENTUM_REVERSAL_DROP",        "10"))
MOMENTUM_HEDGE_MIN_GAP        = int(os.getenv("MOMENTUM_HEDGE_MIN_GAP",         "5"))
MOMENTUM_TP_COOLDOWN          = float(os.getenv("MOMENTUM_TP_COOLDOWN",        "3.0"))
MOMENTUM_HEDGE_COOLDOWN       = float(os.getenv("MOMENTUM_HEDGE_COOLDOWN",     "3.0"))
MOMENTUM_ENTRY_RETRY_MAX      = int(os.getenv("MOMENTUM_ENTRY_RETRY_MAX",      "90"))
MOMENTUM_ENTRY_RETRY_COOLDOWN = float(os.getenv("MOMENTUM_ENTRY_RETRY_COOLDOWN","2.0"))
MOMENTUM_STOP_LOSS            = int(os.getenv("MOMENTUM_STOP_LOSS",            "60"))
MOMENTUM_STOP_LOSS_COOLDOWN   = float(os.getenv("MOMENTUM_STOP_LOSS_COOLDOWN", "3.0"))

# ── Position book ─────────────────────────────────────────────────────────────
class PositionBook:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "open_orders": {},
            "positions":   {},
            "realised_pnl": 0.0,
            "total_fills":  0,
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

    def record_fill(self, ticker: str, side: str, price_cents: int,
                    count: int, client_order_id: str):
        with self._lock:
            pos = self._data["positions"].setdefault(ticker, {
                "yes_contracts": 0, "no_contracts": 0,
                "avg_yes_cost": 0.0, "avg_no_cost": 0.0,
            })
            key, avg_k   = f"{side}_contracts", f"avg_{side}_cost"
            prev_n       = pos[key]
            prev_avg     = pos[avg_k]
            new_n        = prev_n + count
            if new_n > 0:
                pos[avg_k] = (prev_avg * prev_n + (price_cents / 100) * count) / new_n
            pos[key] = new_n
            self._data["total_fills"] += 1
            self._data["open_orders"].pop(client_order_id, None)
            self._save()

    def realise_pnl(self, amount: float):
        with self._lock:
            self._data["realised_pnl"] += amount
            self._save()

POSITIONS = PositionBook()

# ── REST order helpers ────────────────────────────────────────────────────────
def _post_order(body: dict) -> dict:
    path    = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    url     = f"{API_BASE}/portfolio/orders"
    t0 = time.perf_counter()
    r  = SESSION.post(url, json=body, headers=headers, timeout=ORDER_TIMEOUT)
    ms = (time.perf_counter() - t0) * 1000
    log.info("ORDER rtt=%.0fms  side=%s  status=%d", ms, body.get("side", "?"), r.status_code)
    if not r.ok:
        log.error("ORDER body sent: %s", json.dumps(body))
        log.error("ORDER error response: %s", r.text)
    r.raise_for_status()
    return r.json()

def _make_client_id(asset: str, tag: str) -> str:
    return f"mm-{asset.lower()}-{tag}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

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

    Phase 1 — Entry [780–840 s elapsed / 13:00–14:00]:
      IOC buy whichever side has ask >= MOMENTUM_ENTRY_THRESHOLD (85¢) and
      < MOMENTUM_ENTRY_MAX (95¢). Higher ask wins if both qualify.
      Retries every MOMENTUM_ENTRY_RETRY_COOLDOWN s while price <= MOMENTUM_ENTRY_RETRY_MAX.

    Phase 2 — Take profit [last 60 s, secs_left <= 60]:
      IOC sell when bid >= max(MOMENTUM_TAKE_PROFIT, entry_price + 1).
      The max() guard ensures we never TP at a loss.

    Phase 3 — Reversal hedge [last 60 s]:
      Triggered when entry-side bid drops >= MOMENTUM_REVERSAL_DROP from entry.
      Buys opposite side IOC if ask <= (100 - entry_price - MOMENTUM_HEDGE_MIN_GAP),
      locking in a guaranteed profit gap regardless of outcome.

    If neither TP nor hedge fires, the position holds to resolution.
    Kalshi settles at 100¢ (win) or 0¢ (loss).
    """

    def __init__(self, asset: str, on_log=None, on_update=None):
        self.asset      = asset
        self._lock      = threading.Lock()
        self._on_log    = on_log    or (lambda ic, msg: None)
        self._on_update = on_update or (lambda asset, pos: None)

        self._position: Optional[dict] = None
        self._entry_attempted     = False
        self._entry_last_ts       = 0.0
        self._pre_entry_logged    = False
        self._entry_window_logged = False
        self._tp_last_ts          = 0.0
        self._tp_window_logged    = False
        self._hedge_last_ts       = 0.0
        self._hedge_attempted     = False
        self._sl_last_ts          = 0.0
        self._sl_attempted        = False
        self._current_ticker: Optional[str] = None

    # ── Public ────────────────────────────────────────────────────────────────

    def update(self, snap, mkt: dict):
        elapsed = WINDOW_SECS - snap.secs_left
        with self._lock:
            if self._current_ticker and self._current_ticker != snap.ticker:
                self._reset_window()
                mins, secs = snap.secs_left // 60, snap.secs_left % 60
                self._on_log("🕐", (
                    f"{self.asset} — new 15-min window detected  "
                    f"({mins}m {secs}s remaining). "
                    f"Waiting for the 13th-minute entry window."
                ))
            self._current_ticker = snap.ticker
            now = time.time()
            pos = self._position

            # Phase 1: entry window
            if pos is None and not self._entry_attempted:
                secs_to_window = max(0, MOMENTUM_ENTRY_START - elapsed)
                if elapsed < MOMENTUM_ENTRY_START and not self._pre_entry_logged:
                    if secs_to_window <= 120:
                        self._pre_entry_logged = True
                        self._on_log("⏰", (
                            f"{self.asset} — entry window opens in ~{secs_to_window}s. "
                            f"Prices: YES ask {snap.yes_ask}¢  NO ask {snap.no_ask}¢"
                        ))
                if MOMENTUM_ENTRY_START <= elapsed <= MOMENTUM_ENTRY_END:
                    if not self._entry_window_logged:
                        self._entry_window_logged = True
                        self._on_log("👀", (
                            f"{self.asset} — now in the 13th-minute entry window. "
                            f"Looking for YES ask or NO ask ≥ {MOMENTUM_ENTRY_THRESHOLD}¢. "
                            f"Currently: YES ask {snap.yes_ask}¢  NO ask {snap.no_ask}¢"
                        ))
                    if now - self._entry_last_ts >= MOMENTUM_ENTRY_RETRY_COOLDOWN:
                        self._try_entry(snap)

            # Phase 2: take profit
            pos = self._position
            if (pos is not None and pos["phase"] == "holding"
                    and snap.secs_left <= 60
                    and now - self._tp_last_ts >= MOMENTUM_TP_COOLDOWN):
                if not self._tp_window_logged:
                    self._tp_window_logged = True
                    side        = pos["side"]
                    current_bid = snap.yes_bid if side == "yes" else snap.no_bid
                    tp_target   = max(MOMENTUM_TAKE_PROFIT, pos["entry_price"] + 1)
                    self._on_log("⏱", (
                        f"{self.asset} — last {snap.secs_left}s! "
                        f"Holding {side.upper()} entered at {pos['entry_price']}¢. "
                        f"Watching for bid ≥ {tp_target}¢ to take profit "
                        f"(currently {current_bid}¢)."
                    ))
                self._try_take_profit(snap)

            # Stop loss: exit + hedge when bid drops to MOMENTUM_STOP_LOSS (any secs_left)
            pos = self._position
            if (pos is not None and pos["phase"] == "holding"
                    and not self._sl_attempted
                    and now - self._sl_last_ts >= MOMENTUM_STOP_LOSS_COOLDOWN):
                self._try_stop_loss(snap)

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
        with self._lock:
            if self._current_ticker == ticker and self._position:
                p = self._position
                if p["phase"] == "holding":
                    self._on_log("⏰", (
                        f"{self.asset} — window closed with position open. "
                        f"Held {p['side'].upper()} at {p['entry_price']}¢ × {p['count']} to resolution. "
                        f"Kalshi will settle at 100¢ (win) or 0¢ (loss)."
                    ))
            self._reset_window()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reset_window(self):
        self._position            = None
        self._entry_attempted     = False
        self._entry_last_ts       = 0.0
        self._pre_entry_logged    = False
        self._entry_window_logged = False
        self._tp_last_ts          = 0.0
        self._tp_window_logged    = False
        self._hedge_last_ts       = 0.0
        self._hedge_attempted     = False
        self._sl_last_ts          = 0.0
        self._sl_attempted        = False

    def _notify(self):
        self._on_update(self.asset, dict(self._position) if self._position else None)

    def _try_entry(self, snap):
        """
        IOC buy the dominant side when its ASK is >= threshold.
        Uses ask (not bid) because buying YES as a taker costs yes_ask = 100 - no_bid.
        Using yes_bid would place the order below the spread → no fill.
        """
        yes_ok = snap.yes_ask is not None and snap.yes_ask >= MOMENTUM_ENTRY_THRESHOLD
        no_ok  = snap.no_ask  is not None and snap.no_ask  >= MOMENTUM_ENTRY_THRESHOLD
        if not yes_ok and not no_ok:
            self._on_log("👀", (
                f"{self.asset} — watching: YES ask {snap.yes_ask}¢  NO ask {snap.no_ask}¢ "
                f"— need ≥ {MOMENTUM_ENTRY_THRESHOLD}¢, not there yet. "
                f"{snap.secs_left}s left in window."
            ))
            return

        if yes_ok and no_ok:
            side = "yes" if snap.yes_ask >= snap.no_ask else "no"
        elif yes_ok:
            side = "yes"
        else:
            side = "no"

        price = snap.yes_ask if side == "yes" else snap.no_ask

        if price >= MOMENTUM_ENTRY_MAX:
            self._entry_attempted = True
            self._on_log("⏭", (
                f"MOMENTUM ENTRY {self.asset} {side.upper()} skipped — "
                f"ask={price}¢ >= max={MOMENTUM_ENTRY_MAX}¢ "
                f"(bad risk/reward: TP target already below entry)"
            ))
            return

        n   = TRADE_SIZE_CONTRACTS
        cid = _make_client_id(self.asset, f"mom-{side}")

        self._on_log("🎯", (
            f"MOMENTUM ENTRY {self.asset} {side.upper()} at {price}¢  "
            f"(elapsed={WINDOW_SECS - snap.secs_left}s  secs_left={snap.secs_left})"
        ))

        if DRY_RUN:
            self._entry_attempted = True
            self._position = {
                "ticker":      snap.ticker,
                "side":        side,
                "entry_price": price,
                "count":       n,
                "entry_ts":    datetime.now(timezone.utc).isoformat(),
                "phase":          "holding",
                "hedge_price":    None,
                "hedge_count":    None,
                "sl_exit_price":  None,
                "sl_hedge_price": None,
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

        self._entry_last_ts = time.time()
        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._entry_attempted = True
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
                secs_to_tp = max(0, snap.secs_left - 60)
                self._on_log("✅", (
                    f"{self.asset} — entered {side.upper()} at {price}¢ × {filled} contracts. "
                    f"Holding position. TP window opens in ~{secs_to_tp}s."
                ))
                self._notify()
            else:
                retryable  = MOMENTUM_ENTRY_THRESHOLD <= price <= MOMENTUM_ENTRY_RETRY_MAX
                if not retryable:
                    self._entry_attempted = True
                retry_note = (
                    f"retrying in {MOMENTUM_ENTRY_RETRY_COOLDOWN:.0f}s"
                    if retryable else "price left retry range; no retry"
                )
                self._on_log("⏸", (
                    f"MOMENTUM ENTRY {self.asset} {side.upper()} {price}¢ "
                    f"— IOC no fill ({retry_note})"
                ))
        except Exception as e:
            self._on_log("✗", f"MOMENTUM ENTRY error {self.asset}: {e}")

    def _try_take_profit(self, snap):
        self._tp_last_ts = time.time()
        pos  = self._position
        side = pos["side"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        tp_target   = max(MOMENTUM_TAKE_PROFIT, pos["entry_price"] + 1)
        if current_bid is None or current_bid < tp_target:
            if current_bid is not None:
                self._on_log("⏳", (
                    f"{self.asset} — bid now {current_bid}¢, need {tp_target}¢ to take profit "
                    f"({tp_target - current_bid}¢ away). {snap.secs_left}s to resolution."
                ))
            return

        n          = pos["count"]
        sell_price = current_bid
        cid        = _make_client_id(self.asset, f"mom-tp-{side}")
        profit_est = (sell_price - pos["entry_price"]) * n / 100

        self._on_log("💰", (
            f"{self.asset} — bid hit {sell_price}¢! Taking profit on "
            f"{side.upper()} position (entered at {pos['entry_price']}¢). "
            f"Est. profit: +${profit_est:.4f}"
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
                    f"{self.asset} — took profit! Sold {side.upper()} at {sell_price}¢ × {filled} "
                    f"(entry {pos['entry_price']}¢)  pnl=+${pnl:.4f}"
                ))
                self._notify()
            else:
                self._on_log("⏸", f"{self.asset} — TP order at {sell_price}¢ got no fill, will retry.")
        except Exception as e:
            self._on_log("✗", f"MOMENTUM TP error {self.asset}: {e}")

    def _check_reversal_hedge(self, snap):
        """
        Buy the opposite side when entry-side bid has dropped >= REVERSAL_DROP and
        opposite ask <= (100 - entry_price - HEDGE_MIN_GAP), locking in guaranteed profit.
        Uses entry_price for the threshold so combined cost can never exceed what was paid.
        """
        self._hedge_last_ts = time.time()
        pos         = self._position
        side        = pos["side"]
        entry_price = pos["entry_price"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or entry_price - current_bid < MOMENTUM_REVERSAL_DROP:
            return

        hedge_side      = "no"  if side == "yes" else "yes"
        hedge_ask       = snap.no_ask if side == "yes" else snap.yes_ask
        hedge_threshold = 100 - entry_price - MOMENTUM_HEDGE_MIN_GAP

        if hedge_ask is None or hedge_ask >= hedge_threshold:
            self._on_log("↘", (
                f"MOMENTUM REVERSAL {self.asset}  {side.upper()} dropped "
                f"{entry_price - current_bid}¢ (entry={entry_price}¢ → now={current_bid}¢)  "
                f"hedge {hedge_side.upper()} ask={hedge_ask}¢  max={hedge_threshold - 1}¢ — too expensive"
            ))
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
                    f"{hedge_ask}¢ × {filled}  combined={total_cost}¢  locked_gap=+{locked_gap}¢"
                ))
                self._notify()
            else:
                self._hedge_attempted = False
                self._on_log("⏸", (
                    f"MOMENTUM HEDGE {self.asset} {hedge_side.upper()} {hedge_ask}¢ — no fill"
                ))
        except Exception as e:
            self._hedge_attempted = False
            self._on_log("✗", f"MOMENTUM HEDGE error {self.asset}: {e}")

    def _try_stop_loss(self, snap):
        """
        Exit when entry-side bid drops to MOMENTUM_STOP_LOSS (60¢).
        Sells the entry side IOC at current bid, then immediately buys the opposite
        side at the implied ask (100 - bid = 40¢) to recover via reversal.
        The sell is mandatory; the hedge buy is best-effort (one attempt, no retry).
        """
        self._sl_last_ts = time.time()
        pos         = self._position
        side        = pos["side"]
        entry_price = pos["entry_price"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or current_bid > MOMENTUM_STOP_LOSS:
            return

        n          = pos["count"]
        sl_price   = current_bid
        loss_est   = (sl_price - entry_price) * n / 100
        hedge_side = "no"  if side == "yes" else "yes"
        hedge_ask  = 100 - current_bid   # implied opposite ask (40¢ when bid = 60¢)

        self._on_log("🔴", (
            f"STOP LOSS {self.asset} {side.upper()} bid hit {sl_price}¢  "
            f"(entry={entry_price}¢  est. loss=~${abs(loss_est):.4f})  "
            f"Exiting and buying {hedge_side.upper()} at {hedge_ask}¢."
        ))

        if DRY_RUN:
            self._sl_attempted = True
            POSITIONS.realise_pnl(loss_est)
            self._position["phase"]         = "stop_loss"
            self._position["sl_exit_price"] = sl_price
            self._position["sl_hedge_price"] = hedge_ask
            self._on_log("📋", (
                f"[DRY RUN] STOP LOSS {self.asset} sell {side.upper()} {sl_price}¢ × {n}  "
                f"pnl=${loss_est:.4f}  hedge buy {hedge_side.upper()} {hedge_ask}¢ × {n}"
            ))
            self._notify()
            return

        # Step 1: sell the entry side (stop loss exit)
        cid_sl    = _make_client_id(self.asset, f"mom-sl-{side}")
        body_sell = {
            "ticker":          snap.ticker,
            "side":            side,
            "action":          "sell",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid_sl,
        }
        body_sell["yes_price" if side == "yes" else "no_price"] = sl_price

        try:
            resp   = _post_order(body_sell)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._sl_attempted = True
                pnl = (sl_price - entry_price) * filled / 100
                POSITIONS.realise_pnl(pnl)
                self._position["phase"]         = "stop_loss"
                self._position["sl_exit_price"] = sl_price
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_stop_loss",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "sl_price": sl_price,
                    "entry_price": entry_price, "count": filled, "pnl": pnl,
                })
                self._on_log("🔴", (
                    f"STOP LOSS EXECUTED {self.asset} sold {side.upper()} at {sl_price}¢ × {filled}  "
                    f"(entry={entry_price}¢)  pnl=${pnl:.4f}"
                ))
                self._notify()
                # Step 2: buy the opposite side to recover (best-effort)
                self._buy_stop_loss_hedge(snap, hedge_side, hedge_ask, filled)
            else:
                self._on_log("⏸", (
                    f"STOP LOSS {self.asset} sell {side.upper()} at {sl_price}¢ — no fill, retrying."
                ))
        except Exception as e:
            self._on_log("✗", f"STOP LOSS error {self.asset}: {e}")

    def _buy_stop_loss_hedge(self, snap, hedge_side: str, hedge_ask: int, count: int):
        """Buy the opposite side after a stop loss exit. Single attempt, no retry."""
        cid  = _make_client_id(self.asset, f"mom-sl-hedge-{hedge_side}")
        body = {
            "ticker":          snap.ticker,
            "side":            hedge_side,
            "action":          "buy",
            "count":           count,
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
                self._position["sl_hedge_price"] = hedge_ask
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_sl_hedge",
                    "asset": self.asset, "ticker": snap.ticker,
                    "hedge_side": hedge_side, "hedge_price": hedge_ask, "count": filled,
                })
                self._on_log("🛡", (
                    f"STOP LOSS HEDGE FILLED {self.asset} bought {hedge_side.upper()} "
                    f"at {hedge_ask}¢ × {filled}"
                ))
                self._notify()
            else:
                self._on_log("⏸", (
                    f"STOP LOSS HEDGE {self.asset} {hedge_side.upper()} {hedge_ask}¢ — no fill"
                ))
        except Exception as e:
            self._on_log("✗", f"STOP LOSS HEDGE error {self.asset}: {e}")
