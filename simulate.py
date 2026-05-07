#!/usr/bin/env python3
"""
simulate.py — Kalshi Momentum Strategy Scenario Simulator

Runs every meaningful price path through the current protection stack and
reports exact P&L so you can verify the strategy is working as intended.

Usage:
    python simulate.py
    python simulate.py --sl 70 --tp 97      # test with recommended config
"""

import argparse
from dataclasses import dataclass, field
from typing import Optional

# ── Config (defaults match current .env) ──────────────────────────────────────
DEFAULTS = dict(
    trade_size          = 10,   # TRADE_SIZE_CONTRACTS
    entry_price         = 87,   # typical entry (cents)
    take_profit         = 95,   # MOMENTUM_TAKE_PROFIT
    stop_loss           = 60,   # MOMENTUM_STOP_LOSS
    warn_threshold      = 75,   # MOMENTUM_WARN_THRESHOLD
    warn_count          = 5,    # MOMENTUM_WARN_COUNT
    insurance_trigger   = 5,    # MOMENTUM_INSURANCE_THRESHOLD (opp ask ≤ this)
    insurance_count     = 3,    # MOMENTUM_INSURANCE_COUNT
    entry_hedge_count   = 3,    # MOMENTUM_ENTRY_HEDGE_COUNT
)

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Layer:
    name:   str
    side:   str          # "yes" | "no"
    price:  int          # cents paid
    count:  int

@dataclass
class ScenarioResult:
    name:        str
    description: str
    layers:      list[Layer] = field(default_factory=list)
    tp_sell:     Optional[tuple[int,int]] = None   # (price, count)
    sl_sell:     Optional[tuple[int,int]] = None   # (price, count)
    entry_side:  str = "no"
    entry_price: int = 87
    entry_count: int = 10

def simulate(cfg: dict) -> list[ScenarioResult]:
    n        = cfg["trade_size"]
    ep       = cfg["entry_price"]
    tp       = cfg["take_profit"]
    sl       = cfg["stop_loss"]
    warn_thr = cfg["warn_threshold"]
    warn_n   = cfg["warn_count"]
    ins_n    = cfg["insurance_count"]
    eh_n     = cfg["entry_hedge_count"]

    # Entry side = NO (typical), opp = YES
    # Entry hedge price = 100 - entry_bid ≈ 100 - ep (bid ~ ask at entry)
    eh_price   = 100 - ep          # e.g. 87 → YES at 13c
    ins_price  = 5                 # insurance buys YES at ~5c (opp ask ≤ 5c)
    warn_price = 100 - warn_thr    # e.g. 75 → YES at 25c

    results = []

    # ── S1: Clean win — no dips, straight to TP ───────────────────────────────
    r = ScenarioResult("S1", "Clean win — price rises straight to TP")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.tp_sell = (tp, n)
    results.append(r)

    # ── S2: Win with insurance — runs to 95c+, then TP ───────────────────────
    r = ScenarioResult("S2", "Win with insurance — NO bid hits 95c+, then TP")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.layers.append(Layer("Insurance",   "yes", ins_price, ins_n))
    r.tp_sell = (tp, n)
    results.append(r)

    # ── S3: Fast reversal — SL within 10s, no other layers fire ─────────────
    r = ScenarioResult("S3", "Fast reversal — SL hits within 10s, only entry hedge active")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.sl_sell = (sl, n)
    results.append(r)

    # ── S4: Slow reversal — SL, no warn (not in last 30s) ────────────────────
    r = ScenarioResult("S4", "Slow reversal — SL fires, not in last 30s so no warn hedge")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.sl_sell = (sl, n)
    results.append(r)

    # ── S5: Reversal in last 30s — warn fires then SL ─────────────────────────
    r = ScenarioResult("S5", "Reversal in last 30s — warn hedge fires at 75c, then SL at 60c")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.layers.append(Layer("Warn hedge",  "yes", warn_price, warn_n))
    r.sl_sell = (sl, n)
    results.append(r)

    # ── S6: Insurance + warn + SL (runs up then full crash) ──────────────────
    r = ScenarioResult("S6", "Full crash — insurance + warn + SL all fire")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.layers.append(Layer("Insurance",   "yes", ins_price, ins_n))
    r.layers.append(Layer("Warn hedge",  "yes", warn_price, warn_n))
    r.sl_sell = (sl, n)
    results.append(r)

    # ── S7: Warn fires in last 30s but price recovers to TP ──────────────────
    r = ScenarioResult("S7", "Warn fires in last 30s but price recovers — TP fills")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    r.layers.append(Layer("Warn hedge",  "yes", warn_price, warn_n))
    r.tp_sell = (tp, n)
    results.append(r)

    # ── S8: Held to resolution (no TP, no SL — time expires) ─────────────────
    r = ScenarioResult("S8", "Held to resolution — no TP or SL fires (price between 60-95c at expiry)")
    r.entry_side  = "no"
    r.entry_price = ep
    r.entry_count = n
    r.layers.append(Layer("Entry hedge", "yes", eh_price, eh_n))
    results.append(r)

    return results


