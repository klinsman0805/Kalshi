"""
app.py — Kalshi Momentum Bot Dashboard
Run:  python app.py
Open: http://localhost:5001
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

os.environ.setdefault("DRY_RUN",     "true")
os.environ.setdefault("KALSHI_DEMO", "false")

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
_momentum_traders: dict = {}
_event_queue: queue.Queue = queue.Queue(maxsize=500)
_bot_lock = threading.Lock()

BOT_STATE = {
    "status":             "stopped",
    "dry_run":            os.getenv("DRY_RUN",     "true").lower()  != "false",
    "demo":               os.getenv("KALSHI_DEMO", "true").lower()  != "false",
    "update_count":       0,
    "started_at":         None,
    "markets":            {},
    "snapshots":          {},
    "log":                [],
    "enabled_assets":     list(engine.ASSETS),
    "session_pnl":        0.0,
    "session_trades":     0,
    "session_wins":       0,
    "momentum_positions": {a: None for a in engine.ASSETS},
}

def _push(event_type: str, data: dict):
    payload = json.dumps({"type": event_type, "ts": time.time(), **data})
    try:
        _event_queue.put_nowait(payload)
    except queue.Full:
        pass

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

def _on_momentum_update(asset: str, position):
    BOT_STATE["momentum_positions"][asset] = position
    _push("momentum_update", {"asset": asset, "position": position})

for _a in engine.ASSETS:
    _momentum_traders[_a] = trader.MomentumTrader(
        _a,
        on_log=lambda ic, msg: _add_log(ic, msg),
        on_update=_on_momentum_update,
    )

def _on_prices(markets: dict, snapshots: dict):
    BOT_STATE["markets"]   = markets
    BOT_STATE["snapshots"] = snapshots
    if _bot:
        BOT_STATE["update_count"] = _bot.update_count
    _push("prices", {"markets": markets, "snapshots": snapshots})

    if _bot:
        for asset in BOT_STATE["enabled_assets"]:
            snap = _bot.get_snapshot(asset)
            mkt  = markets.get(asset)
            if snap and mkt:
                _momentum_traders[asset].update(snap, mkt)

def _on_status(status: str):
    BOT_STATE["status"] = status
    _push("status", {"status": status})

# ── Bot control ───────────────────────────────────────────────────────────────
def _start_bot():
    global _bot
    with _bot_lock:
        if _bot and _bot.is_running():
            return False, "already running"
        engine.DRY_RUN  = BOT_STATE["dry_run"]
        trader.DRY_RUN  = BOT_STATE["dry_run"]
        engine.USE_DEMO = BOT_STATE["demo"]
        _bot = engine.BotEngine(
            on_log    = _on_log,
            on_prices = _on_prices,
            on_status = _on_status,
        )
        BOT_STATE["update_count"]   = 0
        BOT_STATE["started_at"]     = datetime.now(timezone.utc).isoformat()
        BOT_STATE["session_pnl"]    = 0.0
        BOT_STATE["session_trades"] = 0
        BOT_STATE["session_wins"]   = 0
        BOT_STATE["status"]         = "starting"
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
    init_data = {k: v for k, v in BOT_STATE.items() if k != "log"}
    yield f"data: {json.dumps({'type': 'init', **init_data})}\n\n"
    for entry in BOT_STATE["log"][-50:]:
        yield f"data: {json.dumps({'type': 'log', **entry})}\n\n"
    last_heartbeat = time.time()
    while True:
        try:
            payload = _event_queue.get(timeout=1.0)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            pass
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
    _push("config", {"dry_run": BOT_STATE["dry_run"], "demo": BOT_STATE["demo"]})
    return jsonify({"ok": True, "dry_run": BOT_STATE["dry_run"], "demo": BOT_STATE["demo"]})

@app.route("/api/assets", methods=["POST"])
def api_assets():
    data  = request.get_json() or {}
    asset = data.get("asset")
    enabled = data.get("enabled")
    if asset in engine.ASSETS and isinstance(enabled, bool):
        ea = BOT_STATE["enabled_assets"]
        if enabled and asset not in ea:
            ea.append(asset)
        elif not enabled and asset in ea:
            ea.remove(asset)
        _add_log("⚙", f"{asset} {'enabled' if enabled else 'disabled'}")
        _push("config", {"enabled_assets": BOT_STATE["enabled_assets"]})
    return jsonify({"ok": True, "enabled_assets": BOT_STATE["enabled_assets"]})

@app.route("/api/positions")
def api_positions():
    with trader.POSITIONS._lock:
        return jsonify(dict(trader.POSITIONS._data))

@app.route("/api/trades")
def api_trades():
    try:
        tf = trader.TRADES_FILE
        if Path(tf).exists():
            lines  = Path(tf).read_text().strip().splitlines()
            trades = [json.loads(l) for l in lines[-100:] if l.strip()]
            return jsonify({"trades": list(reversed(trades))})
    except Exception:
        pass
    return jsonify({"trades": []})

@app.route("/api/momentum_positions")
def api_momentum_positions():
    positions = {a: _momentum_traders[a].get_position() for a in engine.ASSETS}
    return jsonify({"momentum_positions": positions})

# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Momentum Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0d11;--surf:#0f1419;--surf2:#141c24;--border:#1e2a38;--border2:#28394e;
  --text:#cdd9e5;--muted:#4d6478;--dim:#283848;--accent:#00d4aa;--up:#4da6ff;
  --down:#ff6b6b;--warn:#f5a623;--ok:#22c55e;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:13px;overflow:hidden}
.shell{display:flex;flex-direction:column;height:100vh}
.spacer{flex:1}

/* ── Topbar ── */
.topbar{display:flex;align-items:center;gap:12px;padding:0 18px;height:46px;
  border-bottom:1px solid var(--border);background:var(--surf);flex-shrink:0}
.logo{font-size:15px;font-weight:600;letter-spacing:.2em;color:var(--accent);text-transform:uppercase}
.logo em{color:var(--dim);font-style:normal}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:all .3s}
.sdot.run{background:var(--accent);box-shadow:0 0 7px var(--accent);animation:pulse 2s infinite}
.sdot.disc{background:var(--warn)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#slabel{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.data-dot-wrap{display:flex;align-items:center;gap:6px;font-size:9px;color:var(--muted)}
.data-dot{width:7px;height:7px;border-radius:50%;background:var(--down);flex-shrink:0;transition:background .5s}
.data-dot.live{background:var(--ok);box-shadow:0 0 5px var(--ok)}
.mode-toggle{display:flex;align-items:center;background:var(--surf2);border:1px solid var(--border2);
  border-radius:20px;padding:3px;gap:2px}
.mode-btn{padding:3px 12px;border-radius:17px;border:none;background:transparent;
  font-family:inherit;font-size:9px;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;
  color:var(--muted);transition:all .2s}
.mode-btn.active-monitor{background:rgba(77,166,255,.15);color:var(--up)}
.mode-btn.active-trade{background:rgba(255,107,107,.15);color:var(--down)}
.mode-btn:hover:not(.active-monitor):not(.active-trade){color:var(--text)}
.btn{padding:5px 16px;border-radius:4px;border:1px solid var(--border2);background:transparent;
  color:var(--text);font-family:inherit;font-size:10px;letter-spacing:.1em;cursor:pointer;
  text-transform:uppercase;transition:all .15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn-stop{border-color:var(--down)!important;color:var(--down)!important}
.btn-stop:hover{background:rgba(255,107,107,.1)!important}

/* ── Stats bar ── */
.stats-bar{display:flex;align-items:stretch;height:52px;border-bottom:1px solid var(--border);
  background:var(--surf);flex-shrink:0}
.stat-cell{flex:1;display:flex;flex-direction:column;justify-content:center;padding:0 20px;
  border-right:1px solid var(--border)}
.stat-cell:last-child{border-right:none}
.stat-lbl{font-size:8px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.stat-val{font-size:18px;font-weight:500;line-height:1}
.stat-val.pos{color:var(--ok)}
.stat-val.neg{color:var(--down)}
.stat-val.neutral{color:var(--text)}

/* ── Markets row ── */
.markets-row{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--border);flex-shrink:0}
.mcard{background:var(--surf);padding:10px 14px;display:flex;flex-direction:column;gap:7px}
.mcard+.mcard{border-left:1px solid var(--border)}
.mcard-top{display:flex;align-items:center;gap:8px}
.masset{font-size:16px;font-weight:600;color:var(--accent)}
.mticker{font-size:9px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mtimer{font-size:10px;padding:1px 6px;border-radius:3px;background:var(--surf2);
  border:1px solid var(--border);color:var(--muted);white-space:nowrap;flex-shrink:0}
.mtimer.warn{color:var(--warn);border-color:rgba(245,166,35,.35)}
.mtimer.crit{color:var(--down);border-color:rgba(255,107,107,.35);animation:pulse 1s infinite}
.mcard-prices{display:grid;grid-template-columns:1fr 1fr 1fr}
.mprice-col{padding:0 10px 0 0}
.mprice-col+.mprice-col{padding-left:10px;border-left:1px solid var(--border)}
.mprice-lbl{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.mprice-ask{font-size:20px;font-weight:500;line-height:1}
.mprice-ask.up{color:var(--up)}
.mprice-ask.dn{color:var(--down)}
.mprice-ask.hot{color:var(--warn)}
.mprice-sub{font-size:9px;color:var(--muted);margin-top:1px}
.mprice-sub b{color:var(--text)}
.sw{display:inline-flex;align-items:center;cursor:pointer}
.sw-track{width:26px;height:14px;border-radius:7px;background:var(--border2);
  position:relative;transition:background .2s;flex-shrink:0}
.sw-track::after{content:'';position:absolute;top:2px;left:2px;width:10px;height:10px;
  border-radius:50%;background:var(--muted);transition:transform .2s,background .2s}
.sw input{display:none}
.sw input:checked + .sw-track{background:rgba(0,212,170,.35)}
.sw input:checked + .sw-track::after{transform:translateX(12px);background:var(--accent)}

/* ── Momentum positions row ── */
#momentum-row{border-bottom:1px solid var(--border);background:var(--surf);
  padding:7px 14px;flex-shrink:0}
.mom-row-inner{display:flex;align-items:center;gap:14px}
.mom-label{font-size:8px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--muted);flex-shrink:0;min-width:80px}
.mom-cards{display:flex;gap:8px;flex:1;flex-wrap:wrap}
.mom-card{display:flex;align-items:center;gap:5px;padding:3px 8px;border-radius:3px;
  font-size:9px;border:1px solid var(--border2);background:var(--surf2)}
.mom-card.holding{border-color:rgba(245,166,35,.4);background:rgba(245,166,35,.06)}
.mom-card.hedged{border-color:rgba(77,166,255,.4);background:rgba(77,166,255,.06)}
.mom-card.closed{border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.06)}
.mom-cfg{font-size:9px;color:var(--muted);flex-shrink:0;margin-left:auto;white-space:nowrap}

/* ── Main area ── */
.main-area{display:flex;flex-direction:column;flex:1;overflow:hidden;min-height:0}
.content-cols{display:grid;grid-template-columns:1fr 1fr;flex:1;gap:1px;
  background:var(--border);overflow:hidden;min-height:0}
.panel{background:var(--bg);display:flex;flex-direction:column;overflow:hidden;min-height:0}
.ph{display:flex;align-items:center;gap:10px;padding:8px 14px;
  border-bottom:1px solid var(--border);background:var(--surf);flex-shrink:0}
.pt{font-size:9px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.pb{flex:1;overflow-y:auto;padding:10px 14px;min-height:0}
.pb::-webkit-scrollbar{width:3px}
.pb::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.no-data{color:var(--muted);font-size:11px;padding:28px 16px;text-align:center;line-height:1.9}

/* ── Momentum status cards (left panel) ── */
.ms-card{border:1px solid var(--border);border-radius:5px;background:var(--surf);
  margin-bottom:8px;overflow:hidden}
.ms-card.watching{border-color:rgba(245,166,35,.35);background:rgba(245,166,35,.03)}
.ms-card.holding{border-color:rgba(245,166,35,.5);background:rgba(245,166,35,.05)}
.ms-card.hedged{border-color:rgba(77,166,255,.5);background:rgba(77,166,255,.05)}
.ms-card.closed{border-color:rgba(34,197,94,.4);background:rgba(34,197,94,.04)}
.ms-head{display:flex;align-items:center;gap:8px;padding:7px 11px;border-bottom:1px solid var(--border)}
.ms-asset{font-size:13px;font-weight:600;color:var(--accent)}
.ms-ticker{font-size:9px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-timer{font-size:10px;color:var(--muted);flex-shrink:0}
.ms-timer.warn{color:var(--warn)}
.ms-timer.crit{color:var(--down)}
.ms-phase{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.1em;flex-shrink:0}
.ms-phase.dim{background:var(--surf2);color:var(--muted);border:1px solid var(--border)}
.ms-phase.warn{background:rgba(245,166,35,.15);color:var(--warn);border:1px solid rgba(245,166,35,.3)}
.ms-phase.up{background:rgba(77,166,255,.15);color:var(--up);border:1px solid rgba(77,166,255,.3)}
.ms-phase.ok{background:rgba(34,197,94,.15);color:var(--ok);border:1px solid rgba(34,197,94,.3)}
.ms-prices{display:grid;grid-template-columns:1fr 1fr 1fr;padding:6px 0 2px}
.ms-pcol{padding:4px 11px}
.ms-pcol+.ms-pcol{border-left:1px solid var(--border)}
.ms-plbl{font-size:8px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.ms-pval{font-size:17px;font-weight:500;line-height:1}
.ms-pval.up{color:var(--up)}
.ms-pval.dn{color:var(--down)}
.ms-pval.hot{color:var(--warn)}
.ms-pval.dim{color:var(--muted)}
.ms-psub{font-size:9px;color:var(--muted);margin-top:1px}
.ms-psub b{color:var(--text)}
.ms-pos{display:flex;align-items:center;gap:8px;padding:5px 11px;
  border-top:1px solid var(--border);font-size:10px;flex-wrap:wrap}
.ms-pos-side.up{color:var(--up)}
.ms-pos-side.dn{color:var(--down)}
.ms-pos-bid{color:var(--muted)}
.ms-pos-pnl.pos{color:var(--ok)}
.ms-pos-pnl.neg{color:var(--down)}
.ms-pos-tp{color:var(--muted)}
.ms-pos-tp.ok{color:var(--ok);font-weight:600}
.ms-pos-hedge{color:var(--up)}

/* ── Trade history ── */
.trade-row{display:flex;align-items:center;gap:10px;padding:7px 14px;
  border-bottom:1px solid var(--border);font-size:10px}
.tbadge{font-size:8px;padding:2px 6px;border-radius:2px;font-weight:600;letter-spacing:.08em;flex-shrink:0}
.tbadge.dry{background:rgba(77,166,255,.1);color:var(--up);border:1px solid rgba(77,166,255,.2)}
.tbadge.live{background:rgba(34,197,94,.1);color:var(--ok);border:1px solid rgba(34,197,94,.2)}
.tbadge.err{background:rgba(255,107,107,.1);color:var(--down);border:1px solid rgba(255,107,107,.2)}
.trade-detail{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trade-pnl{color:var(--ok);font-size:10px;flex-shrink:0}
.trade-ts{color:var(--muted);font-size:9px;flex-shrink:0}

/* ── Log panel ── */
#log-panel{border-top:2px solid var(--border2);background:var(--surf);flex-shrink:0;
  display:flex;flex-direction:column;height:46px;min-height:46px}
#log-panel.expanded{height:var(--log-h,220px)}
#log-drag{height:5px;cursor:ns-resize;flex-shrink:0;border-top:2px solid transparent;
  transition:border-color .15s;margin-top:-2px}
#log-drag:hover{border-top-color:var(--accent)}
.log-ph{display:flex;align-items:center;gap:10px;padding:6px 14px;flex-shrink:0}
.log-pt{font-size:9px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
#log-body{flex:1;overflow-y:auto;padding:4px 14px 6px;min-height:0;display:none}
#log-panel.expanded #log-body{display:block}
#log-panel.expanded .log-ph{border-bottom:1px solid var(--border)}
.log-toggle{font-size:9px;cursor:pointer;color:var(--muted);padding:2px 8px;border-radius:3px;
  border:1px solid var(--border);background:transparent;font-family:inherit;letter-spacing:.08em;
  text-transform:uppercase;transition:all .15s}
.log-toggle:hover{border-color:var(--border2);color:var(--text)}
.log-entry{display:flex;gap:8px;padding:2px 0;border-bottom:1px solid rgba(30,42,56,.5);align-items:baseline}
.le-ts{color:var(--muted);font-size:9px;flex-shrink:0;min-width:54px}
.le-icon{width:16px;text-align:center;flex-shrink:0}
.le-msg{color:var(--text);font-size:10px;word-break:break-all;line-height:1.5}
.le-msg.err{color:var(--down)}
.le-msg.ok{color:var(--ok)}
.le-msg.dim{color:var(--muted)}
.le-msg.warn{color:var(--warn)}
.le-msg.cross{color:var(--muted);opacity:.7}
</style>
</head>
<body>
<div class="shell">

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo">KALSHI<em>/</em>BOT</div>
  <div class="sdot" id="sdot"></div>
  <span id="slabel">STOPPED</span>
  <div class="spacer"></div>
  <div class="data-dot-wrap">
    <div class="data-dot" id="data-dot"></div>
    <span id="data-label">No data</span>
  </div>
  <div class="mode-toggle">
    <button class="mode-btn" id="mode-monitor" onclick="setMode(true)">Monitor</button>
    <button class="mode-btn" id="mode-trade"   onclick="setMode(false)">Trade</button>
  </div>
  <button class="btn"          id="btn-start" onclick="startBot()">&#9654; Start</button>
  <button class="btn btn-stop" id="btn-stop"  onclick="stopBot()" style="display:none">&#9632; Stop</button>
</div>

<!-- STATS BAR -->
<div class="stats-bar">
  <div class="stat-cell">
    <div class="stat-lbl">Session P&amp;L</div>
    <div class="stat-val neutral" id="s-pnl">$0.0000</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Trades</div>
    <div class="stat-val neutral" id="s-trades">0</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Win Rate</div>
    <div class="stat-val neutral" id="s-winrate">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="stat-lbl">Uptime</div>
    <div class="stat-val neutral" id="s-uptime">&#8212;</div>
  </div>
</div>

<!-- MARKETS ROW -->
<div class="markets-row" id="markets-row"></div>

<!-- MOMENTUM POSITIONS ROW -->
<div id="momentum-row">
  <div class="mom-row-inner">
    <span class="mom-label">Momentum</span>
    <div class="mom-cards" id="mom-cards"></div>
    <div class="mom-cfg">
      Entry&nbsp;<b style="color:var(--text)">85–94¢</b>
      &nbsp;·&nbsp;
      TP&nbsp;<b style="color:var(--ok)">≥95¢</b>
      &nbsp;·&nbsp;
      Window&nbsp;<b style="color:var(--text)">13:00–14:00</b>
    </div>
  </div>
</div>

<!-- MAIN AREA -->
<div class="main-area">
  <div class="content-cols">

    <!-- MOMENTUM STATUS -->
    <div class="panel">
      <div class="ph">
        <div class="pt">Momentum Status</div>
        <div class="spacer"></div>
        <span style="font-size:9px;color:var(--muted)">Entry&nbsp;<b style="color:var(--text)">≥85¢&nbsp;&lt;95¢</b>&nbsp;·&nbsp;TP&nbsp;<b style="color:var(--ok)">max(95¢,&nbsp;entry+1¢)</b></span>
      </div>
      <div class="pb" id="ms-body">
        <div class="no-data">Waiting for market data…</div>
      </div>
    </div>

    <!-- TRADE HISTORY -->
    <div class="panel">
      <div class="ph">
        <div class="pt">Trade History</div>
        <div class="spacer"></div>
        <div style="display:flex;gap:16px;font-size:9px;color:var(--muted)">
          <span>P&amp;L:&nbsp;<b id="h-pnl" style="color:var(--ok)">+$0.0000</b></span>
          <span>Fills:&nbsp;<b id="h-fills" style="color:var(--text)">0</b></span>
        </div>
      </div>
      <div class="pb" id="trades-body" style="padding:0">
        <div class="no-data">No trades yet</div>
      </div>
    </div>

  </div>

  <!-- LOG PANEL -->
  <div id="log-panel">
    <div id="log-drag" onmousedown="startDrag(event)"></div>
    <div class="log-ph">
      <div class="log-pt">Event Log</div>
      <div class="spacer"></div>
      <div class="data-dot-wrap" style="margin-right:8px">
        <div class="data-dot" id="conn-dot"></div>
        <span id="conn-label" style="font-size:9px;color:var(--muted)">Disconnected</span>
      </div>
      <button class="log-toggle" id="log-toggle-btn" onclick="toggleLog()">Expand</button>
    </div>
    <div id="log-body"></div>
  </div>
</div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  botRunning:false, dryRun:true, demo:false,
  enabledAssets:['BTC','ETH','SOL'],
  snapshots:{},
  momentumPositions:{BTC:null,ETH:null,SOL:null},
  sessionPnl:0, sessionTrades:0, sessionWins:0,
  startedAt:null, lastPriceTs:0, assetTimers:{},
  logExpanded:false, logHeight:220,
};

// ── SSE ───────────────────────────────────────────────────────────────────────
let es = null, _sseEnabled = true;
function connectSSE() {
  _sseEnabled = true;
  if (es) try { es.close(); } catch(e) {}
  es = new EventSource('/stream');
  es.onopen = () => {
    document.getElementById('conn-dot').classList.add('live');
    document.getElementById('conn-label').textContent = 'Connected';
  };
  es.onerror = () => {
    document.getElementById('conn-dot').classList.remove('live');
    document.getElementById('conn-label').textContent = 'Reconnecting…';
    if (_sseEnabled) setTimeout(connectSSE, 3000);
  };
  es.onmessage = e => handleMsg(JSON.parse(e.data));
}

function handleMsg(msg) {
  const t = msg.type;
  if (t === 'init') {
    S.dryRun        = msg.dry_run;
    S.demo          = msg.demo;
    S.enabledAssets = msg.enabled_assets || ['BTC','ETH','SOL'];
    S.sessionPnl    = msg.session_pnl    || 0;
    S.sessionTrades = msg.session_trades || 0;
    S.sessionWins   = msg.session_wins   || 0;
    S.startedAt     = msg.started_at     || null;
    if (msg.momentum_positions) S.momentumPositions = msg.momentum_positions;
    updateMode(); updateStatus(msg.status);
    S.snapshots = msg.snapshots || {};
    renderMarkets(); renderMomentumRow(); renderMomentumStatus(); updateStats();
    (msg.log || []).forEach(addLog);
  } else if (t === 'prices') {
    S.snapshots   = msg.snapshots || {};
    S.lastPriceTs = Date.now();
    ['BTC','ETH','SOL'].forEach(a => {
      const s = S.snapshots[a];
      if (s && s.secs_left != null)
        S.assetTimers[a] = { secsLeft: s.secs_left, capturedAt: performance.now() };
    });
    renderMarkets(); renderMomentumStatus();
  } else if (t === 'momentum_update') {
    S.momentumPositions[msg.asset] = msg.position;
    renderMomentumRow(); renderMomentumStatus();
  } else if (t === 'log') {
    addLog(msg);
  } else if (t === 'status') {
    updateStatus(msg.status);
  } else if (t === 'session_stats') {
    S.sessionPnl    = msg.session_pnl;
    S.sessionTrades = msg.session_trades;
    S.sessionWins   = msg.session_wins;
    updateStats();
  } else if (t === 'config') {
    if (msg.enabled_assets) { S.enabledAssets = msg.enabled_assets; renderMarkets(); }
    if (msg.dry_run !== undefined) { S.dryRun = !!msg.dry_run; updateMode(); }
  } else if (t === 'order_result') {
    refreshTrades();
  } else if (t === 'positions') {
    refreshTrades();
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
function updateStatus(st) {
  const dot = document.getElementById('sdot');
  dot.className = 'sdot';
  S.botRunning = ['monitoring','connected'].includes(st);
  if (S.botRunning) dot.classList.add('run');
  else if (['discovering','connecting','starting','reconnecting','waiting'].includes(st))
    dot.classList.add('disc');
  document.getElementById('slabel').textContent = st.toUpperCase();
  document.getElementById('btn-start').style.display = S.botRunning ? 'none' : '';
  document.getElementById('btn-stop').style.display  = S.botRunning ? ''     : 'none';
}

function updateMode() {
  document.getElementById('mode-monitor').className =
    'mode-btn' + ( S.dryRun ? ' active-monitor' : '');
  document.getElementById('mode-trade').className =
    'mode-btn' + (!S.dryRun ? ' active-trade'   : '');
}
async function setMode(monitorMode) {
  S.dryRun = monitorMode; updateMode();
  await fetch('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({dry_run: monitorMode})
  });
}

// ── Momentum positions strip ───────────────────────────────────────────────
function renderMomentumRow() {
  document.getElementById('mom-cards').innerHTML =
    ['BTC','ETH','SOL'].map(renderMomCard).join('');
}

function renderMomCard(asset) {
  const pos  = S.momentumPositions[asset];
  const snap = S.snapshots[asset];
  if (!pos) {
    const secsLeft = snap?.secs_left ?? null;
    const elapsed  = secsLeft != null ? 900 - secsLeft : null;
    let statusTxt  = 'waiting…';
    if (elapsed != null) {
      if (elapsed < 780) {
        const rem = 780 - elapsed;
        statusTxt = `entry in ${Math.floor(rem/60)}m${rem%60}s`;
      } else if (elapsed <= 840) {
        statusTxt = 'watching 14th min…';
      } else {
        statusTxt = 'window passed';
      }
    }
    return `<div class="mom-card"><span style="color:var(--accent)">${asset}</span>`
         + `<span style="color:var(--muted);margin-left:4px">${statusTxt}</span></div>`;
  }
  const currentBid = pos.side === 'yes' ? snap?.yes_bid : snap?.no_bid;
  const unreal     = (currentBid != null && pos.phase === 'holding')
    ? (currentBid - pos.entry_price) * pos.count / 100 : null;
  const pnlStr   = unreal != null
    ? `<span style="color:${unreal>=0?'var(--ok)':'var(--down)'};margin-left:3px">`
      + `${unreal>=0?'+':''}$${Math.abs(unreal).toFixed(4)}</span>` : '';
  const nowStr   = currentBid != null
    ? `<span style="color:var(--text);margin-left:3px">→${currentBid}¢</span>` : '';
  const hedgeStr = pos.hedge_price != null
    ? `<span style="color:var(--up);margin-left:3px">`
      + `+${pos.side==='yes'?'NO':'YES'}@${pos.hedge_price}¢</span>` : '';
  return `<div class="mom-card ${pos.phase}">`
       + `<span style="color:var(--accent)">${asset}</span>`
       + `<span style="color:var(--warn);margin-left:4px">`
       + `${pos.side.toUpperCase()}@${pos.entry_price}¢×${pos.count}</span>`
       + nowStr + pnlStr + hedgeStr
       + `<span style="font-size:8px;color:var(--muted);margin-left:4px">${pos.phase}</span>`
       + `</div>`;
}

// ── Momentum status panel ─────────────────────────────────────────────────────
function renderMomentumStatus() {
  const body = document.getElementById('ms-body');
  if (!body) return;
  const cards = ['BTC','ETH','SOL'].map(renderMsCard).join('');
  body.innerHTML = cards || '<div class="no-data">Waiting for market data…</div>';
}

function renderMsCard(asset) {
  const s   = S.snapshots[asset];
  const pos = S.momentumPositions[asset];
  const chk = S.enabledAssets.includes(asset) ? 'checked' : '';

  const t = S.assetTimers[asset];
  const rawSecs = t
    ? Math.max(0, t.secsLeft - (performance.now() - t.capturedAt)/1000)
    : (s?.secs_left ?? null);
  const elapsed = rawSecs != null ? 900 - rawSecs : null;

  let timerLabel = '—', timerCls = '';
  if (rawSecs != null) {
    if (rawSecs >= 3600) timerLabel = Math.floor(rawSecs/3600)+'h '+Math.floor(rawSecs%3600/60)+'m';
    else if (rawSecs >= 60) timerLabel = Math.floor(rawSecs/60)+'m '+Math.floor(rawSecs%60)+'s';
    else { timerLabel = Math.floor(rawSecs)+'s'; timerCls = rawSecs < 10 ? ' crit' : ' warn'; }
  }

  // Phase detection
  let phase = 'WAITING', phaseClass = 'dim', cardCls = '';
  if (pos) {
    phase     = pos.phase.toUpperCase();
    phaseClass = pos.phase === 'holding' ? 'warn' : pos.phase === 'hedged' ? 'up' : pos.phase === 'closed' ? 'ok' : 'dim';
    cardCls   = pos.phase;
  } else if (elapsed != null) {
    if (elapsed >= 780 && elapsed <= 840) { phase = 'WATCHING'; phaseClass = 'warn'; cardCls = 'watching'; }
    else if (elapsed > 840) { phase = 'PASSED'; phaseClass = 'dim'; }
  }

  const swHtml = `<label class="sw" onclick="event.stopPropagation()">
    <input type="checkbox" ${chk} onchange="toggleAsset('${asset}',this.checked)">
    <div class="sw-track"></div>
  </label>`;

  const yA = s?.yes_ask, nA = s?.no_ask, yB = s?.yes_bid, nB = s?.no_bid;
  const yHot = yA != null && yA >= 85 && yA < 95;
  const nHot = nA != null && nA >= 85 && nA < 95;

  let phaseNote = '';
  if (!pos && elapsed != null && elapsed < 780) {
    const rem = 780 - elapsed;
    phaseNote = `entry in ${Math.floor(rem/60)}m ${rem%60}s`;
  }

  let posHtml = '';
  if (pos) {
    const currentBid = pos.side === 'yes' ? yB : nB;
    const tp_target  = Math.max(85, pos.entry_price + 1);  // visual only; real is max(95, entry+1)
    const tpReal     = Math.max(95, pos.entry_price + 1);
    const unreal     = currentBid != null && pos.phase === 'holding'
      ? (currentBid - pos.entry_price) * pos.count / 100 : null;
    const distToTp   = currentBid != null ? tpReal - currentBid : null;
    posHtml = `<div class="ms-pos">
      <span class="ms-pos-side ${pos.side==='yes'?'up':'dn'}">${pos.side.toUpperCase()}@${pos.entry_price}¢×${pos.count}</span>
      ${currentBid!=null?`<span class="ms-pos-bid">bid&nbsp;${currentBid}¢</span>`:''}
      ${unreal!=null?`<span class="ms-pos-pnl ${unreal>=0?'pos':'neg'}">${unreal>=0?'+':''}$${Math.abs(unreal).toFixed(4)}</span>`:''}
      ${distToTp!=null&&pos.phase==='holding'?`<span class="ms-pos-tp ${distToTp<=0?'ok':''}">TP@${tpReal}¢${distToTp>0?' ('+distToTp+'¢ away)':' ✓'}</span>`:''}
      ${pos.hedge_price?`<span class="ms-pos-hedge">hedge&nbsp;${pos.side==='yes'?'NO':'YES'}@${pos.hedge_price}¢</span>`:''}
    </div>`;
  }

  return `<div class="ms-card ${cardCls}">
    <div class="ms-head">
      ${swHtml}
      <span class="ms-asset">${asset}</span>
      <span class="ms-ticker">${(s?.ticker||'waiting…').slice(-22)}</span>
      <span class="ms-timer${timerCls}">${timerLabel}</span>
      <span class="ms-phase ${phaseClass}">${phase}</span>
    </div>
    <div class="ms-prices">
      <div class="ms-pcol">
        <div class="ms-plbl">YES ask</div>
        <div class="ms-pval ${yHot?'hot':'up'}">${yA??'—'}<span style="font-size:10px;opacity:.45">¢</span></div>
        <div class="ms-psub">bid&nbsp;<b>${yB!=null?yB+'¢':'—'}</b></div>
      </div>
      <div class="ms-pcol">
        <div class="ms-plbl">NO ask</div>
        <div class="ms-pval ${nHot?'hot':'dn'}">${nA??'—'}<span style="font-size:10px;opacity:.45">¢</span></div>
        <div class="ms-psub">bid&nbsp;<b>${nB!=null?nB+'¢':'—'}</b></div>
      </div>
      <div class="ms-pcol">
        <div class="ms-plbl">${phaseNote?'Entry':'Threshold'}</div>
        <div class="ms-pval dim" style="font-size:12px">${phaseNote||'≥85¢ &lt;95¢'}</div>
        <div class="ms-psub">TP&nbsp;<b>≥95¢</b></div>
      </div>
    </div>
    ${posHtml}
  </div>`;
}

// ── Markets row ───────────────────────────────────────────────────────────────
function renderMarkets() {
  document.getElementById('markets-row').innerHTML =
    ['BTC','ETH','SOL'].map(renderMcard).join('');
}

function renderMcard(asset) {
  const s   = S.snapshots[asset];
  const chk = S.enabledAssets.includes(asset) ? 'checked' : '';
  const t   = S.assetTimers[asset];
  const rawSecs = t
    ? Math.max(0, t.secsLeft - (performance.now() - t.capturedAt)/1000)
    : (s?.secs_left ?? null);
  let timerLabel = s ? 'No mkt' : '—', timerCls = '';
  if (rawSecs != null) {
    if (rawSecs >= 3600) timerLabel = Math.floor(rawSecs/3600)+'h '+Math.floor(rawSecs%3600/60)+'m';
    else if (rawSecs >= 60) timerLabel = Math.floor(rawSecs/60)+'m '+Math.floor(rawSecs%60)+'s';
    else { timerLabel = Math.floor(rawSecs)+'s'; timerCls = rawSecs < 10 ? ' crit' : ' warn'; }
  }
  const swHtml = `<label class="sw" onclick="event.stopPropagation()">
    <input type="checkbox" ${chk} onchange="toggleAsset('${asset}',this.checked)">
    <div class="sw-track"></div>
  </label>`;
  if (!s) return `<div class="mcard"><div class="mcard-top">${swHtml}
    <span class="masset">${asset}</span><span class="mticker">Waiting…</span>
    <span class="mtimer${timerCls}" id="mtimer-${asset}">${timerLabel}</span></div>
    <div style="font-size:10px;color:var(--muted)">No market data</div></div>`;
  const yA = s.yes_ask, nA = s.no_ask, yB = s.yes_bid, nB = s.no_bid;
  const elapsed = rawSecs != null ? 900 - rawSecs : null;
  const inWindow = elapsed != null && elapsed >= 780 && elapsed <= 840;
  const yHot = inWindow && yA != null && yA >= 85 && yA < 95;
  const nHot = inWindow && nA != null && nA >= 85 && nA < 95;
  return `<div class="mcard" id="mcard-${asset}">
    <div class="mcard-top">${swHtml}
      <span class="masset">${asset}</span>
      <span class="mticker">${(s.ticker||'').slice(-22)}</span>
      <span class="mtimer${timerCls}" id="mtimer-${asset}">${timerLabel}</span>
    </div>
    <div class="mcard-prices">
      <div class="mprice-col">
        <div class="mprice-lbl">YES ask</div>
        <div class="mprice-ask ${yHot?'hot':'up'}">${yA??'—'}<span style="font-size:11px;opacity:.45">¢</span></div>
        <div class="mprice-sub">bid <b>${yB!=null?yB+'¢':'—'}</b></div>
      </div>
      <div class="mprice-col">
        <div class="mprice-lbl">NO ask</div>
        <div class="mprice-ask ${nHot?'hot':'dn'}">${nA??'—'}<span style="font-size:11px;opacity:.45">¢</span></div>
        <div class="mprice-sub">bid <b>${nB!=null?nB+'¢':'—'}</b></div>
      </div>
      <div class="mprice-col">
        <div class="mprice-lbl">Mid</div>
        <div class="mprice-ask up">${s.mid!=null?s.mid.toFixed(1):'—'}<span style="font-size:11px;opacity:.45">¢</span></div>
        <div class="mprice-sub">window <b>${elapsed!=null?Math.floor(elapsed/60)+'m '+Math.floor(elapsed%60)+'s':'—'}</b></div>
      </div>
    </div>
  </div>`;
}

async function toggleAsset(asset, enabled) {
  if (enabled && !S.enabledAssets.includes(asset)) S.enabledAssets.push(asset);
  else if (!enabled) S.enabledAssets = S.enabledAssets.filter(a => a !== asset);
  await fetch('/api/assets', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({asset, enabled})
  });
}

// ── Stats bar ─────────────────────────────────────────────────────────────────
function updateStats() {
  const pnl = S.sessionPnl || 0;
  const el  = document.getElementById('s-pnl');
  el.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(4);
  el.className   = 'stat-val ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neutral');
  document.getElementById('s-trades').textContent = S.sessionTrades || 0;
  document.getElementById('s-winrate').textContent = S.sessionTrades > 0
    ? Math.round(S.sessionWins / S.sessionTrades * 100) + '%' : '—';
}

setInterval(() => {
  if (!S.startedAt) { document.getElementById('s-uptime').textContent = '—'; return; }
  const secs = Math.floor((Date.now() - new Date(S.startedAt).getTime()) / 1000);
  const h = Math.floor(secs/3600), m = Math.floor(secs%3600/60), s2 = secs%60;
  document.getElementById('s-uptime').textContent =
    h > 0 ? h+'h '+m+'m' : m > 0 ? m+'m '+s2+'s' : s2+'s';
}, 1000);

setInterval(() => {
  const fresh = S.lastPriceTs && (Date.now() - S.lastPriceTs) < 6000;
  document.getElementById('data-dot').className  = 'data-dot' + (fresh ? ' live' : '');
  document.getElementById('data-label').textContent = fresh ? 'Live' : 'No data';
}, 1000);

setInterval(() => {
  ['BTC','ETH','SOL'].forEach(a => {
    const t  = S.assetTimers[a];
    if (!t) return;
    const secs = Math.max(0, t.secsLeft - (performance.now() - t.capturedAt) / 1000);
    const el   = document.getElementById('mtimer-' + a);
    if (!el) return;
    let label = '', cls = '';
    if      (secs >= 3600) { label = Math.floor(secs/3600)+'h '+Math.floor(secs%3600/60)+'m'; }
    else if (secs >=   60) { label = Math.floor(secs/60)+'m '+Math.floor(secs%60)+'s'; }
    else if (secs >    0)  { label = Math.floor(secs)+'s'; cls = secs < 10 ? ' crit' : ' warn'; }
    else                   { label = 'Expired'; cls = ' crit'; }
    el.textContent = label;
    el.className   = 'mtimer' + cls;
  });
}, 100);

// ── Trade history ─────────────────────────────────────────────────────────────
async function refreshTrades() {
  const d   = await fetch('/api/trades').then(r => r.json()).catch(() => ({trades:[]}));
  const pos = await fetch('/api/positions').then(r => r.json()).catch(() => ({}));
  renderTrades(d.trades || []);
  const pnl = pos.realised_pnl || 0;
  document.getElementById('h-pnl').textContent =
    (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(4);
  document.getElementById('h-fills').textContent = pos.total_fills || 0;
}

function renderTrades(trades) {
  const body = document.getElementById('trades-body');
  if (!trades.length) { body.innerHTML = '<div class="no-data">No trades yet</div>'; return; }
  body.innerHTML = trades.slice(0, 60).map(t => {
    const isEntry  = t.type === 'momentum_entry';
    const isTP     = t.type === 'momentum_tp';
    const isHedge  = t.type === 'momentum_hedge';
    const isDry    = t.dry_run;
    const bc       = isDry ? 'dry' : (isTP ? 'live' : (isHedge ? 'live' : 'live'));
    const lbl      = isDry ? 'DRY' : (isEntry ? 'ENTRY' : isTP ? 'TP' : isHedge ? 'HEDGE' : 'TRADE');
    let det = '';
    if (isEntry) det = `${t.asset} ${(t.side||'').toUpperCase()} @ ${t.price}¢ × ${t.count}`;
    else if (isTP) det = `${t.asset} SOLD ${(t.side||'').toUpperCase()} @ ${t.sell_price}¢ (entry ${t.entry_price}¢)`;
    else if (isHedge) det = `${t.asset} HEDGE ${(t.hedge_side||'').toUpperCase()} @ ${t.hedge_price}¢`;
    else det = `${t.asset||''} ${JSON.stringify(t).slice(0,60)}`;
    const pnlStr = t.pnl != null
      ? `<span class="trade-pnl">${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(4)}</span>` : '';
    return `<div class="trade-row">
      <span class="tbadge ${bc}">${lbl}</span>
      <span class="trade-detail">${esc(det)}</span>
      ${pnlStr}
      <span class="trade-ts">${(t.ts||'').slice(11,19)}</span>
    </div>`;
  }).join('');
}

// ── Log panel ─────────────────────────────────────────────────────────────────
function addLog(e) {
  const cls = e.icon==='✗'?'err':e.icon==='✅'?'ok':e.icon==='→'?'dim':e.icon==='!'?'warn':e.icon==='~'?'cross':'';
  const body = document.getElementById('log-body');
  const d = document.createElement('div');
  d.className = 'log-entry';
  d.innerHTML = `<span class="le-ts">${e.ts||''}</span>`
    + `<span class="le-icon">${e.icon||'·'}</span>`
    + `<span class="le-msg ${cls}">${esc(e.msg||'')}</span>`;
  body.insertBefore(d, body.firstChild);
  while (body.children.length > 150) body.removeChild(body.lastChild);
}

function toggleLog() {
  const panel = document.getElementById('log-panel');
  S.logExpanded = !S.logExpanded;
  if (S.logExpanded) {
    panel.classList.add('expanded');
    panel.style.setProperty('--log-h', S.logHeight + 'px');
    document.getElementById('log-toggle-btn').textContent = 'Collapse';
  } else {
    panel.classList.remove('expanded');
    document.getElementById('log-toggle-btn').textContent = 'Expand';
  }
}

let _dragging = false, _dragY0 = 0, _h0 = 0;
function startDrag(e) {
  if (!S.logExpanded) { toggleLog(); return; }
  _dragging = true; _dragY0 = e.clientY; _h0 = S.logHeight;
  document.addEventListener('mousemove', onDrag);
  document.addEventListener('mouseup',   stopDrag);
  e.preventDefault();
}
function onDrag(e) {
  if (!_dragging) return;
  S.logHeight = Math.max(80, Math.min(500, _h0 + (_dragY0 - e.clientY)));
  document.getElementById('log-panel').style.setProperty('--log-h', S.logHeight + 'px');
}
function stopDrag() {
  _dragging = false;
  document.removeEventListener('mousemove', onDrag);
  document.removeEventListener('mouseup',   stopDrag);
}

// ── Bot controls ──────────────────────────────────────────────────────────────
async function startBot() {
  S.startedAt = new Date().toISOString();
  document.getElementById('btn-start').style.display = 'none';
  document.getElementById('btn-stop').style.display  = '';
  document.getElementById('slabel').textContent = 'STARTING';
  document.getElementById('sdot').className = 'sdot disc';
  await fetch('/api/start', {method:'POST'});
  connectSSE();
}
async function stopBot() {
  S.startedAt   = null;
  S.botRunning  = false;
  S.lastPriceTs = 0;
  document.getElementById('btn-stop').style.display  = 'none';
  document.getElementById('btn-start').style.display = '';
  document.getElementById('slabel').textContent = 'STOPPED';
  document.getElementById('sdot').className = 'sdot';
  _sseEnabled = false;
  if (es) { try { es.close(); } catch(e) {} es = null; }
  document.getElementById('conn-dot').classList.remove('live');
  document.getElementById('conn-label').textContent = 'Disconnected';
  await fetch('/api/stop', {method:'POST'});
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
updateMode();
renderMarkets();
renderMomentumRow();
renderMomentumStatus();
connectSSE();
setInterval(refreshTrades, 5000);
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
        print("  KALSHI MOMENTUM BOT — Starting in DRY RUN / DEMO mode")
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

    _start_bot()

    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True, use_reloader=False)
