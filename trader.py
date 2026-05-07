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
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from engine import (
    kalshi_get, kalshi_delete, _auth_headers, API_BASE, SESSION, ORDER_TIMEOUT,
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
MOMENTUM_INSURANCE_THRESHOLD  = int(os.getenv("MOMENTUM_INSURANCE_THRESHOLD",   "5"))
MOMENTUM_INSURANCE_COUNT      = int(os.getenv("MOMENTUM_INSURANCE_COUNT",        "3"))
MOMENTUM_INSURANCE_COOLDOWN   = float(os.getenv("MOMENTUM_INSURANCE_COOLDOWN",  "3.0"))
MOMENTUM_WARN_THRESHOLD       = int(os.getenv("MOMENTUM_WARN_THRESHOLD",        "75"))
MOMENTUM_WARN_COUNT           = int(os.getenv("MOMENTUM_WARN_COUNT",             "5"))
MOMENTUM_WARN_COOLDOWN        = float(os.getenv("MOMENTUM_WARN_COOLDOWN",        "3.0"))
MOMENTUM_SL_MIN_AGE           = float(os.getenv("MOMENTUM_SL_MIN_AGE",           "10.0"))
MOMENTUM_ENTRY_HEDGE_COUNT    = int(os.getenv("MOMENTUM_ENTRY_HEDGE_COUNT",       "3"))
AI_ADVISOR_ENABLED            = os.getenv("AI_ADVISOR_ENABLED",  "false").lower() == "true"
AI_ADVISOR_LOOKBACK           = int(os.getenv("AI_ADVISOR_LOOKBACK", "20"))
AI_ADVISOR_MODEL              = os.getenv("AI_ADVISOR_MODEL", "claude-haiku-4-5-20251001")

HEDGING_ENABLED               = os.getenv("HEDGING_ENABLED",          "true").lower() == "true"
MOMENTUM_COUNTER_START        = int(os.getenv("MOMENTUM_COUNTER_START", "840"))
MOMENTUM_COUNTER_END          = int(os.getenv("MOMENTUM_COUNTER_END",   "885"))

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

def _cancel_order(order_id: str) -> bool:
    """Cancel a resting GTC order by order_id. Returns True if successful."""
    if not order_id or order_id.startswith("dry-"):
        return True
    try:
        kalshi_delete(f"/portfolio/orders/{order_id}")
        log.info("Cancelled order %s", order_id)
        return True
    except Exception as e:
        log.warning("cancel_order %s failed: %s", order_id, e)
        return False

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
        self._warn_last_ts        = 0.0
        self._warn_attempted      = False
        self._insurance_last_ts   = 0.0
        self._insurance_attempted = False
        self._ai_advice: Optional[str]      = None  # "enter" | "skip" | None
        self._ai_advice_reason: str         = ""
        self._ai_advice_requested: bool     = False
        self._current_ticker: Optional[str] = None

        # Resting GTC SL for main position
        self._resting_sl_order_id: Optional[str] = None
        self._sl_check_ts          = 0.0

        # Counter-trade state
        self._counter_position: Optional[dict]    = None
        self._counter_entry_attempted             = False
        self._counter_entry_last_ts               = 0.0
        self._counter_window_logged               = False
        self._counter_resting_sl_id: Optional[str] = None
        self._counter_sl_check_ts                 = 0.0
        self._counter_tp_last_ts                  = 0.0
        self._counter_tp_window_logged            = False

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

            # AI advisor: request advice 60s before entry window (runs in background)
            if (AI_ADVISOR_ENABLED and _ANTHROPIC_AVAILABLE
                    and pos is None and not self._entry_attempted
                    and not self._ai_advice_requested
                    and elapsed >= MOMENTUM_ENTRY_START - 60):
                self._ai_advice_requested = True
                threading.Thread(
                    target=self._fetch_ai_advice,
                    args=(snap,),
                    daemon=True,
                ).start()

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

            # Resting SL status poll (main) — every 5s
            if (self._resting_sl_order_id
                    and not self._resting_sl_order_id.startswith("dry-")
                    and now - self._sl_check_ts >= 5.0):
                self._sl_check_ts = now
                self._check_resting_sl_status()

            # Reactive SL fallback — only runs if no resting SL was placed
            pos = self._position
            if (pos is not None and pos["phase"] == "holding"
                    and not self._sl_attempted
                    and self._resting_sl_order_id is None
                    and now - self._sl_last_ts >= MOMENTUM_STOP_LOSS_COOLDOWN
                    and now - self._entry_last_ts >= MOMENTUM_SL_MIN_AGE):
                self._try_stop_loss(snap)

            # Warning hedge: last 30s only (gated by HEDGING_ENABLED)
            if HEDGING_ENABLED:
                pos = self._position
                if (pos is not None and pos["phase"] == "holding"
                        and not self._warn_attempted
                        and snap.secs_left <= 30
                        and now - self._warn_last_ts >= MOMENTUM_WARN_COOLDOWN
                        and now - self._entry_last_ts >= MOMENTUM_SL_MIN_AGE):
                    self._try_warn_hedge(snap)

            # Insurance: cheap opposite side (gated by HEDGING_ENABLED)
            if HEDGING_ENABLED:
                pos = self._position
                if (pos is not None and pos["phase"] == "holding"
                        and not self._insurance_attempted
                        and now - self._insurance_last_ts >= MOMENTUM_INSURANCE_COOLDOWN):
                    self._try_insurance_hedge(snap)

            # Reversal hedge (gated by HEDGING_ENABLED)
            if HEDGING_ENABLED:
                pos = self._position
                if (pos is not None and pos["phase"] == "holding"
                        and snap.secs_left <= 60
                        and not self._hedge_attempted
                        and now - self._hedge_last_ts >= MOMENTUM_HEDGE_COOLDOWN):
                    self._check_reversal_hedge(snap)

            # Counter-trade entry window (840–885s, only after main entry)
            pos = self._position
            if (pos is not None
                    and self._counter_position is None
                    and not self._counter_entry_attempted
                    and now - self._counter_entry_last_ts >= MOMENTUM_ENTRY_RETRY_COOLDOWN):
                elapsed = WINDOW_SECS - snap.secs_left
                if MOMENTUM_COUNTER_START <= elapsed <= MOMENTUM_COUNTER_END:
                    if not self._counter_window_logged:
                        self._counter_window_logged = True
                        ctr_side = "no" if pos["side"] == "yes" else "yes"
                        ctr_ask  = snap.no_ask if ctr_side == "no" else snap.yes_ask
                        self._on_log("👀", (
                            f"{self.asset} — counter window open. Watching "
                            f"{ctr_side.upper()} ask for >={MOMENTUM_ENTRY_THRESHOLD}c "
                            f"(currently {ctr_ask}c). {snap.secs_left}s left."
                        ))
                    self._try_counter_entry(snap)

            # Counter resting SL status poll — every 5s
            if (self._counter_resting_sl_id
                    and not self._counter_resting_sl_id.startswith("dry-")
                    and now - self._counter_sl_check_ts >= 5.0):
                self._counter_sl_check_ts = now
                self._check_counter_resting_sl_status()

            # Counter TP
            cpos = self._counter_position
            if (cpos is not None and cpos["phase"] == "holding"
                    and snap.secs_left <= 60
                    and now - self._counter_tp_last_ts >= MOMENTUM_TP_COOLDOWN):
                if not self._counter_tp_window_logged:
                    self._counter_tp_window_logged = True
                    cside = cpos["side"]
                    cbid  = snap.yes_bid if cside == "yes" else snap.no_bid
                    ctp   = max(MOMENTUM_TAKE_PROFIT, cpos["entry_price"] + 1)
                    self._on_log("⏱", (
                        f"{self.asset} — counter last {snap.secs_left}s! "
                        f"Holding {cside.upper()} at {cpos['entry_price']}c. "
                        f"Watching bid >= {ctp}c (now {cbid}c)."
                    ))
                self._try_counter_tp(snap)

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
        self._warn_last_ts        = 0.0
        self._warn_attempted      = False
        self._insurance_last_ts   = 0.0
        self._insurance_attempted = False
        self._ai_advice           = None
        self._ai_advice_reason    = ""
        self._ai_advice_requested = False
        self._resting_sl_order_id = None
        self._sl_check_ts         = 0.0
        self._counter_position         = None
        self._counter_entry_attempted  = False
        self._counter_entry_last_ts    = 0.0
        self._counter_window_logged    = False
        self._counter_resting_sl_id    = None
        self._counter_sl_check_ts      = 0.0
        self._counter_tp_last_ts       = 0.0
        self._counter_tp_window_logged = False

    def _notify(self):
        self._on_update(self.asset, dict(self._position) if self._position else None)

    def _fetch_ai_advice(self, snap):
        """
        Background thread: call Claude with recent trade history and current
        market snapshot. Sets self._ai_advice to "enter" or "skip".
        Falls back to "enter" on any error so the bot never stalls.
        """
        try:
            # Read last N trades from log
            recent: list[dict] = []
            if TRADES_FILE.exists():
                lines = TRADES_FILE.read_text().strip().splitlines()
                for line in lines[-AI_ADVISOR_LOOKBACK:]:
                    try:
                        recent.append(json.loads(line))
                    except Exception:
                        pass

            trades_summary = json.dumps(recent, indent=2) if recent else "No trades yet."

            prompt = f"""You are advising a Kalshi 15-minute binary options trading bot for {self.asset}.

Recent trade history (last {len(recent)} trades across all assets):
{trades_summary}

Current market snapshot for {self.asset} ({snap.ticker}):
- YES ask: {snap.yes_ask}¢  NO ask: {snap.no_ask}¢
- YES bid: {snap.yes_bid}¢  NO bid: {snap.no_bid}¢
- Seconds left in window: {snap.secs_left}

Strategy: enter when one side's ask >= 85¢ in the 13:40-14:20 mark.
TP at 95-97¢, SL at 60¢. Entry at 85-92¢ gives 7-12¢ upside vs 25-30¢ downside.

Look at recent win/loss patterns, back-to-back losses on same asset, and whether
the current price looks like strong momentum or a choppy/reversing market.

Should the bot ENTER or SKIP this window?
Respond with JSON only, no extra text: {{"decision": "enter", "reason": "one short sentence"}}"""

            client   = _anthropic.Anthropic()
            message  = client.messages.create(
                model=AI_ADVISOR_MODEL,
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(raw)
            decision = parsed.get("decision", "enter").lower()
            reason   = parsed.get("reason", "")
            with self._lock:
                self._ai_advice        = decision
                self._ai_advice_reason = reason
            self._on_log("🤖", (
                f"AI ADVISOR [{self.asset}]: {decision.upper()} — {reason}"
            ))
        except Exception as e:
            self._on_log("🤖", f"AI ADVISOR error ({self.asset}): {e} — defaulting to enter")
            with self._lock:
                self._ai_advice = "enter"

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

        if AI_ADVISOR_ENABLED and self._ai_advice == "skip":
            self._entry_attempted = True
            self._on_log("🤖", (
                f"AI ADVISOR skipped {self.asset} entry — {self._ai_advice_reason}"
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
            self._resting_sl_order_id = self._place_resting_sl(snap, side, n, "main")
            self._notify()
            if HEDGING_ENABLED:
                self._buy_entry_hedge(snap, side)
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
                self._resting_sl_order_id = self._place_resting_sl(snap, side, filled, "main")
                self._notify()
                if HEDGING_ENABLED:
                    self._buy_entry_hedge(snap, side)
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

    def _buy_entry_hedge(self, snap, entry_side: str):
        """
        Immediately after entry, buy MOMENTUM_ENTRY_HEDGE_COUNT contracts on the
        opposite side at the implied ask. Covers fast reversals that hit before
        any other protection layer can activate. Best-effort, no retry.
        """
        opp_side  = "no" if entry_side == "yes" else "yes"
        entry_bid = snap.yes_bid if entry_side == "yes" else snap.no_bid
        if entry_bid is None:
            return
        opp_ask = 100 - entry_bid
        if opp_ask <= 0 or opp_ask >= 100:
            return

        n   = MOMENTUM_ENTRY_HEDGE_COUNT
        cid = _make_client_id(self.asset, f"mom-eh-{opp_side}")

        if DRY_RUN:
            self._on_log("📋", (
                f"[DRY RUN] ENTRY HEDGE {self.asset} {opp_side.upper()} {opp_ask}¢ × {n}"
            ))
            return

        body = {
            "ticker":          snap.ticker,
            "side":            opp_side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if opp_side == "yes" else "no_price"] = opp_ask

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                POSITIONS.record_fill(snap.ticker, opp_side, opp_ask, filled, cid)
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_entry_hedge",
                    "asset": self.asset, "ticker": snap.ticker,
                    "hedge_side": opp_side, "hedge_price": opp_ask, "count": filled,
                })
                self._on_log("🛡", (
                    f"ENTRY HEDGE {self.asset} bought {opp_side.upper()} "
                    f"at {opp_ask}¢ × {filled}"
                ))
            else:
                self._on_log("⏸", (
                    f"ENTRY HEDGE {self.asset} {opp_side.upper()} {opp_ask}¢ — no fill"
                ))
        except Exception as e:
            self._on_log("✗", f"ENTRY HEDGE error {self.asset}: {e}")

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

        # Cancel resting SL before selling to avoid double-exit
        if self._resting_sl_order_id:
            _cancel_order(self._resting_sl_order_id)
            self._resting_sl_order_id = None

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
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_tp",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "sell_price": sell_price,
                    "entry_price": pos["entry_price"], "count": filled, "pnl": pnl,
                })
                remaining = pos["count"] - filled
                if remaining <= 0:
                    self._position["phase"] = "closed"
                    self._on_log("✅", (
                        f"{self.asset} — took profit! Sold {side.upper()} at {sell_price}¢ × {filled} "
                        f"(entry {pos['entry_price']}¢)  pnl=+${pnl:.4f}"
                    ))
                else:
                    self._position["count"] = remaining
                    self._on_log("⚡", (
                        f"{self.asset} — partial TP: sold {filled} of {pos['count'] + filled} "
                        f"{side.upper()} at {sell_price}¢  pnl=+${pnl:.4f}  "
                        f"{remaining} contracts remaining, retrying."
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
        Sells the entry side IOC at current bid. No chase hedge after exit —
        the warning hedge at 75¢ already placed cheaper opposite-side coverage.
        """
        self._sl_last_ts = time.time()
        pos         = self._position
        side        = pos["side"]
        entry_price = pos["entry_price"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or current_bid > MOMENTUM_STOP_LOSS:
            return

        n        = pos["count"]
        sl_price = current_bid
        loss_est = (sl_price - entry_price) * n / 100

        # Cancel resting SL to avoid double-exit
        if self._resting_sl_order_id:
            _cancel_order(self._resting_sl_order_id)
            self._resting_sl_order_id = None

        self._on_log("🔴", (
            f"STOP LOSS {self.asset} {side.upper()} bid hit {sl_price}¢  "
            f"(entry={entry_price}¢  est. loss=~${abs(loss_est):.4f})  Exiting."
        ))

        if DRY_RUN:
            self._sl_attempted = True
            POSITIONS.realise_pnl(loss_est)
            self._position["phase"]         = "stop_loss"
            self._position["sl_exit_price"] = sl_price
            self._on_log("📋", (
                f"[DRY RUN] STOP LOSS {self.asset} sell {side.upper()} {sl_price}¢ × {n}  "
                f"pnl=${loss_est:.4f}"
            ))
            self._notify()
            return

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
            else:
                self._on_log("⏸", (
                    f"STOP LOSS {self.asset} sell {side.upper()} at {sl_price}¢ — no fill, retrying."
                ))
        except Exception as e:
            self._on_log("✗", f"STOP LOSS error {self.asset}: {e}")

    def _try_warn_hedge(self, snap):
        """
        While holding, buy MOMENTUM_WARN_COUNT opposite-side contracts when the
        entry-side bid drops to MOMENTUM_WARN_THRESHOLD (default 75¢).
        Cheaper coverage (~25¢/contract) than waiting for the 60¢ SL.
        Position is NOT exited — this is insurance only, one purchase per trade.
        """
        self._warn_last_ts = time.time()
        pos         = self._position
        side        = pos["side"]
        entry_price = pos["entry_price"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if current_bid is None or current_bid > MOMENTUM_WARN_THRESHOLD:
            return

        # Only fire if bid actually dropped from entry (not at entry with a wide spread)
        if current_bid >= entry_price:
            return

        opp_side = "no" if side == "yes" else "yes"
        opp_ask  = 100 - current_bid
        n        = MOMENTUM_WARN_COUNT
        cid      = _make_client_id(self.asset, f"mom-warn-{opp_side}")

        self._on_log("⚠", (
            f"WARNING HEDGE {self.asset} — {side.upper()} bid dropped to {current_bid}¢  "
            f"(entry={entry_price}¢). Buying {n} {opp_side.upper()} at {opp_ask}¢ as warning hedge."
        ))

        if DRY_RUN:
            self._warn_attempted = True
            self._position["warn_hedge_price"] = opp_ask
            self._position["warn_hedge_count"] = n
            self._on_log("📋", f"[DRY RUN] WARNING HEDGE {self.asset} {opp_side.upper()} {opp_ask}¢ × {n}")
            self._notify()
            return

        body = {
            "ticker":          snap.ticker,
            "side":            opp_side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if opp_side == "yes" else "no_price"] = opp_ask

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._warn_attempted = True
                POSITIONS.record_fill(snap.ticker, opp_side, opp_ask, filled, cid)
                self._position["warn_hedge_price"] = opp_ask
                self._position["warn_hedge_count"] = filled
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_warn_hedge",
                    "asset": self.asset, "ticker": snap.ticker,
                    "warn_side": opp_side, "warn_price": opp_ask, "count": filled,
                })
                self._on_log("✅", (
                    f"WARNING HEDGE FILLED {self.asset} bought {opp_side.upper()} "
                    f"at {opp_ask}¢ × {filled}"
                ))
                self._notify()
            else:
                self._on_log("⏸", (
                    f"WARNING HEDGE {self.asset} {opp_side.upper()} {opp_ask}¢ — no fill"
                ))
        except Exception as e:
            self._on_log("✗", f"WARNING HEDGE error {self.asset}: {e}")

    def _try_insurance_hedge(self, snap):
        """
        While in "holding" phase, buy a small number of opposite-side contracts if their
        implied ask drops to <= MOMENTUM_INSURANCE_THRESHOLD (default 5¢).
        Cost is ~1–5¢/contract; pays ~95–99¢ if the main side collapses past the SL.
        One purchase per position, no retry.
        """
        self._insurance_last_ts = time.time()
        pos      = self._position
        side     = pos["side"]
        opp_side = "no" if side == "yes" else "yes"

        # Implied opposite ask = 100 - entry-side bid
        entry_bid = snap.yes_bid if side == "yes" else snap.no_bid
        if entry_bid is None:
            return
        opp_ask = 100 - entry_bid

        if opp_ask > MOMENTUM_INSURANCE_THRESHOLD:
            return

        n   = MOMENTUM_INSURANCE_COUNT
        cid = _make_client_id(self.asset, f"mom-ins-{opp_side}")

        self._on_log("🛡", (
            f"INSURANCE {self.asset} — {opp_side.upper()} ask is {opp_ask}¢  "
            f"(≤ {MOMENTUM_INSURANCE_THRESHOLD}¢). Buying {n} contracts as cheap insurance."
        ))

        if DRY_RUN:
            self._insurance_attempted = True
            self._position["insurance_price"] = opp_ask
            self._position["insurance_count"] = n
            self._on_log("📋", f"[DRY RUN] INSURANCE {self.asset} {opp_side.upper()} {opp_ask}¢ × {n}")
            self._notify()
            return

        body = {
            "ticker":          snap.ticker,
            "side":            opp_side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if opp_side == "yes" else "no_price"] = opp_ask

        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._insurance_attempted = True
                POSITIONS.record_fill(snap.ticker, opp_side, opp_ask, filled, cid)
                self._position["insurance_price"] = opp_ask
                self._position["insurance_count"] = filled
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_insurance",
                    "asset": self.asset, "ticker": snap.ticker,
                    "insurance_side": opp_side, "insurance_price": opp_ask, "count": filled,
                })
                self._on_log("✅", (
                    f"INSURANCE FILLED {self.asset} bought {opp_side.upper()} "
                    f"at {opp_ask}¢ × {filled}"
                ))
                self._notify()
            else:
                self._on_log("⏸", (
                    f"INSURANCE {self.asset} {opp_side.upper()} {opp_ask}¢ — no fill"
                ))
        except Exception as e:
            self._on_log("✗", f"INSURANCE error {self.asset}: {e}")

    # ── Resting SL helpers ────────────────────────────────────────────────────

    def _place_resting_sl(self, snap, side: str, count: int, label: str) -> Optional[str]:
        """
        Place a GTC sell order at MOMENTUM_STOP_LOSS.
        NOTE: On Kalshi, a sell limit at 70c placed when bid > 70c will fill immediately
        at the current market bid (not at 70c). The GTC is most useful as gap protection
        when the order is placed near the current price. Returns order_id or None.
        """
        sl_price = MOMENTUM_STOP_LOSS
        cid      = _make_client_id(self.asset, f"mom-rsl-{label}-{side}")

        if DRY_RUN:
            fake_id = f"dry-{label}-{int(time.time())}"
            self._on_log("📋", (
                f"[DRY RUN] RESTING SL {self.asset} sell {side.upper()} "
                f"{sl_price}c x {count} GTC  id={fake_id}"
            ))
            return fake_id

        body = {
            "ticker":          snap.ticker,
            "side":            side,
            "action":          "sell",
            "count":           count,
            "time_in_force":   "good_til_cancelled",
            "client_order_id": cid,
        }
        body["yes_price" if side == "yes" else "no_price"] = sl_price

        try:
            resp     = _post_order(body)
            order    = resp.get("order", {})
            order_id = order.get("order_id")
            filled   = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                # Filled immediately — treat as SL hit at sl_price
                entry_price = self._position["entry_price"] if self._position else sl_price
                pnl = (sl_price - entry_price) * filled / 100
                POSITIONS.realise_pnl(pnl)
                if self._position:
                    self._position["phase"]         = "stop_loss"
                    self._position["sl_exit_price"] = sl_price
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_resting_sl_imm",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "sl_price": sl_price, "count": filled, "pnl": pnl,
                })
                self._on_log("🔴", (
                    f"RESTING SL {self.asset} filled immediately at {sl_price}c x {filled}  "
                    f"pnl=${pnl:.4f} — position closed"
                ))
                self._notify()
                return None
            if order_id:
                self._on_log("📌", (
                    f"RESTING SL placed {self.asset} sell {side.upper()} "
                    f"{sl_price}c x {count} GTC  id={order_id}"
                ))
            return order_id
        except Exception as e:
            self._on_log("✗", f"RESTING SL place error {self.asset}: {e}")
            return None

    def _check_resting_sl_status(self):
        """Poll main resting SL order; mark position stop_loss if filled."""
        order_id = self._resting_sl_order_id
        if not order_id or not self._position:
            return
        try:
            data   = kalshi_get(f"/portfolio/orders/{order_id}")
            order  = data.get("order", {})
            status = order.get("status", "")
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if status in ("filled", "cancelled") or filled >= self._position.get("count", 0):
                if filled > 0 and self._position["phase"] == "holding":
                    entry_price = self._position["entry_price"]
                    sl_price    = MOMENTUM_STOP_LOSS
                    pnl         = (sl_price - entry_price) * filled / 100
                    POSITIONS.realise_pnl(pnl)
                    self._position["phase"]         = "stop_loss"
                    self._position["sl_exit_price"] = sl_price
                    _log_trade({
                        "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_resting_sl",
                        "asset": self.asset, "ticker": self._position["ticker"],
                        "side": self._position["side"], "sl_price": sl_price,
                        "entry_price": entry_price, "count": filled, "pnl": pnl,
                    })
                    self._on_log("🔴", (
                        f"RESTING SL FILLED {self.asset} at {sl_price}c x {filled}  "
                        f"(entry={entry_price}c)  pnl=${pnl:.4f}"
                    ))
                    self._notify()
                self._resting_sl_order_id = None
        except Exception as e:
            log.debug("resting SL status check %s: %s", self.asset, e)

    # ── Counter-trade ─────────────────────────────────────────────────────────

    def _try_counter_entry(self, snap):
        """
        After main entry, watch the opposite side during COUNTER_START–COUNTER_END.
        If opposite ask is in [ENTRY_THRESHOLD, ENTRY_MAX), buy TRADE_SIZE contracts
        with its own resting GTC SL.
        """
        elapsed = WINDOW_SECS - snap.secs_left
        if not (MOMENTUM_COUNTER_START <= elapsed <= MOMENTUM_COUNTER_END):
            self._counter_entry_last_ts = time.time()
            return

        pos          = self._position
        entry_side   = pos["side"]
        counter_side = "no" if entry_side == "yes" else "yes"
        counter_ask  = snap.no_ask if counter_side == "no" else snap.yes_ask

        if counter_ask is None:
            return

        if not (MOMENTUM_ENTRY_THRESHOLD <= counter_ask < MOMENTUM_ENTRY_MAX):
            self._on_log("👀", (
                f"{self.asset} — counter watching {counter_side.upper()} ask {counter_ask}c "
                f"(need {MOMENTUM_ENTRY_THRESHOLD}-{MOMENTUM_ENTRY_MAX - 1}c)  {snap.secs_left}s left."
            ))
            self._counter_entry_last_ts = time.time()
            return

        n   = TRADE_SIZE_CONTRACTS
        cid = _make_client_id(self.asset, f"mom-ctr-{counter_side}")

        self._on_log("🎯", (
            f"COUNTER ENTRY {self.asset} {counter_side.upper()} at {counter_ask}c  "
            f"(elapsed={elapsed}s  secs_left={snap.secs_left})"
        ))

        if DRY_RUN:
            self._counter_entry_attempted = True
            self._counter_position = {
                "ticker":      snap.ticker,
                "side":        counter_side,
                "entry_price": counter_ask,
                "count":       n,
                "entry_ts":    datetime.now(timezone.utc).isoformat(),
                "phase":       "holding",
            }
            self._on_log("📋", (
                f"[DRY RUN] COUNTER ENTRY {self.asset} {counter_side.upper()} {counter_ask}c x {n}"
            ))
            self._counter_resting_sl_id = self._place_counter_resting_sl(snap, counter_side, n)
            return

        body = {
            "ticker":          snap.ticker,
            "side":            counter_side,
            "action":          "buy",
            "count":           n,
            "time_in_force":   "immediate_or_cancel",
            "client_order_id": cid,
        }
        body["yes_price" if counter_side == "yes" else "no_price"] = counter_ask

        self._counter_entry_last_ts = time.time()
        try:
            resp   = _post_order(body)
            order  = resp.get("order", {})
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                self._counter_entry_attempted = True
                self._counter_position = {
                    "ticker":      snap.ticker,
                    "side":        counter_side,
                    "entry_price": counter_ask,
                    "count":       filled,
                    "entry_ts":    datetime.now(timezone.utc).isoformat(),
                    "phase":       "holding",
                }
                POSITIONS.record_fill(snap.ticker, counter_side, counter_ask, filled, cid)
                _log_trade({
                    "ts": self._counter_position["entry_ts"], "type": "momentum_counter_entry",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": counter_side, "price": counter_ask, "count": filled,
                })
                self._on_log("✅", (
                    f"{self.asset} — counter entered {counter_side.upper()} at {counter_ask}c x {filled}. "
                    f"Placing resting SL at {MOMENTUM_STOP_LOSS}c."
                ))
                self._counter_resting_sl_id = self._place_counter_resting_sl(snap, counter_side, filled)
            else:
                retryable = MOMENTUM_ENTRY_THRESHOLD <= counter_ask <= MOMENTUM_ENTRY_RETRY_MAX
                if not retryable:
                    self._counter_entry_attempted = True
                self._on_log("⏸", (
                    f"COUNTER ENTRY {self.asset} {counter_side.upper()} {counter_ask}c — no fill"
                    + ("" if retryable else ", skipping")
                ))
        except Exception as e:
            self._on_log("✗", f"COUNTER ENTRY error {self.asset}: {e}")

    def _place_counter_resting_sl(self, snap, side: str, count: int) -> Optional[str]:
        """Place resting GTC SL for counter position. Same logic as main SL."""
        sl_price = MOMENTUM_STOP_LOSS
        cid      = _make_client_id(self.asset, f"mom-rsl-ctr-{side}")

        if DRY_RUN:
            fake_id = f"dry-ctr-{int(time.time())}"
            self._on_log("📋", (
                f"[DRY RUN] COUNTER RESTING SL {self.asset} sell {side.upper()} "
                f"{sl_price}c x {count} GTC  id={fake_id}"
            ))
            return fake_id

        body = {
            "ticker":          snap.ticker,
            "side":            side,
            "action":          "sell",
            "count":           count,
            "time_in_force":   "good_til_cancelled",
            "client_order_id": cid,
        }
        body["yes_price" if side == "yes" else "no_price"] = sl_price

        try:
            resp     = _post_order(body)
            order    = resp.get("order", {})
            order_id = order.get("order_id")
            filled   = int(float(order.get("fill_count_fp", "0") or "0"))
            if filled > 0:
                entry_price = self._counter_position["entry_price"] if self._counter_position else sl_price
                pnl = (sl_price - entry_price) * filled / 100
                POSITIONS.realise_pnl(pnl)
                if self._counter_position:
                    self._counter_position["phase"]         = "stop_loss"
                    self._counter_position["sl_exit_price"] = sl_price
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_counter_rsl_imm",
                    "asset": self.asset, "side": side, "sl_price": sl_price, "count": filled, "pnl": pnl,
                })
                self._on_log("🔴", (
                    f"COUNTER RESTING SL {self.asset} filled immediately at {sl_price}c x {filled}  "
                    f"pnl=${pnl:.4f}"
                ))
                return None
            if order_id:
                self._on_log("📌", (
                    f"COUNTER RESTING SL placed {self.asset} sell {side.upper()} "
                    f"{sl_price}c x {count} GTC  id={order_id}"
                ))
            return order_id
        except Exception as e:
            self._on_log("✗", f"COUNTER RESTING SL place error {self.asset}: {e}")
            return None

    def _check_counter_resting_sl_status(self):
        """Poll counter resting SL order; mark counter position stop_loss if filled."""
        order_id = self._counter_resting_sl_id
        if not order_id or not self._counter_position:
            return
        try:
            data   = kalshi_get(f"/portfolio/orders/{order_id}")
            order  = data.get("order", {})
            status = order.get("status", "")
            filled = int(float(order.get("fill_count_fp", "0") or "0"))
            if status in ("filled", "cancelled") or filled >= self._counter_position.get("count", 0):
                if filled > 0 and self._counter_position["phase"] == "holding":
                    entry_price = self._counter_position["entry_price"]
                    sl_price    = MOMENTUM_STOP_LOSS
                    pnl         = (sl_price - entry_price) * filled / 100
                    POSITIONS.realise_pnl(pnl)
                    self._counter_position["phase"]         = "stop_loss"
                    self._counter_position["sl_exit_price"] = sl_price
                    _log_trade({
                        "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_counter_rsl",
                        "asset": self.asset, "sl_price": sl_price,
                        "entry_price": entry_price, "count": filled, "pnl": pnl,
                    })
                    self._on_log("🔴", (
                        f"COUNTER RESTING SL FILLED {self.asset} at {sl_price}c x {filled}  "
                        f"(entry={entry_price}c)  pnl=${pnl:.4f}"
                    ))
                self._counter_resting_sl_id = None
        except Exception as e:
            log.debug("counter SL status check %s: %s", self.asset, e)

    def _try_counter_tp(self, snap):
        """Take profit on counter position when its bid >= TP target."""
        self._counter_tp_last_ts = time.time()
        cpos = self._counter_position
        side = cpos["side"]

        current_bid = snap.yes_bid if side == "yes" else snap.no_bid
        tp_target   = max(MOMENTUM_TAKE_PROFIT, cpos["entry_price"] + 1)
        if current_bid is None or current_bid < tp_target:
            if current_bid is not None:
                self._on_log("⏳", (
                    f"{self.asset} counter — bid {current_bid}c, need {tp_target}c  "
                    f"{snap.secs_left}s left."
                ))
            return

        # Cancel counter resting SL before selling
        if self._counter_resting_sl_id:
            _cancel_order(self._counter_resting_sl_id)
            self._counter_resting_sl_id = None

        n          = cpos["count"]
        sell_price = current_bid
        cid        = _make_client_id(self.asset, f"mom-ctr-tp-{side}")
        profit_est = (sell_price - cpos["entry_price"]) * n / 100

        self._on_log("💰", (
            f"{self.asset} counter — bid hit {sell_price}c! TP on {side.upper()} "
            f"(entry {cpos['entry_price']}c)  est. profit: +${profit_est:.4f}"
        ))

        if DRY_RUN:
            POSITIONS.realise_pnl(profit_est)
            self._counter_position["phase"] = "closed"
            self._on_log("📋", (
                f"[DRY RUN] COUNTER TP {self.asset} {sell_price}c x {n}  pnl=+${profit_est:.4f}"
            ))
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
                pnl = (sell_price - cpos["entry_price"]) * filled / 100
                POSITIONS.realise_pnl(pnl)
                remaining = cpos["count"] - filled
                if remaining <= 0:
                    self._counter_position["phase"] = "closed"
                else:
                    self._counter_position["count"] = remaining
                _log_trade({
                    "ts": datetime.now(timezone.utc).isoformat(), "type": "momentum_counter_tp",
                    "asset": self.asset, "ticker": snap.ticker,
                    "side": side, "sell_price": sell_price,
                    "entry_price": cpos["entry_price"], "count": filled, "pnl": pnl,
                })
                self._on_log("✅", (
                    f"{self.asset} — counter TP! Sold {side.upper()} at {sell_price}c x {filled}  "
                    f"pnl=+${pnl:.4f}"
                ))
            else:
                self._on_log("⏸", (
                    f"{self.asset} — counter TP at {sell_price}c no fill, retrying."
                ))
        except Exception as e:
            self._on_log("✗", f"COUNTER TP error {self.asset}: {e}")

    def get_counter_position(self) -> Optional[dict]:
        with self._lock:
            return dict(self._counter_position) if self._counter_position else None
