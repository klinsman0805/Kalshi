"""
app.py -- Kalshi Market-Making Bot Dashboard
Run: python app.py
Open: http://localhost:5000

Serves a live dashboard with:
  - Real-time price feed per asset (BTC/ETH/SOL)
  - Arb/quote signals log
  - Bot status + controls (start/stop, dry-run toggle)
  - Position summary
  - Trade log

Requires: pip install flask
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env FIRST so its values are in os.environ before anything else reads them.
# setdefault below only fills gaps not covered by .env.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)   # won't overwrite vars already set by the OS shell
except ImportError:
    pass

os.environ.setdefault("DRY_RUN",     "true")   # safe fallback if .env omits it
os.environ.setdefault("KALSHI_DEMO", "false")  # default to live API if .env omits it

# Stub websocket if not installed so app still loads
try:
    import websocket  # noqa
except ImportError:
    from unittest.mock import MagicMock
    import sys
    sys.modules["websocket"] = MagicMock()

import engine
import trader

from flask import Flask, Response, jsonify, render_template_string, request

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

# ── Shared state ──────────────────────────────────────────────────────────────
_bot: engine.BotEngine = None
_quote_managers = {a: trader.QuoteManager(a) for a in engine.ASSETS}
_sse_clients: list = []
_sse_lock = threading.Lock()
_event_queue: queue.Queue = queue.Queue(maxsize=500)
_bot_lock = threading.Lock()

BOT_STATE = {
    "status":      "stopped",
    "dry_run":     os.getenv("DRY_RUN", "true").lower() != "false",
    "demo":        os.getenv("KALSHI_DEMO", "true").lower() != "false",
    "arb_count":   0,
    "update_count": 0,
    "started_at":  None,
    "markets":     {},
    "snapshots":   {},
    "arb_stats":   {a: {} for a in engine.ASSETS},
    "log":         [],   # last 200 log lines
}

def _push(event_type: str, data: dict):
    """Push an SSE event to all connected clients."""
    payload = json.dumps({"type": event_type, "ts": time.time(), **data})
    try:
        _event_queue.put_nowait(payload)
    except queue.Full:
        pass

def _push_positions():
    """Push current in-memory position data to the dashboard immediately."""
    with trader.POSITIONS._lock:
        _push("positions", {"positions_data": dict(trader.POSITIONS._data)})

def _add_log(icon: str, msg: str):
    entry = {
        "ts":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "icon": icon,
        "msg":  msg,
    }
    BOT_STATE["log"].append(entry)
    if len(BOT_STATE["log"]) > 200:
        BOT_STATE["log"] = BOT_STATE["log"][-200:]
    _push("log", entry)

# ── Bot callbacks ─────────────────────────────────────────────────────────────
def _on_log(icon: str, msg: str):
    _add_log(icon, msg)

def _on_prices(markets: dict, snapshots: dict):
    BOT_STATE["markets"]   = markets
    BOT_STATE["snapshots"] = snapshots
    if _bot:
        BOT_STATE["update_count"] = _bot.update_count
        BOT_STATE["arb_stats"]    = _bot.get_arb_stats()
    _push("prices", {"markets": markets, "snapshots": snapshots,
                     "arb_stats": BOT_STATE["arb_stats"]})

def _on_arb(snap):
    BOT_STATE["arb_count"] += 1
    data = snap.to_dict()
    _push("arb", data)
    _add_log("⚡", (
        f"ARB {snap.asset}  YES={snap.yes_ask}c  NO={snap.no_ask}c  "
        f"taker_gap=+{float(snap.taker_gap):.3f}c"
    ))
    # Execute (dry-run or live) via trader
    if _bot:
        mkt = _bot.markets.get(snap.asset)
        if mkt:
            def _exec_and_push(s, m):
                result = trader.execute_arb(s, m, _bot)
                if not result:
                    return
                status = result.get("status", "")
                _push("guardrails", trader.get_guardrail_status())
                _push("order_result", result)   # always push so dashboard reflects reality

                if status == "filled":
                    _push_positions()
                    _add_log("✅", (
                        f"FILLED {s.asset}  YES={result.get('yes_ask')}c  "
                        f"NO={result.get('no_ask')}c  "
                        f"gap=+{result.get('taker_gap', 0):.3f}c  n={result.get('n')}"
                    ))
                elif status == "dry_run":
                    _add_log("📋", (
                        f"DRY_RUN {s.asset}  YES={result.get('yes_ask')}c  "
                        f"NO={result.get('no_ask')}c  "
                        f"est_profit=${result.get('est_profit', 0):.4f}"
                    ))
                elif status == "unwound_partial":
                    _add_log("↩", (
                        f"UNWOUND {s.asset}  NO failed, YES sold back — "
                        f"{result.get('error', '')}"
                    ))
                elif status == "partial_unhedged":
                    _push_positions()
                    _add_log("🚨", (
                        f"UNHEDGED {s.asset}  YES open, NO+unwind failed — "
                        f"{result.get('error', '')}"
                    ))
                elif status == "error":
                    _add_log("✗", f"ORDER ERROR {s.asset}: {result.get('error', 'unknown')}")
                elif status.startswith("skipped_"):
                    _add_log("⏸", f"ARB {s.asset} skipped: {status}")
            threading.Thread(
                target=_exec_and_push,
                args=(snap, mkt),
                daemon=True,
            ).start()

def _on_status(status: str):
    BOT_STATE["status"] = status
    _push("status", {"status": status})

def _on_feed(asset: str, role: str, source: str):
    pass   # too noisy to push to UI

# ── Bot control ───────────────────────────────────────────────────────────────
def _start_bot():
    global _bot
    with _bot_lock:
        if _bot and _bot.is_running():
            return False, "already running"

        # Apply current dry_run setting to modules
        engine.DRY_RUN = BOT_STATE["dry_run"]
        trader.DRY_RUN = BOT_STATE["dry_run"]
        engine.USE_DEMO = BOT_STATE["demo"]

        _bot = engine.BotEngine(
            on_log    = _on_log,
            on_prices = _on_prices,
            on_arb    = _on_arb,
            on_status = _on_status,
            on_feed   = _on_feed,
        )
        BOT_STATE["arb_count"]   = 0
        BOT_STATE["update_count"] = 0
        BOT_STATE["started_at"]  = datetime.now(timezone.utc).isoformat()
        BOT_STATE["status"]      = "starting"

        # Pre-warm HTTP connection so the first arb order doesn't pay TCP+TLS cost.
        threading.Thread(target=engine.pre_warm_connection, daemon=True, name="http-prewarm").start()
        threading.Thread(target=_bot.start, daemon=True, name="bot-start").start()
        _add_log("→", f"Bot starting  demo={BOT_STATE['demo']}  dry_run={BOT_STATE['dry_run']}")
        return True, "ok"

def _stop_bot():
    global _bot
    with _bot_lock:
        if _bot:
            _bot.stop()
            BOT_STATE["status"] = "stopped"
            _add_log("■", "Bot stopped")
            return True, "ok"
        return False, "not running"

# ── SSE stream ────────────────────────────────────────────────────────────────
def _sse_generator():
    """Generator that yields SSE messages from the shared queue."""
    # Send current state immediately on connect
    yield f"data: {json.dumps({'type': 'init', **{k: v for k, v in BOT_STATE.items() if k != 'log'}})}\n\n"
    for entry in BOT_STATE["log"][-50:]:
        yield f"data: {json.dumps({'type': 'log', **entry})}\n\n"

    last_heartbeat = time.time()
    while True:
        try:
            payload = _event_queue.get(timeout=1.0)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            pass
        # Heartbeat every 15s to keep connection alive
        if time.time() - last_heartbeat > 15:
            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': time.time()})}\n\n"
            last_heartbeat = time.time()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/stream")
def stream():
    return Response(_sse_generator(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = _start_bot()
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = _stop_bot()
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/state")
def api_state():
    state = dict(BOT_STATE)
    state["log"] = state["log"][-50:]
    if _bot:
        state["update_count"] = _bot.update_count
        state["arb_count"]    = _bot.arb_count
    return jsonify(state)

@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json() or {}
    if "dry_run" in data:
        BOT_STATE["dry_run"] = bool(data["dry_run"])
        engine.DRY_RUN = BOT_STATE["dry_run"]
        trader.DRY_RUN = BOT_STATE["dry_run"]
    if "demo" in data:
        BOT_STATE["demo"] = bool(data["demo"])
    _add_log("⚙", f"Config updated: dry_run={BOT_STATE['dry_run']}  demo={BOT_STATE['demo']}")
    return jsonify({"ok": True, "dry_run": BOT_STATE["dry_run"], "demo": BOT_STATE["demo"]})

@app.route("/api/monitor_stats")
def api_monitor_stats():
    if _bot:
        return jsonify(_bot.get_arb_stats())
    return jsonify(BOT_STATE.get("arb_stats", {}))

@app.route("/api/guardrails")
def api_guardrails():
    return jsonify(trader.get_guardrail_status())

@app.route("/api/reset_halt", methods=["POST"])
def api_reset_halt():
    trader.reset_halt()
    _add_log("⚙", "Circuit breaker manually reset — arb execution re-enabled")
    return jsonify({"ok": True})

@app.route("/api/positions")
def api_positions():
    with trader.POSITIONS._lock:
        return jsonify(dict(trader.POSITIONS._data))

@app.route("/api/trades")
def api_trades():
    try:
        tf = trader.TRADES_FILE
        if Path(tf).exists():
            lines = Path(tf).read_text().strip().splitlines()
            trades = [json.loads(l) for l in lines[-100:] if l.strip()]
            return jsonify({"trades": list(reversed(trades))})
    except Exception:
        pass
    return jsonify({"trades": []})

# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Arb Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #0a0d11;
  --surf:    #0f1419;
  --surf2:   #141c24;
  --border:  #1e2a38;
  --border2: #28394e;
  --text:    #cdd9e5;
  --muted:   #4d6478;
  --dim:     #283848;
  --accent:  #00d4aa;
  --up:      #4da6ff;
  --down:    #ff6b6b;
  --warn:    #f5a623;
  --ok:      #22c55e;
  --arb:     #c084fc;
  --arb-bg:  rgba(192,132,252,.07);
  --arb-glow:rgba(192,132,252,.28);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:13px;overflow:hidden}

/* ── Layout ── */
.shell{display:grid;grid-template-rows:46px 1fr;height:100vh}
.main{display:grid;grid-template-columns:310px 1fr 290px;grid-template-rows:1fr 190px;
  gap:1px;background:var(--border);overflow:hidden;height:100%}
.spacer{flex:1}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;gap:14px;padding:0 20px;
  border-bottom:1px solid var(--border);background:var(--surf)}
.logo{font-size:15px;font-weight:600;letter-spacing:.2em;color:var(--accent);text-transform:uppercase}
.logo em{color:var(--dim);font-style:normal}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:all .3s}
.sdot.run{background:var(--accent);box-shadow:0 0 7px var(--accent);animation:pulse 2s infinite}
.sdot.disc{background:var(--warn)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
#slabel{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.tag{padding:3px 8px;border-radius:3px;font-size:9px;letter-spacing:.12em;font-weight:600;text-transform:uppercase}
.tag-demo{background:rgba(245,166,35,.1);color:var(--warn);border:1px solid rgba(245,166,35,.25)}
.tag-dry {background:rgba(77,166,255,.1);color:var(--up);  border:1px solid rgba(77,166,255,.25)}
.tag-live{background:rgba(34,197,94,.1); color:var(--ok);  border:1px solid rgba(34,197,94,.25)}
.ctr{display:flex;flex-direction:column;align-items:flex-end;gap:1px}
.ctr-val{font-size:17px;font-weight:500;line-height:1}
.ctr-lbl{font-size:8px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.btn{padding:5px 14px;border-radius:4px;border:1px solid var(--border2);background:transparent;
  color:var(--text);font-family:inherit;font-size:10px;letter-spacing:.1em;cursor:pointer;
  text-transform:uppercase;transition:all .15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.on{background:rgba(0,212,170,.12);border-color:var(--accent);color:var(--accent)}
.btn.stop{border-color:var(--down);color:var(--down)}
.btn.stop:hover{background:rgba(255,107,107,.12)}
.btn-sm{padding:3px 9px;font-size:9px}

/* ── Panel base ── */
.panel{background:var(--bg);display:flex;flex-direction:column;overflow:hidden;min-height:0}
.ph{display:flex;align-items:center;gap:10px;padding:10px 16px;
  border-bottom:1px solid var(--border);background:var(--surf);flex-shrink:0}
.pt{font-size:9px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.pb{flex:1;overflow-y:auto;padding:12px 16px;min-height:0}
.pb::-webkit-scrollbar{width:3px}
.pb::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.no-data{color:var(--muted);font-size:11px;padding:28px 16px;text-align:center;line-height:1.8}

/* ── Asset cards ── */
.panel-assets{grid-row:1/3}
.asset-wrap{display:flex;flex-direction:column;gap:12px;padding:14px}

.acard{border:1px solid var(--border);border-radius:8px;background:var(--surf);
  overflow:hidden;transition:border-color .3s,box-shadow .3s}
.acard.arb-live{border-color:var(--arb);box-shadow:0 0 18px var(--arb-glow)}
.acard-head{display:flex;align-items:center;gap:10px;padding:11px 15px;
  border-bottom:1px solid var(--border)}
.aname{font-size:22px;font-weight:600;color:var(--accent);letter-spacing:-.02em;min-width:52px}
.aticker{font-size:9px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.atimer{font-size:11px;padding:2px 8px;border-radius:3px;background:var(--surf2);
  border:1px solid var(--border);color:var(--muted);flex-shrink:0}
.atimer.warn{color:var(--warn);border-color:rgba(245,166,35,.35)}
.atimer.crit{color:var(--down);border-color:rgba(255,107,107,.35);animation:pulse 1s infinite}

.price-grid{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--border)}
.pgcol{padding:12px 15px}
.pgcol+.pgcol{border-left:1px solid var(--border)}
.pgcol-lbl{font-size:8px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);margin-bottom:5px}
.price-big{font-size:34px;font-weight:300;letter-spacing:-.04em;line-height:1}
.price-big sup{font-size:12px;font-weight:400;opacity:.45}
.price-big.up{color:var(--up)}
.price-big.dn{color:var(--down)}
.price-sub{display:flex;gap:10px;margin-top:5px;font-size:10px;color:var(--muted)}
.price-sub b{color:var(--text)}

.combo-row{display:flex;align-items:center;justify-content:space-between;
  padding:9px 15px;background:var(--surf2)}
.combo-sum{font-size:17px;font-weight:500}
.combo-sum.gap-exists{color:var(--arb)}
.combo-sum.no-gap{color:var(--muted)}
.combo-meta{font-size:9px;color:var(--muted);margin-top:1px}
.gap-val{font-size:15px;font-weight:600;text-align:right}
.gap-val.pos{color:var(--arb)}
.gap-val.neg{color:var(--muted)}
.gap-lbl{font-size:9px;color:var(--muted);text-align:right}

.arb-badge{display:flex;align-items:center;justify-content:center;gap:6px;padding:7px 15px;
  background:var(--arb-bg);border-top:1px solid rgba(192,132,252,.2);
  font-size:11px;font-weight:600;color:var(--arb);letter-spacing:.05em;
  animation:pulse 1.2s infinite}
.arb-badge.hidden{display:none}

/* ── Arb monitor (middle top) ── */
.panel-arb{grid-column:2;grid-row:1}

.mst{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--border);flex-shrink:0}
.mst-col{padding:10px 13px;border-right:1px solid var(--border)}
.mst-col:last-child{border-right:none}
.mst-head{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.mst-asset{font-size:12px;font-weight:600;color:var(--accent)}
.mst-badge{font-size:8px;padding:1px 5px;border-radius:2px;font-weight:600;letter-spacing:.08em}
.mst-badge.live{background:rgba(34,197,94,.12);color:var(--ok);border:1px solid rgba(34,197,94,.2)}
.mst-badge.idle{background:var(--surf2);color:var(--muted);border:1px solid var(--border)}
.mst-stat{display:flex;justify-content:space-between;padding:2px 0;font-size:10px}
.mst-lbl{color:var(--muted)}
.mst-val{font-weight:500}
.mst-val.hot{color:var(--arb)}
.mst-val.ok{color:var(--ok)}
.mst-none{font-size:10px;color:var(--muted);font-style:italic}

.sig-empty{color:var(--muted);font-size:11px;text-align:center;padding:36px 20px;line-height:1.9}
.sig-card{border:1px solid var(--border);border-radius:6px;background:var(--surf);
  margin-bottom:8px;overflow:hidden;animation:slidein .25s ease-out}
.sig-card.hot{border-color:var(--arb);background:var(--arb-bg)}
@keyframes slidein{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.sig-head{display:flex;align-items:center;gap:10px;padding:9px 13px;border-bottom:1px solid var(--border)}
.sig-asset{font-size:14px;font-weight:600;color:var(--accent)}
.sig-gap{font-size:14px;font-weight:600}
.sig-gap.pos{color:var(--arb)}
.sig-gap.neg{color:var(--muted)}
.sig-pill{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.08em}
.sig-pill.hot{background:rgba(192,132,252,.2);color:var(--arb);border:1px solid rgba(192,132,252,.3)}
.sig-pill.sub{background:var(--surf2);color:var(--muted);border:1px solid var(--border)}
.sig-ts{font-size:9px;color:var(--muted);margin-left:auto}
.sig-prices{display:grid;grid-template-columns:1fr 1fr 1fr}
.sp{padding:7px 13px;font-size:10px}
.sp+.sp{border-left:1px solid var(--border)}
.sp-lbl{color:var(--muted);font-size:9px;margin-bottom:2px}
.sp-val{font-size:12px;font-weight:500}
.sp-val.up{color:var(--up)}
.sp-val.dn{color:var(--down)}
.sp-val.ok{color:var(--ok)}
.sp-val.arb{color:var(--arb)}
.sp-val.dim{color:var(--muted)}

.scan-bar{display:flex;align-items:center;gap:18px;padding:7px 16px;
  border-top:1px solid var(--border);background:var(--surf);flex-shrink:0;font-size:10px;color:var(--muted)}
.scan-bar b{color:var(--text)}
.scan-bar .hot{color:var(--arb)}

/* ── Log ── */
.panel-log{grid-column:2;grid-row:2}
.log-entry{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid rgba(30,42,56,.5);align-items:baseline}
.le-ts{color:var(--muted);font-size:9px;flex-shrink:0;min-width:54px}
.le-icon{width:16px;text-align:center;flex-shrink:0}
.le-msg{color:var(--text);font-size:10px;word-break:break-all;line-height:1.55}
.le-msg.arb{color:var(--arb)}
.le-msg.err{color:var(--down)}
.le-msg.ok{color:var(--ok)}
.le-msg.dim{color:var(--muted)}
.conn-bar{display:flex;align-items:center;gap:6px;padding:5px 16px;font-size:9px;
  color:var(--muted);background:var(--surf);border-top:1px solid var(--border);flex-shrink:0}
.cdot{width:6px;height:6px;border-radius:50%;background:var(--muted);flex-shrink:0}
.cdot.ok{background:var(--accent);box-shadow:0 0 4px var(--accent)}

/* ── Right panel ── */
.panel-right{grid-column:3;grid-row:1/3}
.tab-bar{display:flex;gap:6px;padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0}
.tab-btn{flex:1;padding:5px;border-radius:4px;border:1px solid transparent;background:transparent;
  color:var(--muted);font-family:inherit;font-size:9px;letter-spacing:.12em;cursor:pointer;
  text-transform:uppercase;transition:all .15s}
.tab-btn.active{background:var(--surf2);border-color:var(--border2);color:var(--text)}
.tab-btn:hover:not(.active){color:var(--text)}

.pnl-strip{display:flex;gap:0;border-bottom:1px solid var(--border);flex-shrink:0}
.pnl-cell{flex:1;padding:11px 14px;border-right:1px solid var(--border)}
.pnl-cell:last-child{border-right:none}
.pnl-lbl{font-size:8px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.pnl-val{font-size:16px;font-weight:500}
.pnl-val.pos{color:var(--ok)}
.pnl-val.neg{color:var(--down)}

.pos-group{padding:8px 14px 4px;border-bottom:1px solid var(--border)}
.pos-ticker{font-size:9px;color:var(--muted);margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pos-legs{display:flex;gap:8px}
.pos-leg{flex:1;padding:6px 10px;border-radius:4px;background:var(--surf2);border:1px solid var(--border)}
.pos-side{font-size:10px;font-weight:600;margin-bottom:2px}
.pos-side.up{color:var(--up)}
.pos-side.dn{color:var(--down)}
.pos-cost{font-size:9px;color:var(--muted)}

.trade-item{padding:9px 14px;border-bottom:1px solid var(--border)}
.trade-header{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.tbadge{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.08em}
.tbadge.dry {background:rgba(77,166,255,.1); color:var(--up); border:1px solid rgba(77,166,255,.2)}
.tbadge.live{background:rgba(34,197,94,.1);  color:var(--ok); border:1px solid rgba(34,197,94,.2)}
.tbadge.err {background:rgba(255,107,107,.1);color:var(--down);border:1px solid rgba(255,107,107,.2)}
.trade-detail{font-size:11px;font-weight:500}
.trade-meta{font-size:9px;color:var(--muted);display:flex;gap:8px}
.trade-meta .pos-gap{color:var(--arb)}
</style>
</head>
<body>
<div class="shell">

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo">KALSHI<em>/</em>ARB</div>
  <div class="sdot" id="sdot"></div>
  <span id="slabel">—</span>
  <span id="tag-demo" class="tag tag-demo" style="display:none">DEMO</span>
  <span id="tag-dry"  class="tag tag-dry"  style="display:none">DRY RUN</span>
  <span id="tag-live" class="tag tag-live" style="display:none">LIVE</span>
  <div class="spacer"></div>
  <div style="display:flex;gap:22px;padding-right:10px;align-items:center">
    <div class="ctr"><div class="ctr-val" style="color:var(--accent)" id="c-updates">0</div><div class="ctr-lbl">Updates</div></div>
    <div class="ctr"><div class="ctr-val" style="color:var(--arb)"    id="c-arbs">0</div><div class="ctr-lbl">Arb Signals</div></div>
    <span style="font-size:9px;color:var(--muted)" id="last-upd"></span>
  </div>
  <button class="btn" id="btn-dry" onclick="toggleDryRun()">Dry Run: ON</button>
  <button class="btn"      id="btn-start" onclick="startBot()">▶ Start</button>
  <button class="btn stop" id="btn-stop"  onclick="stopBot()" style="display:none">■ Stop</button>
</div>

<div class="main">

  <!-- LEFT: Asset cards -->
  <div class="panel panel-assets">
    <div class="ph"><div class="pt">Markets — YES / NO asks</div></div>
    <div class="asset-wrap" id="asset-wrap">
      <div class="no-data">Start the bot to see live prices.</div>
    </div>
  </div>

  <!-- MIDDLE TOP: Arb monitor -->
  <div class="panel panel-arb">
    <div class="ph">
      <div class="pt">Arb Monitor — YES ask + NO ask &lt; 100¢</div>
      <div class="spacer"></div>
      <button class="btn btn-sm" onclick="clearArb()">Clear</button>
    </div>
    <div class="mst" id="mst-wrap">
      <div class="mst-col"><div class="mst-head"><span class="mst-asset">BTC</span><span class="mst-badge idle">WAITING</span></div><div class="mst-none">No data</div></div>
      <div class="mst-col"><div class="mst-head"><span class="mst-asset">ETH</span><span class="mst-badge idle">WAITING</span></div><div class="mst-none">No data</div></div>
      <div class="mst-col"><div class="mst-head"><span class="mst-asset">SOL</span><span class="mst-badge idle">WAITING</span></div><div class="mst-none">No data</div></div>
    </div>
    <div class="pb" id="arb-body">
      <div class="sig-empty" id="arb-empty">
        Watching for gaps…<br>
        Signal fires when <b style="color:var(--text)">YES ask + NO ask &lt; 100¢</b>
      </div>
      <div id="sig-cards"></div>
    </div>
    <div class="scan-bar">
      <span>Raw gaps: <b id="sc-total">0</b></span>
      <span>Profitable: <b class="hot" id="sc-hot">0</b></span>
      <span>Best: <b class="hot" id="sc-best">—</b></span>
    </div>
  </div>

  <!-- MIDDLE BOTTOM: Log -->
  <div class="panel panel-log">
    <div class="ph">
      <div class="pt">Event Log</div>
      <div class="spacer"></div>
      <button class="btn btn-sm" onclick="clearLog()">Clear</button>
    </div>
    <div class="pb" id="log-body"></div>
    <div class="conn-bar">
      <div class="cdot" id="cdot"></div>
      <span id="clabel">Disconnected</span>
    </div>
  </div>

  <!-- RIGHT: Positions + Trades -->
  <div class="panel panel-right">
    <div class="ph"><div class="pt" id="rtab-lbl">Positions</div></div>
    <div class="tab-bar">
      <button class="tab-btn active" id="btn-pos"    onclick="showTab('pos')">Positions</button>
      <button class="tab-btn"        id="btn-trades" onclick="showTab('trades')">Trades</button>
    </div>
    <div class="pb" id="pos-body" style="padding:0"></div>
    <div class="pb" id="trades-body" style="display:none;padding:0"></div>
  </div>

</div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let botRunning = false, dryRun = true, demo = true;
let currentTab = 'pos';
let snapshots = {}, markets = {}, arbStats = {};
let arbSignals = [];   // rolling 50 unique signals
let es = null;

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  es = new EventSource('/stream');
  es.onopen = () => { document.getElementById('cdot').classList.add('ok');
    document.getElementById('clabel').textContent = 'Connected'; };
  es.onerror = () => { document.getElementById('cdot').classList.remove('ok');
    document.getElementById('clabel').textContent = 'Reconnecting…';
    setTimeout(connectSSE, 3000); };
  es.onmessage = e => handleMsg(JSON.parse(e.data));
}

function handleMsg(msg) {
  if (msg.type === 'init') {
    dryRun = msg.dry_run; demo = msg.demo; updateTags();
    updateStatus(msg.status);
    snapshots = msg.snapshots || {}; markets = msg.markets || {};
    if (msg.arb_stats) { arbStats = msg.arb_stats; renderArbStats(); }
    renderAssets();
    (msg.log || []).forEach(addLog);
    document.getElementById('c-arbs').textContent    = msg.arb_count    || 0;
    document.getElementById('c-updates').textContent = msg.update_count || 0;
  } else if (msg.type === 'prices') {
    snapshots = msg.snapshots || {}; markets = msg.markets || {};
    if (msg.arb_stats) { arbStats = msg.arb_stats; renderArbStats(); }
    document.getElementById('last-upd').textContent =
      new Date().toLocaleTimeString('en-US',{hour12:false});
    renderAssets();
    checkArbSignals();
  } else if (msg.type === 'log') {
    addLog(msg);
  } else if (msg.type === 'status') {
    updateStatus(msg.status);
  } else if (msg.type === 'arb') {
    document.getElementById('c-arbs').textContent =
      parseInt(document.getElementById('c-arbs').textContent||'0') + 1;
    document.getElementById('card-'+msg.asset)?.classList.add('arb-live');
  } else if (msg.type === 'positions') {
    if (currentTab === 'pos') renderPositions(msg.positions_data || {});
  } else if (msg.type === 'order_result') {
    refreshRight();
    // Stamp the most-recent signal card for this asset with the execution outcome
    const st = msg.status || '';
    const asset = msg.asset || '';
    if (asset) {
      const idx = arbSignals.findIndex(x => x.asset === asset);
      if (idx !== -1 && !arbSignals[idx].execResult) {
        arbSignals[idx].execResult = st;
        renderSignalHistory();
      }
    }
  }
}

// ── Asset cards ───────────────────────────────────────────────────────────────
function renderAssets() {
  const wrap = document.getElementById('asset-wrap');
  const any = ['BTC','ETH','SOL'].some(a => snapshots[a]);
  if (!any) { wrap.innerHTML = '<div class="no-data">Waiting for market data…</div>'; return; }
  wrap.innerHTML = ['BTC','ETH','SOL'].map(renderCard).join('');
}

function renderCard(asset) {
  const s = snapshots[asset];
  if (!s) return `<div class="acard"><div class="acard-head"><div class="aname">${asset}</div><div class="aticker">No data</div></div></div>`;

  const yA = s.yes_ask, nA = s.no_ask, yB = s.yes_bid, nB = s.no_bid;
  const mid = s.mid != null ? parseFloat(s.mid) : null;
  const spr = s.spread;
  const secs = s.secs_left != null ? s.secs_left : '?';
  const ticker = (s.ticker||'').slice(-20);
  const combined = (yA != null && nA != null) ? yA + nA : null;
  const tGap = s.taker_gap != null ? parseFloat(s.taker_gap) : null;
  const isArb = tGap != null && tGap > 0;
  const rawGap = combined != null ? 100 - combined : null;

  const secsLabel = typeof secs==='number'
    ? (secs >= 60 ? Math.floor(secs/60)+'m '+(secs%60)+'s' : secs+'s') : secs+'s';
  const secsClass = secs < 60 ? 'crit' : secs < 120 ? 'warn' : '';

  const combCls = combined != null && combined < 100 ? 'gap-exists' : 'no-gap';
  const gapCls  = isArb ? 'pos' : 'neg';
  const tGapStr = tGap != null
    ? (tGap > 0 ? '+' : '') + tGap.toFixed(3) + '¢'
    : (rawGap != null ? rawGap.toFixed(0)+'¢ raw' : '—');

  return `
  <div class="acard ${isArb?'arb-live':''}" id="card-${asset}">
    <div class="acard-head">
      <div class="aname">${asset}</div>
      <div class="aticker">${ticker}</div>
      <div class="atimer ${secsClass}">${secsLabel}</div>
    </div>
    <div class="price-grid">
      <div class="pgcol">
        <div class="pgcol-lbl">YES ask — you pay</div>
        <div class="price-big up">${yA!=null?yA:'—'}<sup>¢</sup></div>
        <div class="price-sub">
          <span>bid <b>${yB!=null?yB+'¢':'—'}</b></span>
          <span>spread <b>${spr!=null?spr+'¢':'—'}</b></span>
        </div>
      </div>
      <div class="pgcol">
        <div class="pgcol-lbl">NO ask — you pay</div>
        <div class="price-big dn">${nA!=null?nA:'—'}<sup>¢</sup></div>
        <div class="price-sub">
          <span>bid <b>${nB!=null?nB+'¢':'—'}</b></span>
          <span>mid <b>${mid!=null?mid.toFixed(1)+'¢':'—'}</b></span>
        </div>
      </div>
    </div>
    <div class="combo-row">
      <div>
        <div class="combo-sum ${combCls}">${combined!=null?combined+'¢':'—'}</div>
        <div class="combo-meta">YES + NO combined</div>
      </div>
      <div>
        <div class="gap-val ${gapCls}">${tGapStr}</div>
        <div class="gap-lbl">${isArb?'taker profit after fees':'gap after fees'}</div>
      </div>
    </div>
    <div class="arb-badge ${isArb?'':'hidden'}">⚡ ARB — buy both sides for guaranteed profit</div>
  </div>`;
}

// ── Monitoring stats ───────────────────────────────────────────────────────────
function renderArbStats() {
  const wrap = document.getElementById('mst-wrap');
  if (!wrap) return;
  wrap.innerHTML = ['BTC','ETH','SOL'].map(a => {
    const s = arbStats[a] || {};
    const live = (s.profitable_count||0) > 0;
    if (!s.raw_gap_count) return `
      <div class="mst-col">
        <div class="mst-head"><span class="mst-asset">${a}</span><span class="mst-badge idle">WAITING</span></div>
        <div class="mst-none">No gaps yet</div>
      </div>`;
    return `
      <div class="mst-col">
        <div class="mst-head">
          <span class="mst-asset">${a}</span>
          <span class="mst-badge ${live?'live':'idle'}">${live?'LIVE':'WATCHING'}</span>
        </div>
        <div class="mst-stat"><span class="mst-lbl">Gaps / hr</span><span class="mst-val">${(s.gaps_per_hour||0).toFixed(1)}</span></div>
        <div class="mst-stat"><span class="mst-lbl">Avg gap</span><span class="mst-val">${(s.avg_gap||0).toFixed(2)}¢</span></div>
        <div class="mst-stat"><span class="mst-lbl">Max gap</span><span class="mst-val hot">${(s.max_gap||0).toFixed(2)}¢</span></div>
        <div class="mst-stat"><span class="mst-lbl">Profitable</span><span class="mst-val ${live?'hot':''}">${s.profitable_count||0} / ${s.raw_gap_count||0}</span></div>
        <div class="mst-stat"><span class="mst-lbl">Last gap</span><span class="mst-val">${s.last_gap!=null?s.last_gap.toFixed(2)+'¢':'—'}</span></div>
      </div>`;
  }).join('');
  const total = ['BTC','ETH','SOL'].reduce((n,a)=>n+(arbStats[a]?.raw_gap_count||0),0);
  const hot   = ['BTC','ETH','SOL'].reduce((n,a)=>n+(arbStats[a]?.profitable_count||0),0);
  const best  = ['BTC','ETH','SOL'].reduce((mx,a)=>{const g=arbStats[a]?.max_gap||0;return g>mx?g:mx;},0);
  document.getElementById('sc-total').textContent = total;
  document.getElementById('sc-hot').textContent   = hot;
  document.getElementById('sc-best').textContent  = best>0 ? '+'+best.toFixed(3)+'¢' : '—';
}

// ── Arb signal history ─────────────────────────────────────────────────────────
function checkArbSignals() {
  ['BTC','ETH','SOL'].forEach(asset => {
    const s = snapshots[asset];
    if (!s || s.yes_ask==null || s.no_ask==null) return;
    const yA = s.yes_ask, nA = s.no_ask;
    const combined = yA + nA;
    if (combined >= 100) return;
    const tGap = s.taker_gap != null ? parseFloat(s.taker_gap) : null;
    const mGap = s.maker_gap != null ? parseFloat(s.maker_gap) : null;
    const isHot = tGap != null && tGap > 0;
    const now = Date.now();
    // Dedup: skip same prices seen within the last 15 seconds for this asset
    const last = arbSignals.find(x => x.asset === asset);
    if (last && last.yA===yA && last.nA===nA && (now-last.rawTs)<15000) return;
    arbSignals.unshift({asset,yA,nA,combined,tGap,mGap,isHot,
      ts:new Date().toLocaleTimeString('en-US',{hour12:false}), rawTs:now});
    if (arbSignals.length > 50) arbSignals.pop();
    renderSignalHistory();
  });
}

function execLabel(s) {
  if (!s.isHot) return 'Fees not covered';
  const r = s.execResult;
  if (!r)                        return '⚡ Profitable — attempting';
  if (r === 'filled')            return '✅ Trade executed';
  if (r === 'dry_run')           return '📋 Dry-run logged';
  if (r === 'unwound_partial')   return '↩ Unwound (no fill)';
  if (r === 'partial_unhedged')  return '🚨 Unhedged — check positions';
  if (r.startsWith('skipped_'))  return '⏸ ' + r.replace('skipped_','').replace(/_/g,' ');
  return '✗ ' + r;
}
function execCls(s) {
  if (!s.isHot) return 'dim';
  const r = s.execResult;
  if (!r)                       return 'arb';
  if (r === 'filled')           return 'ok';
  if (r === 'dry_run')          return 'up';
  if (r === 'partial_unhedged') return 'dn';
  if (r && r !== 'unwound_partial' && !r.startsWith('skipped_')) return 'dn';
  return 'dim';
}

function renderSignalHistory() {
  const cards = document.getElementById('sig-cards');
  const empty = document.getElementById('arb-empty');
  if (!cards) return;
  if (!arbSignals.length) { if(empty) empty.style.display=''; cards.innerHTML=''; return; }
  if (empty) empty.style.display = 'none';
  const rawGapTotal = s => 100 - s.combined;
  cards.innerHTML = arbSignals.map(s => {
    const rawG  = rawGapTotal(s);
    const gapStr = s.tGap!=null ? (s.tGap>0?'+':'')+s.tGap.toFixed(3)+'¢' : rawG.toFixed(1)+'¢ raw';
    const mStr   = s.mGap!=null ? (s.mGap>0?'+':'')+s.mGap.toFixed(3)+'¢' : '—';
    return `
    <div class="sig-card ${s.isHot?'hot':''}">
      <div class="sig-head">
        <span class="sig-asset">${s.asset}</span>
        <span class="sig-gap ${s.isHot?'pos':'neg'}">${s.isHot?'⚡ ':''}${gapStr}</span>
        <span class="sig-pill ${s.isHot?'hot':'sub'}">${s.isHot?'PROFITABLE':'SUB-THRESHOLD'}</span>
        <span class="sig-ts">${s.ts}</span>
      </div>
      <div class="sig-prices">
        <div class="sp"><div class="sp-lbl">YES ask</div><div class="sp-val up">${s.yA}¢</div></div>
        <div class="sp"><div class="sp-lbl">NO ask</div><div class="sp-val dn">${s.nA}¢</div></div>
        <div class="sp"><div class="sp-lbl">Combined (vs 100¢)</div><div class="sp-val ${s.isHot?'arb':''}">${s.combined}¢ (−${rawG.toFixed(0)}¢)</div></div>
        <div class="sp"><div class="sp-lbl">Taker gap (after fees)</div><div class="sp-val ${s.isHot?'arb':'dim'}">${s.tGap!=null?(s.tGap>0?'+':'')+s.tGap.toFixed(3)+'¢':'—'}</div></div>
        <div class="sp"><div class="sp-lbl">Maker gap (after fees)</div><div class="sp-val">${mStr}</div></div>
        <div class="sp"><div class="sp-lbl">Result</div><div class="sp-val ${execCls(s)}">${execLabel(s)}</div></div>
      </div>
    </div>`;
  }).join('');
}

function clearArb() {
  arbSignals = [];
  const cards = document.getElementById('sig-cards');
  const empty = document.getElementById('arb-empty');
  if (cards) cards.innerHTML = '';
  if (empty) empty.style.display = '';
}

// ── Log ───────────────────────────────────────────────────────────────────────
function addLog(e) {
  const cls = e.icon==='⚡'?'arb':e.icon==='✗'?'err':e.icon==='✓'?'ok':e.icon==='→'?'dim':'';
  const body = document.getElementById('log-body');
  const d = document.createElement('div');
  d.className = 'log-entry';
  d.innerHTML = `<span class="le-ts">${e.ts||''}</span><span class="le-icon">${e.icon||'·'}</span><span class="le-msg ${cls}">${esc(e.msg||'')}</span>`;
  body.insertBefore(d, body.firstChild);
  while (body.children.length > 150) body.removeChild(body.lastChild);
}
function clearLog() { document.getElementById('log-body').innerHTML=''; }
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── Status ────────────────────────────────────────────────────────────────────
function updateStatus(st) {
  const dot = document.getElementById('sdot');
  dot.className = 'sdot';
  if (st==='monitoring'||st==='connected'){dot.classList.add('run');botRunning=true;}
  else if(['discovering','connecting','starting','reconnecting'].includes(st)) dot.classList.add('disc');
  else if(st==='waiting') dot.classList.add('disc');
  else if(st==='stopped') botRunning=false;
  document.getElementById('slabel').textContent = st.toUpperCase();
  document.getElementById('btn-start').style.display = botRunning?'none':'';
  document.getElementById('btn-stop').style.display  = botRunning?'':'none';
}
function updateTags() {
  document.getElementById('tag-demo').style.display = demo  ?'':'none';
  document.getElementById('tag-dry').style.display  = dryRun?'':'none';
  document.getElementById('tag-live').style.display = (!demo&&!dryRun)?'':'none';
  document.getElementById('btn-dry').textContent = 'Dry Run: '+(dryRun?'ON':'OFF');
  document.getElementById('btn-dry').classList.toggle('on', dryRun);
}

// ── Controls ──────────────────────────────────────────────────────────────────
async function startBot()  { await fetch('/api/start',{method:'POST'}); }
async function stopBot()   { await fetch('/api/stop', {method:'POST'}); }
async function toggleDryRun() {
  dryRun = !dryRun; updateTags();
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dry_run:dryRun,demo})});
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
function showTab(t) {
  currentTab = t;
  document.getElementById('pos-body').style.display    = t==='pos'   ?'':'none';
  document.getElementById('trades-body').style.display = t==='trades'?'':'none';
  document.getElementById('rtab-lbl').textContent = t==='pos'?'Positions':'Trades';
  document.getElementById('btn-pos').classList.toggle('active',    t==='pos');
  document.getElementById('btn-trades').classList.toggle('active', t==='trades');
}

// ── Right panel data ──────────────────────────────────────────────────────────
async function refreshRight() {
  if (currentTab==='pos') {
    const d = await fetch('/api/positions').then(r=>r.json()).catch(()=>({}));
    renderPositions(d);
  } else {
    const d = await fetch('/api/trades').then(r=>r.json()).catch(()=>({trades:[]}));
    renderTrades(d.trades||[]);
  }
}

function renderPositions(d) {
  const body = document.getElementById('pos-body');
  const pos  = d.positions || {};
  const keys = Object.keys(pos);
  const pnl  = d.realised_pnl || 0;
  let html = `<div class="pnl-strip">
    <div class="pnl-cell">
      <div class="pnl-lbl">Realised P&amp;L</div>
      <div class="pnl-val ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(4)}</div>
    </div>
    <div class="pnl-cell">
      <div class="pnl-lbl">Total Fills</div>
      <div class="pnl-val">${d.total_fills||0}</div>
    </div>
  </div>`;
  if (!keys.length) {
    html += '<div class="no-data">No open positions</div>';
  } else {
    keys.forEach(tk => {
      const p = pos[tk];
      const short = tk.length>24?'…'+tk.slice(-22):tk;
      const hedged = Math.min(p.yes_contracts||0, p.no_contracts||0);
      // locked profit = ($1 payout - cost per YES - cost per NO) × hedged contracts
      const lockedPnl = hedged > 0 && p.avg_yes_cost && p.avg_no_cost
        ? (1 - p.avg_yes_cost - p.avg_no_cost) * hedged : null;
      const pnlStr = lockedPnl != null
        ? `<span style="color:var(--ok);font-size:9px">locked +$${lockedPnl.toFixed(4)}</span>` : '';
      html += `<div class="pos-group"><div class="pos-ticker" style="display:flex;justify-content:space-between">`
            + `<span>${short}</span>${pnlStr}</div><div class="pos-legs">`;
      if (p.yes_contracts>0) html += `<div class="pos-leg"><div class="pos-side up">YES ×${p.yes_contracts}</div><div class="pos-cost">avg ${p.avg_yes_cost?(p.avg_yes_cost*100).toFixed(1)+'¢':'—'}</div></div>`;
      if (p.no_contracts>0)  html += `<div class="pos-leg"><div class="pos-side dn">NO ×${p.no_contracts}</div><div class="pos-cost">avg ${p.avg_no_cost?(p.avg_no_cost*100).toFixed(1)+'¢':'—'}</div></div>`;
      html += `</div></div>`;
    });
  }
  body.innerHTML = html;
}

function renderTrades(trades) {
  const body = document.getElementById('trades-body');
  if (!trades.length) { body.innerHTML='<div class="no-data">No trades yet</div>'; return; }
  body.innerHTML = trades.slice(0,50).map(t => {
    const bc  = t.dry_run?'dry':t.status==='filled'?'live':'err';
    const lbl = t.dry_run?'DRY':(t.status||'?').toUpperCase();
    const det = t.side
      ? `${t.side==='yes'?'YES':'NO'} ${t.price_cents}¢ × ${t.n_contracts}`
      : `YES ${t.yes_ask}¢  NO ${t.no_ask}¢`;
    const gStr = t.taker_gap!=null
      ? `gap ${parseFloat(t.taker_gap)>0?'+':''}${parseFloat(t.taker_gap).toFixed(3)}¢` : '';
    return `<div class="trade-item">
      <div class="trade-header">
        <span class="tbadge ${bc}">${lbl}</span>
        <span class="trade-detail">${esc(t.asset||'')} — ${esc(det)}</span>
      </div>
      <div class="trade-meta">
        <span>${(t.ts||'').slice(11,19)}</span>
        ${gStr?`<span class="${parseFloat(t.taker_gap||0)>0?'pos-gap':''}">${gStr}</span>`:''}
        ${t.error?`<span style="color:var(--down)">${esc(t.error)}</span>`:''}
      </div>
    </div>`;
  }).join('');
}

// ── Periodic refresh ──────────────────────────────────────────────────────────
setInterval(() => {
  refreshRight();
  if (botRunning) {
    fetch('/api/state').then(r=>r.json()).then(d => {
      document.getElementById('c-updates').textContent = d.update_count||0;
      document.getElementById('c-arbs').textContent    = d.arb_count||0;
    }).catch(()=>{});
  }
}, 3000);

updateTags(); connectSSE(); refreshRight();
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    creds_ok = bool(engine.KALSHI_KEY_ID and Path(engine.KALSHI_KEY_FILE).exists())
    if not creds_ok:
        print("\n" + "="*60)
        print("  KALSHI BOT — Starting in DRY RUN / DEMO mode")
        print("  No credentials found. Set in .env:")
        print("    KALSHI_KEY_ID=your-key-id")
        print("    KALSHI_KEY_FILE=kalshi.key")
        print("="*60 + "\n")
        os.environ["DRY_RUN"]     = "true"
        os.environ["KALSHI_DEMO"] = "true"
        BOT_STATE["dry_run"] = True
        BOT_STATE["demo"]    = True

    print(f"  Dashboard → http://localhost:5001")
    print(f"  Mode: {'DEMO' if BOT_STATE['demo'] else 'LIVE'}  |  {'DRY RUN' if BOT_STATE['dry_run'] else 'REAL ORDERS'}\n")

    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True, use_reloader=False)