def pnl(result: ScenarioResult, resolution: str, cfg: dict) -> float:
    """
    Compute net P&L for a scenario given the resolution ("yes" wins or "no" wins).
    resolution = "no"  → entry-side (NO) wins → YES contracts = $0
    resolution = "yes" → entry-side (NO) loses → YES contracts = $1 each
    """
    n  = result.entry_count
    ep = result.entry_price

    total_spent   = ep * n / 100           # main entry cost
    total_received = 0.0

    # TP sell
    if result.tp_sell:
        price, count = result.tp_sell
        total_received += price * count / 100

    # SL sell
    if result.sl_sell:
        price, count = result.sl_sell
        total_received += price * count / 100

    # Hedge layers (all buy YES for a NO entry)
    total_hedge_cost = 0.0
    total_hedge_payout = 0.0
    for layer in result.layers:
        cost = layer.price * layer.count / 100
        total_hedge_cost += cost
        if layer.side == resolution:   # hedge side won
            total_hedge_payout += layer.count * 1.0  # $1 per contract

    # Main position held to resolution (contracts not sold via TP/SL)
    sold = 0
    if result.tp_sell:
        sold += result.tp_sell[1]
    if result.sl_sell:
        sold += result.sl_sell[1]
    held = max(0, n - sold)
    if result.entry_side == resolution:   # entry side won
        total_received += held * 1.0

    net = total_received + total_hedge_payout - total_spent - total_hedge_cost
    return net


