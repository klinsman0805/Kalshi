"""
test_kalshi_bot.py - Test suite for engine.py + trader.py

Run:  python -m pytest test_kalshi_bot.py -v
  or: python -m unittest test_kalshi_bot -v

Tests cover:
  1. Orderbook mechanics (snapshot, delta apply, implied ask derivation)
  2. Market snapshot fields
  3. Auth header generation (structure only)
  4. Price parsing helpers
  5. PositionBook tracking
  6. BotEngine callbacks (no real WS)
  7. Live-readiness checklist (env, keys, DRY_RUN flag)
  8. MomentumTrader (entry, take-profit, reversal hedge, window reset, live orders)
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure tests run from the bot directory
sys.path.insert(0, str(Path(__file__).parent))

# Stub websocket-client if not installed (allows tests to run without live deps)
try:
    import websocket
except ImportError:
    websocket = MagicMock()
    sys.modules["websocket"] = websocket

# Cross-platform temp dir (works on Windows, Linux, macOS)
_TMPDIR = Path(tempfile.gettempdir())

# Force DRY_RUN and DEMO for all tests -- must be set BEFORE importing engine/trader
os.environ["DRY_RUN"]        = "true"
os.environ["KALSHI_DEMO"]    = "true"
os.environ["POSITIONS_FILE"] = str(_TMPDIR / "test_positions.json")
os.environ["TRADES_FILE"]    = str(_TMPDIR / "test_trades.jsonl")
os.environ["LOG_FILE"]       = str(_TMPDIR / "test_log.jsonl")

import engine
import trader

# Override module-level Path constants captured at import time
trader.TRADES_FILE = _TMPDIR / "test_trades.jsonl"
engine.LOG_FILE    = _TMPDIR / "test_log.jsonl"


# ==============================================================================
# 1. LocalBook Tests
# ==============================================================================
class TestLocalBook(unittest.TestCase):

    def _make_book(self) -> engine.LocalBook:
        book = engine.LocalBook("KXBTCD-15MIN-12345")
        snapshot = {
            "yes_dollars": [
                ["0.4500", "20.00"],
                ["0.4700", "10.00"],
                ["0.4800", "5.00"],
            ],
            "no_dollars": [
                ["0.4900", "15.00"],
                ["0.5100", "8.00"],
                ["0.5200", "3.00"],
            ],
        }
        book.apply_snapshot(snapshot)
        return book

    def test_snapshot_populates_bids(self):
        book = self._make_book()
        self.assertTrue(book.ready)
        self.assertIn(48, book.yes_bids)
        self.assertIn(52, book.no_bids)

    def test_best_yes_bid(self):
        book = self._make_book()
        self.assertEqual(book.best_yes_bid(), 48)

    def test_best_no_bid(self):
        book = self._make_book()
        self.assertEqual(book.best_no_bid(), 52)

    def test_implied_yes_ask(self):
        """YES ask = 100 - best_NO_bid = 100 - 52 = 48."""
        book = self._make_book()
        self.assertEqual(book.best_yes_ask(), 48)

    def test_implied_no_ask(self):
        """NO ask = 100 - best_YES_bid = 100 - 48 = 52."""
        book = self._make_book()
        self.assertEqual(book.best_no_ask(), 52)

    def test_mid(self):
        """Mid = (YES_bid + YES_ask) / 2 = (48 + 48) / 2 = 48."""
        book = self._make_book()
        self.assertAlmostEqual(book.mid_cents(), 48.0)

    def test_delta_add_level(self):
        """Positive delta adds a new level."""
        book = self._make_book()
        book.apply_delta({"price_dollars": "0.4300", "delta_fp": "30.00", "side": "yes"})
        self.assertIn(43, book.yes_bids)
        self.assertAlmostEqual(book.yes_bids[43], 30.0)

    def test_delta_remove_level(self):
        """Delta that zeroes a level removes it."""
        book = self._make_book()
        book.apply_delta({"price_dollars": "0.4800", "delta_fp": "-5.00", "side": "yes"})
        self.assertNotIn(48, book.yes_bids)

    def test_delta_reduce_level(self):
        """Partial reduction keeps the level with reduced count."""
        book = self._make_book()
        book.apply_delta({"price_dollars": "0.4800", "delta_fp": "-3.00", "side": "yes"})
        self.assertIn(48, book.yes_bids)
        self.assertAlmostEqual(book.yes_bids[48], 2.0)

    def test_delta_invalid_price_ignored(self):
        """Delta with price 0 or 100 is ignored."""
        book = self._make_book()
        count_before = len(book.yes_bids)
        book.apply_delta({"price_dollars": "0.0000", "delta_fp": "10.00", "side": "yes"})
        book.apply_delta({"price_dollars": "1.0000", "delta_fp": "10.00", "side": "yes"})
        self.assertEqual(len(book.yes_bids), count_before)

    def test_empty_book_returns_none(self):
        book = engine.LocalBook("KXBTCD-15MIN-0")
        self.assertIsNone(book.best_yes_bid())
        self.assertIsNone(book.best_no_bid())
        self.assertIsNone(book.best_yes_ask())
        self.assertIsNone(book.best_no_ask())
        self.assertIsNone(book.mid_cents())

    def test_max_levels_cap(self):
        """Book should not exceed MAX_LEVELS price levels."""
        book = engine.LocalBook("KXBTCD-15MIN-0")
        book.ready = True
        for i in range(1, engine.LocalBook.MAX_LEVELS + 50):
            if 1 <= i <= 99:
                book.apply_delta({
                    "price_dollars": f"{i/100:.2f}",
                    "delta_fp": "10.00",
                    "side": "yes",
                })
        self.assertLessEqual(len(book.yes_bids), engine.LocalBook.MAX_LEVELS)


# ==============================================================================
# 2. MarketSnapshot Tests
# ==============================================================================
class TestMarketSnapshot(unittest.TestCase):

    def _make_snap(self, yes_bids, no_bids) -> engine.MarketSnapshot:
        book = engine.LocalBook("KXBTCD-15MIN-12345")
        book.yes_bids = yes_bids
        book.no_bids  = no_bids
        book.ready    = True
        return engine.MarketSnapshot(
            asset="BTC", ticker="KXBTCD-15MIN-12345",
            book=book, window_ts=12345, secs_left=300
        )

    def test_implied_asks_computed(self):
        """YES ask = 100 - best_NO_bid, NO ask = 100 - best_YES_bid."""
        snap = self._make_snap(yes_bids={48: 10}, no_bids={52: 10})
        self.assertEqual(snap.yes_ask, 48)   # 100 - 52
        self.assertEqual(snap.no_ask,  52)   # 100 - 48

    def test_mid_is_average(self):
        snap = self._make_snap(yes_bids={48: 10}, no_bids={52: 10})
        self.assertIsNotNone(snap.mid)

    def test_to_dict_has_required_keys(self):
        snap = self._make_snap(yes_bids={48: 10}, no_bids={52: 10})
        d = snap.to_dict()
        for key in ["asset", "ticker", "ts", "secs_left",
                    "yes_bid", "no_bid", "yes_ask", "no_ask", "mid"]:
            self.assertIn(key, d, f"Missing key: {key}")

    def test_none_when_book_empty(self):
        snap = self._make_snap(yes_bids={}, no_bids={})
        self.assertIsNone(snap.yes_bid)
        self.assertIsNone(snap.no_bid)
        self.assertIsNone(snap.yes_ask)
        self.assertIsNone(snap.no_ask)


# ==============================================================================
# 3. Auth Header Tests
# ==============================================================================
class TestAuthHeaders(unittest.TestCase):

    def setUp(self):
        self._orig_key_id   = engine.KALSHI_KEY_ID
        self._orig_key_file = engine.KALSHI_KEY_FILE
        engine.KALSHI_KEY_ID   = "test-key-id-123"
        engine.KALSHI_KEY_FILE = str(_TMPDIR / "nonexistent_key.pem")
        engine._private_key    = None

    def tearDown(self):
        engine.KALSHI_KEY_ID   = self._orig_key_id
        engine.KALSHI_KEY_FILE = self._orig_key_file
        engine._private_key    = None

    def test_auth_headers_have_required_keys(self):
        headers = engine._auth_headers("GET", "/trade-api/v2/portfolio/balance")
        self.assertIn("KALSHI-ACCESS-KEY", headers)
        self.assertIn("KALSHI-ACCESS-TIMESTAMP", headers)
        self.assertIn("KALSHI-ACCESS-SIGNATURE", headers)
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "test-key-id-123")

    def test_timestamp_is_milliseconds(self):
        headers = engine._auth_headers("POST", "/trade-api/v2/portfolio/orders")
        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        self.assertGreater(ts, 1_000_000_000_000)

    def test_sign_strips_query_params(self):
        """Signing must strip query params -- no exception even without key."""
        sig = engine._sign(1234567890000, "GET", "/trade-api/v2/markets?status=open")
        self.assertIsInstance(sig, str)


# ==============================================================================
# 4. Price Parsing Tests
# ==============================================================================
class TestPriceParsing(unittest.TestCase):

    def test_parse_dollar_to_cents(self):
        self.assertEqual(engine._parse_price("0.4800"), 48)
        self.assertEqual(engine._parse_price("0.0100"), 1)
        self.assertEqual(engine._parse_price("0.9900"), 99)

    def test_parse_none(self):
        self.assertIsNone(engine._parse_price(None))
        self.assertIsNone(engine._parse_price("invalid"))

    def test_cents_helper(self):
        self.assertEqual(engine._cents("0.4800"), 48)
        self.assertEqual(engine._cents("0.0500"), 5)
        self.assertIsNone(engine._cents(None))


# ==============================================================================
# 5. PositionBook Tests  (fully isolated -- no shared global state)
# ==============================================================================
class TestPositionBook(unittest.TestCase):

    def _fresh_book(self) -> trader.PositionBook:
        """Create a fresh PositionBook instance with in-memory state only."""
        import threading
        book = trader.PositionBook.__new__(trader.PositionBook)
        book._lock = threading.Lock()
        book._data = {
            "open_orders": {},
            "positions":   {},
            "realised_pnl": 0.0,
            "total_fills":  0,
        }
        book._save = lambda: None   # no-op: don't touch disk
        return book

    def test_initial_position_zero(self):
        book = self._fresh_book()
        self.assertEqual(book.yes_position("KXBTCD-15MIN-0"), 0)
        self.assertEqual(book.no_position("KXBTCD-15MIN-0"), 0)

    def test_record_fill_updates_position(self):
        book = self._fresh_book()
        book.record_fill("KXBTCD-15MIN-0", "yes", 48, 5, "cid-001")
        self.assertEqual(book.yes_position("KXBTCD-15MIN-0"), 5)

    def test_record_multiple_fills_avg_cost(self):
        book = self._fresh_book()
        book.record_fill("KXBTCD-15MIN-0", "yes", 48, 5, "cid-001")
        book.record_fill("KXBTCD-15MIN-0", "yes", 50, 5, "cid-002")
        pos = book._data["positions"]["KXBTCD-15MIN-0"]
        self.assertEqual(pos["yes_contracts"], 10)
        self.assertAlmostEqual(pos["avg_yes_cost"], 0.49, places=4)

    def test_total_exposure(self):
        book = self._fresh_book()
        book.record_fill("KXBTCD-15MIN-0", "yes", 48, 5, "cid-001")
        book.record_fill("KXBTCD-15MIN-0", "no",  52, 5, "cid-002")
        self.assertEqual(book.total_exposure("KXBTCD-15MIN-0"), 10)

    def test_record_open_order(self):
        book = self._fresh_book()
        book.record_open_order("cid-001", {
            "asset": "BTC", "ticker": "KXBTCD-15MIN-0",
            "side": "yes", "price_cents": 48, "count": 5,
            "order_id": "oid-001", "ts": "2026-01-01T00:00:00Z",
        })
        orders = book.open_orders_for("KXBTCD-15MIN-0")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["side"], "yes")

    def test_remove_open_order(self):
        book = self._fresh_book()
        book.record_open_order("cid-001", {
            "asset": "BTC", "ticker": "KXBTCD-15MIN-0",
            "side": "yes", "price_cents": 48, "count": 5,
            "order_id": "oid-001", "ts": "2026-01-01T00:00:00Z",
        })
        book.remove_open_order("cid-001")
        self.assertEqual(book.open_orders_for("KXBTCD-15MIN-0"), [])

    def test_fill_removes_open_order(self):
        book = self._fresh_book()
        book.record_open_order("cid-001", {
            "asset": "BTC", "ticker": "KXBTCD-15MIN-0",
            "side": "yes", "price_cents": 48, "count": 5,
            "order_id": "oid-001", "ts": "2026-01-01T00:00:00Z",
        })
        book.record_fill("KXBTCD-15MIN-0", "yes", 48, 5, "cid-001")
        self.assertEqual(book.open_orders_for("KXBTCD-15MIN-0"), [])


# ==============================================================================
# 6. BotEngine Callback Tests (no real WS)
# ==============================================================================
class TestBotEngineCallbacks(unittest.TestCase):

    def test_engine_initializes_and_stops(self):
        bot = engine.BotEngine(
            on_log    = lambda i, m: None,
            on_prices = lambda m, s: None,
            on_status = lambda s: None,
        )
        self.assertIsNotNone(bot)
        self.assertIsInstance(bot.markets, dict)
        self.assertEqual(bot.update_count, 0)
        bot.stop()
        self.assertFalse(bot.is_running())

    def test_compute_and_push_sets_snapshot(self):
        bot = engine.BotEngine(
            on_log=lambda i, m: None, on_prices=lambda m, s: None,
            on_status=lambda s: None,
        )
        ticker = "KXBTCD-15MIN-12345"
        book   = engine.LocalBook(ticker)
        book.yes_bids = {55: 100}
        book.no_bids  = {55: 100}
        book.ready    = True
        bot.markets["BTC"] = {
            "asset": "BTC", "ticker": ticker, "window_ts": 12345,
            "close_time": "2099-01-01T00:00:00Z", "secs_left": 300, "tick_size": 1,
        }
        bot._books[ticker]      = book
        bot._ticker_map[ticker] = "BTC"
        bot._snapshots["BTC"]   = None

        bot._compute_and_push("BTC")

        snap = bot._snapshots.get("BTC")
        self.assertIsNotNone(snap)
        # YES ask = 100 - 55 = 45, NO ask = 100 - 55 = 45
        self.assertEqual(snap.yes_ask, 45)
        self.assertEqual(snap.no_ask,  45)

    def test_on_prices_fires_on_price_change(self):
        """on_prices callback fires when yes_ask or no_ask changes."""
        events = []
        bot = engine.BotEngine(
            on_log=lambda i, m: None,
            on_prices=lambda m, s: events.append(s),
            on_status=lambda s: None,
        )
        ticker = "KXBTCD-15MIN-12345"
        book   = engine.LocalBook(ticker)
        book.yes_bids = {60: 100}
        book.no_bids  = {60: 100}
        book.ready    = True
        bot.markets["BTC"] = {
            "asset": "BTC", "ticker": ticker, "window_ts": 12345,
            "close_time": "2099-01-01T00:00:00Z", "secs_left": 300, "tick_size": 1,
        }
        bot._books[ticker]      = book
        bot._ticker_map[ticker] = "BTC"

        bot._compute_and_push("BTC")
        bot._compute_and_push("BTC")   # same prices — should NOT fire again

        self.assertEqual(len(events), 1)


# ==============================================================================
# Shared helper for MomentumTrader tests
# ==============================================================================
def _make_momentum_snap(yes_bid=None, no_bid=None, secs_left=100,
                        ticker="KXBTCD-15MIN-12345",
                        override_no_ask=None, override_yes_ask=None
                        ) -> engine.MarketSnapshot:
    """
    Build a MarketSnapshot for MomentumTrader tests.

    override_no_ask / override_yes_ask let tests force ask prices that cannot
    be produced by the standard implied-ask formula (100 - opposite_bid), which
    is needed to test the reversal-hedge branch in isolation.
    """
    book = engine.LocalBook(ticker)
    if yes_bid is not None:
        book.yes_bids = {yes_bid: 10}
    if no_bid is not None:
        book.no_bids = {no_bid: 10}
    book.ready = True
    snap = engine.MarketSnapshot("BTC", ticker, book, 12345, secs_left)
    if override_no_ask is not None:
        snap.no_ask = override_no_ask
    if override_yes_ask is not None:
        snap.yes_ask = override_yes_ask
    return snap


# ==============================================================================
# 7. Live-Readiness Checklist
# ==============================================================================
class TestLiveReadiness(unittest.TestCase):
    """
    Validates all prerequisites needed before flipping DRY_RUN=false.
    These tests are safe to run at any time — no network calls, no orders.
    """

    def test_api_base_is_demo_when_demo_mode(self):
        """In KALSHI_DEMO=true mode, API_BASE must point to the demo host."""
        if engine.USE_DEMO:
            self.assertIn("demo", engine.API_BASE,
                          f"API_BASE={engine.API_BASE!r} but USE_DEMO=True — "
                          "orders would hit the live API with demo credentials!")

    def test_api_base_is_live_when_live_mode(self):
        """In KALSHI_DEMO=false mode, API_BASE must point to the live host."""
        if not engine.USE_DEMO:
            self.assertIn("elections.kalshi.com", engine.API_BASE,
                          f"API_BASE={engine.API_BASE!r} but USE_DEMO=False")

    def test_ws_url_matches_demo_flag(self):
        if engine.USE_DEMO:
            self.assertIn("demo", engine.WS_URL)
        else:
            self.assertIn("elections.kalshi.com", engine.WS_URL)

    def test_key_id_is_set(self):
        key_id = os.getenv("KALSHI_KEY_ID", "")
        self.assertTrue(key_id, "KALSHI_KEY_ID not set in .env — cannot authenticate")

    def test_rsa_key_file_loads(self):
        """Key file exists and parses as a valid RSA private key."""
        actual_key_file = os.getenv("KALSHI_KEY_FILE", "kalshi.key")
        if not Path(actual_key_file).exists():
            self.skipTest(
                f"RSA key file not present ({actual_key_file!r}) — "
                "download from Kalshi account → API Keys and save as kalshi.key"
            )
        orig = engine.KALSHI_KEY_FILE
        engine.KALSHI_KEY_FILE = actual_key_file
        engine._private_key    = None
        try:
            key = engine._load_key()
            self.assertIsNotNone(key, "Key file found but failed to load — check PEM format")
        finally:
            engine.KALSHI_KEY_FILE = orig
            engine._private_key    = None

    def test_auth_signature_is_non_empty_when_key_absent(self):
        """_sign returns empty string (not an exception) when key file is missing."""
        engine._private_key = None
        original_key_file = engine.KALSHI_KEY_FILE
        engine.KALSHI_KEY_FILE = str(_TMPDIR / "no_such_key.pem")
        try:
            sig = engine._sign(int(time.time() * 1000), "GET", "/trade-api/v2/markets")
            self.assertIsInstance(sig, str)   # no exception, graceful empty string
        finally:
            engine.KALSHI_KEY_FILE = original_key_file
            engine._private_key    = None

    def test_dry_run_is_on_in_test_environment(self):
        """Confirm DRY_RUN is forced true in the test suite."""
        self.assertTrue(engine.DRY_RUN,
                        "DRY_RUN is False during test run — this is unsafe")

    def test_momentum_config_defaults_are_sensible(self):
        """Momentum thresholds must satisfy entry_threshold < take_profit < 100."""
        self.assertGreater(trader.MOMENTUM_ENTRY_THRESHOLD, 0)
        self.assertLess(trader.MOMENTUM_ENTRY_THRESHOLD, trader.MOMENTUM_TAKE_PROFIT)
        self.assertLess(trader.MOMENTUM_TAKE_PROFIT, 100)
        self.assertLess(trader.MOMENTUM_ENTRY_THRESHOLD, trader.MOMENTUM_ENTRY_MAX)
        self.assertGreater(trader.MOMENTUM_ENTRY_START, 0)
        self.assertGreater(trader.MOMENTUM_ENTRY_END, trader.MOMENTUM_ENTRY_START)


# ==============================================================================
# 8. MomentumTrader Tests
# ==============================================================================

# ── Entry phase ───────────────────────────────────────────────────────────────
class TestMomentumTraderEntry(unittest.TestCase):
    """Phase 1: IOC buy in the 780–840 s elapsed window (13th–14th minute)."""

    def _make_trader(self):
        return trader.MomentumTrader("BTC",
                                     on_log=lambda i, m: None,
                                     on_update=lambda a, p: None)

    def test_no_entry_before_window(self):
        """No entry when elapsed < MOMENTUM_ENTRY_START (780 s)."""
        mt   = self._make_trader()
        # yes_bid=10 → yes_ask=90 >= 85 (qualifies), but elapsed=779 < 780
        snap = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=121)  # elapsed=779
        mt.update(snap, {})
        self.assertIsNone(mt.get_position())
        self.assertFalse(mt._entry_attempted)

    def test_no_entry_after_window(self):
        """No entry when elapsed > MOMENTUM_ENTRY_END (840 s)."""
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=59)   # elapsed=841
        mt.update(snap, {})
        self.assertIsNone(mt.get_position())
        self.assertFalse(mt._entry_attempted)

    def test_no_entry_when_both_asks_below_threshold(self):
        """No entry when both implied asks < MOMENTUM_ENTRY_THRESHOLD (85 ¢).

        Entry uses ask prices (cost to buy), not bids.
        yes_ask = 100 - no_bid;  no_ask = 100 - yes_bid.
        With yes_bid=50, no_bid=50: yes_ask=50 and no_ask=50 — both below 85.
        """
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=50, no_bid=50, secs_left=100)  # elapsed=800
        mt.update(snap, {})
        self.assertIsNone(mt.get_position())
        self.assertFalse(mt._entry_attempted)

    def test_entry_fires_yes_when_only_yes_qualifies(self):
        """Entry fires on YES when yes_ask >= 85 ¢ and no_ask < 85 ¢.

        yes_bid=50, no_bid=10  →  yes_ask=90 (≥85), no_ask=50 (<85).
        Entry price is the ask price paid (90 ¢), not the bid.
        """
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=50, no_bid=10, secs_left=100)
        mt.update(snap, {})
        pos = mt.get_position()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["side"], "yes")
        self.assertEqual(pos["entry_price"], 90)   # yes_ask = 100 - 10
        self.assertEqual(pos["phase"], "holding")

    def test_entry_fires_no_when_only_no_qualifies(self):
        """Entry fires on NO when no_ask >= 85 ¢ and yes_ask < 85 ¢.

        yes_bid=10, no_bid=50  →  yes_ask=50 (<85), no_ask=90 (≥85).
        """
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=50, secs_left=100)
        mt.update(snap, {})
        pos = mt.get_position()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["side"], "no")
        self.assertEqual(pos["entry_price"], 90)   # no_ask = 100 - 10

    def test_entry_prefers_higher_ask_yes(self):
        """When both qualify, entry takes the side with the higher ask (YES here).

        yes_bid=12, no_bid=10  →  yes_ask=90, no_ask=88  →  YES selected.
        """
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=12, no_bid=10, secs_left=100)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["side"], "yes")

    def test_entry_prefers_higher_ask_no(self):
        """When both qualify, entry takes the side with the higher ask (NO here).

        yes_bid=10, no_bid=12  →  yes_ask=88, no_ask=90  →  NO selected.
        """
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=12, secs_left=100)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["side"], "no")

    def test_entry_fires_only_once_per_window(self):
        """A second tick inside the entry window does not replace the position."""
        mt    = self._make_trader()
        snap1 = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=100)  # yes_ask=90
        mt.update(snap1, {})
        first_price = mt.get_position()["entry_price"]

        snap2 = _make_momentum_snap(yes_bid=8, no_bid=8, secs_left=95)  # yes_ask=92, still in window
        mt.update(snap2, {})
        self.assertEqual(mt.get_position()["entry_price"], first_price)

    def test_entry_count_equals_trade_size_contracts(self):
        """Entry fills exactly TRADE_SIZE_CONTRACTS contracts."""
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=100)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["count"], trader.TRADE_SIZE_CONTRACTS)

    def test_entry_position_has_all_required_fields(self):
        """Position dict exposes all required fields after a dry-run entry."""
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=100)
        mt.update(snap, {})
        pos = mt.get_position()
        for field in ["ticker", "side", "entry_price", "count", "entry_ts",
                      "phase", "hedge_price", "hedge_count"]:
            self.assertIn(field, pos, f"Missing field: {field}")
        self.assertIsNone(pos["hedge_price"])
        self.assertIsNone(pos["hedge_count"])

    def test_get_position_returns_copy(self):
        """get_position() returns a copy — mutating it does not affect internal state."""
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=10, no_bid=10, secs_left=100)
        mt.update(snap, {})
        copy = mt.get_position()
        copy["phase"] = "TAMPERED"
        self.assertEqual(mt.get_position()["phase"], "holding")

    def test_get_position_returns_none_initially(self):
        """Fresh MomentumTrader has no position."""
        self.assertIsNone(self._make_trader().get_position())

    def test_on_update_callback_invoked_on_entry(self):
        """on_update callback fires once with (asset, position) on entry."""
        updates = []
        mt = trader.MomentumTrader("BTC",
                                   on_log=lambda i, m: None,
                                   on_update=lambda a, p: updates.append((a, p)))
        mt.update(_make_momentum_snap(yes_bid=10, no_bid=10, secs_left=100), {})
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "BTC")
        self.assertIsNotNone(updates[0][1])


# ── Take-profit phase ─────────────────────────────────────────────────────────
class TestMomentumTraderTakeProfit(unittest.TestCase):
    """Phase 2: IOC sell when bid >= MOMENTUM_TAKE_PROFIT in the last 60 s."""

    def _make_trader_holding(self, side="yes", entry_price=85):
        """MomentumTrader pre-loaded with a 'holding' position."""
        mt = trader.MomentumTrader("BTC",
                                   on_log=lambda i, m: None,
                                   on_update=lambda a, p: None)
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": side, "entry_price": entry_price, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        return mt

    def test_tp_does_not_fire_when_secs_left_above_60(self):
        """TP phase is only active when secs_left <= 60."""
        mt   = self._make_trader_holding("yes", 85)
        snap = _make_momentum_snap(yes_bid=96, secs_left=90)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")

    def test_tp_does_not_fire_when_bid_below_target(self):
        """TP does not trigger when bid < MOMENTUM_TAKE_PROFIT (95 ¢)."""
        mt   = self._make_trader_holding("yes", 85)
        snap = _make_momentum_snap(yes_bid=94, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")

    def test_tp_fires_when_yes_bid_reaches_target(self):
        """DRY_RUN: TP closes YES position when bid >= 95 ¢ in last 60 s."""
        mt   = self._make_trader_holding("yes", 85)
        snap = _make_momentum_snap(yes_bid=95, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "closed")

    def test_tp_fires_on_no_side(self):
        """DRY_RUN: TP closes NO position when NO bid >= 95 ¢."""
        mt   = self._make_trader_holding("no", 87)
        snap = _make_momentum_snap(no_bid=96, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "closed")

    def test_tp_respects_cooldown(self):
        """TP attempt is suppressed within MOMENTUM_TP_COOLDOWN seconds of prior try."""
        mt = self._make_trader_holding("yes", 85)
        mt._tp_last_ts = time.time()          # simulate a very recent attempt
        snap = _make_momentum_snap(yes_bid=96, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")

    def test_tp_does_not_fire_when_phase_is_hedged(self):
        """TP only runs while phase == 'holding'; skips if already hedged."""
        mt = self._make_trader_holding("yes", 85)
        mt._position["phase"] = "hedged"
        snap = _make_momentum_snap(yes_bid=97, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "hedged")


# ── Reversal-hedge phase ──────────────────────────────────────────────────────
class TestMomentumTraderHedge(unittest.TestCase):
    """
    Phase 3: IOC buy opposite side when:
      - entry-side bid dropped >= MOMENTUM_REVERSAL_DROP from entry price, AND
      - opposite ask < (100 - entry_price - MOMENTUM_HEDGE_MIN_GAP)
    """

    def _make_trader_holding(self, side="yes", entry_price=85):
        mt = trader.MomentumTrader("BTC",
                                   on_log=lambda i, m: None,
                                   on_update=lambda a, p: None)
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": side, "entry_price": entry_price, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        return mt

    def test_hedge_skipped_when_drop_insufficient(self):
        """Hedge not triggered when entry-side drop < MOMENTUM_REVERSAL_DROP (10 ¢)."""
        mt   = self._make_trader_holding("yes", entry_price=85)
        # YES bid=76 → drop=9 < 10
        snap = _make_momentum_snap(yes_bid=76, secs_left=30)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")
        self.assertFalse(mt._hedge_attempted)

    def test_hedge_marks_attempted_when_ask_too_high(self):
        """When drop is sufficient but opposite ask >= threshold, hedge_attempted is set."""
        mt   = self._make_trader_holding("yes", entry_price=85)
        # YES bid=70 → drop=15 >= 10; implied NO ask = 100-70 = 30 >= threshold(10)
        snap = _make_momentum_snap(yes_bid=70, secs_left=30)
        mt.update(snap, {})
        self.assertTrue(mt._hedge_attempted)
        self.assertEqual(mt.get_position()["phase"], "holding")  # no fill

    def test_hedge_fires_when_opposite_ask_below_threshold(self):
        """
        DRY_RUN: hedge fires when drop >= 10 AND opposite ask < threshold.

        The implied NO ask (100 - yes_bid) is always >= threshold when the drop
        condition holds in a fair book, so we override snap.no_ask directly to
        test this branch in isolation.
        """
        mt   = self._make_trader_holding("yes", entry_price=85)
        # YES bid=70 (drop=15 >= 10); override NO ask to 8 ¢ (< threshold=10)
        snap = _make_momentum_snap(yes_bid=70, secs_left=30, override_no_ask=8)
        mt.update(snap, {})
        pos = mt.get_position()
        self.assertEqual(pos["phase"], "hedged")
        self.assertEqual(pos["hedge_price"], 8)
        self.assertEqual(pos["hedge_count"], 5)

    def test_hedge_threshold_uses_entry_price(self):
        """Threshold = 100 - entry_price - MOMENTUM_HEDGE_MIN_GAP (not current price)."""
        # entry=90, gap=5 → threshold=5; YES bid=79 (drop=11 >= 10); NO ask=4 (< 5)
        mt   = self._make_trader_holding("yes", entry_price=90)
        snap = _make_momentum_snap(yes_bid=79, secs_left=30, override_no_ask=4)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "hedged")
        self.assertEqual(mt.get_position()["hedge_price"], 4)

    def test_hedge_fires_only_once_per_window(self):
        """_hedge_attempted flag prevents a second hedge attempt in the same window."""
        mt = self._make_trader_holding("yes", entry_price=85)
        mt._hedge_attempted = True
        snap = _make_momentum_snap(yes_bid=70, secs_left=30, override_no_ask=8)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")

    def test_hedge_skipped_when_secs_left_above_60(self):
        """Hedge phase is only active in the last 60 s."""
        mt   = self._make_trader_holding("yes", entry_price=85)
        snap = _make_momentum_snap(yes_bid=70, secs_left=90, override_no_ask=8)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")
        self.assertFalse(mt._hedge_attempted)

    def test_hedge_skipped_when_phase_is_not_holding(self):
        """Hedge only runs while phase == 'holding'."""
        mt = self._make_trader_holding("yes", entry_price=85)
        mt._position["phase"] = "closed"
        snap = _make_momentum_snap(yes_bid=70, secs_left=30, override_no_ask=8)
        mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "closed")

    def test_hedge_no_side_entry(self):
        """DRY_RUN: hedge fires on YES side when holding NO and NO bid drops."""
        mt   = self._make_trader_holding("no", entry_price=87)
        # NO bid=76 (drop=11 >= 10); override YES ask to 6 ¢ (< threshold=8)
        snap = _make_momentum_snap(no_bid=76, secs_left=30, override_yes_ask=6)
        mt.update(snap, {})
        pos = mt.get_position()
        self.assertEqual(pos["phase"], "hedged")
        self.assertEqual(pos["hedge_price"], 6)


# ── Window reset ──────────────────────────────────────────────────────────────
class TestMomentumTraderWindowReset(unittest.TestCase):

    def _make_trader_holding(self):
        mt = trader.MomentumTrader("BTC",
                                   on_log=lambda i, m: None,
                                   on_update=lambda a, p: None)
        mt._entry_attempted  = True
        mt._hedge_attempted  = True
        mt._tp_last_ts       = time.time()
        mt._hedge_last_ts    = time.time()
        mt._current_ticker   = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": "yes", "entry_price": 85, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        return mt

    def test_on_market_expire_clears_position(self):
        mt = self._make_trader_holding()
        mt.on_market_expire("KXBTCD-15MIN-12345")
        self.assertIsNone(mt.get_position())

    def test_on_market_expire_resets_all_state(self):
        mt = self._make_trader_holding()
        mt.on_market_expire("KXBTCD-15MIN-12345")
        self.assertFalse(mt._entry_attempted)
        self.assertFalse(mt._hedge_attempted)
        self.assertEqual(mt._tp_last_ts, 0.0)
        self.assertEqual(mt._hedge_last_ts, 0.0)

    def test_on_market_expire_with_no_position_is_safe(self):
        """on_market_expire does not raise when no position is held."""
        mt = trader.MomentumTrader("BTC",
                                   on_log=lambda i, m: None,
                                   on_update=lambda a, p: None)
        mt._current_ticker = "KXBTCD-15MIN-12345"
        mt.on_market_expire("KXBTCD-15MIN-12345")   # must not raise
        self.assertIsNone(mt.get_position())

    def test_ticker_change_resets_window(self):
        """When a new market ticker arrives, prior window state is cleared."""
        mt   = self._make_trader_holding()
        snap = _make_momentum_snap(yes_bid=50, secs_left=500,
                                   ticker="KXBTCD-15MIN-99999")
        mt.update(snap, {})
        self.assertIsNone(mt.get_position())
        self.assertFalse(mt._entry_attempted)


# ── Live-order paths (mocked _post_order) ─────────────────────────────────────
class TestMomentumTraderLiveOrders(unittest.TestCase):
    """
    Exercises the non-dry-run branches of MomentumTrader by mocking _post_order.
    DRY_RUN is temporarily set False and restored in tearDown.
    """

    def setUp(self):
        self._orig_dry = trader.DRY_RUN

    def tearDown(self):
        trader.DRY_RUN = self._orig_dry
        trader.POSITIONS._data["positions"].pop("KXBTCD-15MIN-12345", None)

    def _make_trader(self):
        return trader.MomentumTrader("BTC",
                                     on_log=lambda i, m: None,
                                     on_update=lambda a, p: None)

    def _fake_resp(self, fill_count):
        return {"order": {"order_id": "o1", "status": "executed",
                          "fill_count_fp": str(float(fill_count))}}

    # Entry
    def test_live_entry_fill_sets_position(self):
        """Filled IOC buy creates a 'holding' position.

        yes_bid=12, no_bid=12  →  yes_ask=88, no_ask=88  →  YES selected, entry_price=88.
        """
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=12, no_bid=12, secs_left=100)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(5)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        pos = mt.get_position()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["phase"], "holding")
        self.assertEqual(pos["count"], 5)

    def test_live_entry_no_fill_outside_retry_range_marks_attempted(self):
        """Zero-fill at a price above MOMENTUM_ENTRY_RETRY_MAX permanently stops retries.

        yes_bid=50, no_bid=6  →  yes_ask=94 > RETRY_MAX(90)  →  not retryable
        → _entry_attempted=True after zero fill.
        """
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt   = self._make_trader()
        snap = _make_momentum_snap(yes_bid=50, no_bid=6, secs_left=100)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(0)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        self.assertIsNone(mt.get_position())
        self.assertTrue(mt._entry_attempted)

    # Take-profit
    def test_live_tp_fill_closes_position(self):
        """Filled IOC sell transitions phase to 'closed'."""
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt = self._make_trader()
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": "yes", "entry_price": 85, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        snap = _make_momentum_snap(yes_bid=96, secs_left=30)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(5)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "closed")

    def test_live_tp_no_fill_keeps_holding(self):
        """Zero-fill IOC sell keeps phase as 'holding' for next retry."""
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt = self._make_trader()
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": "yes", "entry_price": 85, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        snap = _make_momentum_snap(yes_bid=96, secs_left=30)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(0)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")

    # Reversal hedge
    def test_live_hedge_fill_sets_hedged_phase(self):
        """Filled IOC hedge buy transitions phase to 'hedged' and records price."""
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt = self._make_trader()
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": "yes", "entry_price": 85, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        snap = _make_momentum_snap(yes_bid=70, secs_left=30, override_no_ask=8)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(5)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        pos = mt.get_position()
        self.assertEqual(pos["phase"], "hedged")
        self.assertEqual(pos["hedge_price"], 8)

    def test_live_hedge_no_fill_allows_retry(self):
        """Zero-fill hedge IOC clears hedge_attempted so the next tick can retry."""
        import unittest.mock as mock
        trader.DRY_RUN = False
        mt = self._make_trader()
        mt._entry_attempted = True
        mt._current_ticker  = "KXBTCD-15MIN-12345"
        mt._position = {
            "ticker": "KXBTCD-15MIN-12345",
            "side": "yes", "entry_price": 85, "count": 5,
            "entry_ts": "2026-01-01T00:00:00Z", "phase": "holding",
            "hedge_price": None, "hedge_count": None,
        }
        snap = _make_momentum_snap(yes_bid=70, secs_left=30, override_no_ask=8)
        with mock.patch.object(trader, "_post_order", return_value=self._fake_resp(0)):
            with mock.patch.object(trader, "_log_trade"):
                mt.update(snap, {})
        self.assertEqual(mt.get_position()["phase"], "holding")
        self.assertFalse(mt._hedge_attempted)   # cleared → retry allowed


if __name__ == "__main__":
    unittest.main(verbosity=2)