def print_table(results: list[ScenarioResult], cfg: dict, label: str):
    W = 90
    print(f"\n{'='*W}")
    print(f"  {label}")
    print(f"  Entry: {cfg['entry_price']}c x {cfg['trade_size']}  |  "
          f"TP: {cfg['take_profit']}c  SL: {cfg['stop_loss']}c  |  "
          f"EH: {cfg['entry_hedge_count']}x{100 - cfg['entry_price']}c  "
          f"Ins: {cfg['insurance_count']}x5c  "
          f"Warn: {cfg['warn_count']}x{100 - cfg['warn_threshold']}c")
    print(f"{'='*W}")

    header = f"  {'Scenario':<50} {'Entry wins':>10} {'Entry loses':>12} {'Verdict'}"
    print(header)
    print(f"  {'-'*86}")

    for r in results:
        win_pnl  = pnl(r, r.entry_side,                                    cfg)
        lose_pnl = pnl(r, "yes" if r.entry_side == "no" else "no",         cfg)

        layers_str = ", ".join(l.name for l in r.layers) or "—"

        verdict = ""
        if win_pnl > 0 and lose_pnl > 0:
            verdict = "[OK] profit both ways"
        elif win_pnl > 0 and lose_pnl >= -0.5:
            verdict = "[OK] win / small loss"
        elif win_pnl > 0:
            verdict = "[!!] win / loss"
        elif win_pnl <= 0:
            verdict = "[XX] loss both ways" if lose_pnl <= 0 else "[!!] loss / win"

        print(f"  {r.name}  {r.description:<47} {win_pnl:>+8.2f}    {lose_pnl:>+8.2f}    {verdict}")
        print(f"       Layers: {layers_str}")

        if r.tp_sell:
            print(f"       TP: sell {r.tp_sell[1]} NO @ {r.tp_sell[0]}¢")
        if r.sl_sell:
            print(f"       SL: sell {r.sl_sell[1]} NO @ {r.sl_sell[0]}¢")
        print()

    # Summary
    all_win_pnls  = [pnl(r, r.entry_side, cfg) for r in results]
    all_lose_pnls = [pnl(r, "yes" if r.entry_side=="no" else "no", cfg) for r in results]
    print(f"  {'-'*86}")
    print(f"  {'Best case (entry wins):':<52} {max(all_win_pnls):>+8.2f}")
    print(f"  {'Worst case (entry wins):':<52} {min(all_win_pnls):>+8.2f}")
    print(f"  {'Best case (entry loses):':<52} {max(all_lose_pnls):>+8.2f}")
    print(f"  {'Worst case (entry loses):':<52} {min(all_lose_pnls):>+8.2f}")
    breakeven = sum(1 for p in all_win_pnls if p > 0) / len(all_win_pnls) * 100
    print(f"  {'Win scenarios that are profitable:':<52} {breakeven:.0f}%")
    print(f"{'='*W}\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi strategy simulator")
    parser.add_argument("--sl",    type=int, help="Stop loss threshold (default 60)")
    parser.add_argument("--tp",    type=int, help="Take profit threshold (default 95)")
    parser.add_argument("--entry", type=int, help="Entry price (default 87)")
    parser.add_argument("--size",  type=int, help="Trade size contracts (default 10)")
    args = parser.parse_args()

    current_cfg = dict(DEFAULTS)
    if args.sl:    current_cfg["stop_loss"]    = args.sl
    if args.tp:    current_cfg["take_profit"]  = args.tp
    if args.entry: current_cfg["entry_price"]  = args.entry
    if args.size:  current_cfg["trade_size"]   = args.size

    results = simulate(current_cfg)
    print_table(results, current_cfg, "CURRENT CONFIG")

    # Always show recommended side-by-side
    if not (args.sl or args.tp):
        rec_cfg = dict(current_cfg, stop_loss=70, take_profit=97)
        rec_results = simulate(rec_cfg)
        print_table(rec_results, rec_cfg, "RECOMMENDED CONFIG  (SL=70, TP=97)")

    # Risk/reward summary
    cfg = current_cfg
    ep, tp_val, sl_val = cfg["entry_price"], cfg["take_profit"], cfg["stop_loss"]
    win_per_contract  = tp_val - ep
    loss_per_contract = ep - sl_val
    ratio = loss_per_contract / win_per_contract if win_per_contract else float("inf")
    breakeven_wr = loss_per_contract / (win_per_contract + loss_per_contract) * 100
    print(f"  Risk/Reward (current):  +{win_per_contract}c win  vs  -{loss_per_contract}c loss  "
          f"=> need >{breakeven_wr:.0f}% win rate to break even on main position\n")

    if not (args.sl or args.tp):
        ep2, tp2, sl2 = ep, 97, 70
        w2 = tp2 - ep2
        l2 = ep2 - sl2
        wr2 = l2 / (w2 + l2) * 100
        print(f"  Risk/Reward (recommended):  +{w2}c win  vs  -{l2}c loss  "
              f"=> need >{wr2:.0f}% win rate to break even on main position\n")


if __name__ == "__main__":
    main()
