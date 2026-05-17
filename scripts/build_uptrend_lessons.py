"""Build 6 teaching lessons for learning_data.json.

For each of the 6 strategy IDs, searches ALL regimes in AU chart_data.json,
scores positions by teaching clarity (tp2 > ema_exit > tp1), picks the best.
Overwrites learning_data.json with exactly 6 lessons.
"""
import json
import re
from datetime import date, timedelta

CHART_DATA = "docs/chart_data.json"
OHLCV_AU   = "docs/ohlcv_AU.json"
LEARN_DATA  = "docs/learning_data.json"

STRATEGY_IDS = [
    "pivot_breakout",
    "pullback_buy",
    "ma_cross",
    "reversal",
    "bb_squeeze",
]

# ── teaching score — tp2 hit is the goal ────────────────────────────────────
def score_teaching(pos):
    exits       = pos.get("exits", [])
    last_exit   = exits[-1] if exits else {}
    final       = str(last_exit.get("exit_reason") or "").lower()
    has_tp2_hit = any(str(e.get("exit_reason") or "").lower() == "tp2" for e in exits)

    if not pos.get("pnl", 0) > 0:
        return -9999  # must be profitable

    sc = 0
    # Primary: exit quality
    if has_tp2_hit:
        sc += 300
    elif re.search(r"ema|trail", final):
        sc += 150
    elif final == "tp1":
        sc += 50

    # Risk structure visible on chart
    sc += 20 if pos.get("stop_price") else 0
    sc += 20 if pos.get("tp1_price")  else 0
    sc += 20 if pos.get("tp2_price")  else 0

    # Hold time: 12-30 bars is cleanest to display
    bars = pos.get("bars_held") or 0
    if 12 <= bars <= 30:
        sc += 30
    else:
        sc -= abs(bars - 20) * 1.2

    # Price readability (avoid sub-dollar noise or very high prices)
    ep = pos.get("entry_price") or 0
    if 0 < ep < 0.5:
        sc -= 40
    if ep > 300:
        sc -= 20

    return sc

# ── extract positions from strategy symbols ──────────────────────────────────
def extract_positions(symbols):
    pos_map = {}
    for sym_row in symbols:
        sym = sym_row.get("symbol", "")
        for t in sym_row.get("trades", []):
            pid = t.get("position_id") or f"{t.get('entry_date','')}_{t.get('entry_price','')}_{sym}"
            if pid not in pos_map:
                pos_map[pid] = {
                    "symbol":      sym,
                    "entry_date":  t.get("entry_date", ""),
                    "exit_date":   t.get("exit_date", ""),
                    "entry_price": t.get("entry_price"),
                    "stop_price":  t.get("sl_price"),
                    "tp1_price":   t.get("tp1_price"),
                    "tp2_price":   t.get("tp2_price"),
                    "pnl":         0.0,
                    "bars_held":   0,
                    "exits":       [],
                }
            row = pos_map[pid]
            row["pnl"] += float(t.get("pnl") or 0)
            if t.get("exit_date", "") > row["exit_date"]:
                row["exit_date"] = t.get("exit_date", "")
            if row["stop_price"] is None and t.get("sl_price") is not None:
                row["stop_price"] = t.get("sl_price")
            if row["tp1_price"] is None and t.get("tp1_price") is not None:
                row["tp1_price"] = t.get("tp1_price")
            if row["tp2_price"] is None and t.get("tp2_price") is not None:
                row["tp2_price"] = t.get("tp2_price")
            row["bars_held"] = max(row["bars_held"], int(t.get("bars_held") or 0))
            row["exits"].append({
                "exit_date":   t.get("exit_date", ""),
                "exit_price":  t.get("exit_price"),
                "exit_reason": t.get("exit_reason", ""),
            })
    for row in pos_map.values():
        row["exits"].sort(key=lambda e: e["exit_date"])
    return list(pos_map.values())

# ── build anonymized date sequence ──────────────────────────────────────────
def business_dates(count, start="2024-01-02"):
    d = date.fromisoformat(start)
    dates = []
    while len(dates) < count:
        if d.weekday() < 5:
            dates.append(str(d))
        d += timedelta(days=1)
    return dates

def find_bar_idx(ohlcv, target_date):
    for i, b in enumerate(ohlcv):
        if b["time"] >= target_date:
            return i
    return len(ohlcv) - 1

def median(values):
    nums = sorted(v for v in values if v and v > 0)
    if not nums:
        return 1
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2

def rnd(v):
    return round(v, 2) if v is not None else None

# ── main ─────────────────────────────────────────────────────────────────────
with open(CHART_DATA, encoding="utf-8") as f:
    chart = json.load(f)
with open(OHLCV_AU, encoding="utf-8") as f:
    raw = json.load(f)
ohlcv_map = raw.get("ohlcv", raw) if isinstance(raw, dict) else {}

au_strats = chart.get("markets", {}).get("AU", chart).get("strategies", [])

lessons = []

for strat_id in STRATEGY_IDS:
    # Collect all positions across ALL regimes for this strategy
    all_positions = []
    regime_map = {}  # pid → regime
    for strat in au_strats:
        if strat.get("id") != strat_id:
            continue
        regime = strat.get("regime", "")
        positions = extract_positions(strat.get("symbols", []))
        for p in positions:
            pid = p["symbol"] + "_" + p["entry_date"]
            regime_map[pid] = regime
            p["_regime"] = regime
        all_positions.extend(positions)

    # Filter: needs ohlcv, positive pnl, valid prices
    candidates = [
        p for p in all_positions
        if ohlcv_map.get(p["symbol"])
        and (p.get("entry_price") or 0) > 0.5
        and p.get("stop_price")
        and p.get("tp1_price")
    ]

    if not candidates:
        print(f"SKIP {strat_id}: no valid candidates")
        continue

    best = max(candidates, key=score_teaching)
    sc   = score_teaching(best)
    exits_str = " > ".join(e.get("exit_reason", "?") for e in best["exits"])
    print(f"{strat_id:25} regime={best['_regime']:10} sym={best['symbol']:12} "
          f"score={sc:.0f} bars={best['bars_held']} exits=[{exits_str}]")

    sym   = best["symbol"]
    ohlcv = ohlcv_map[sym]

    entry_idx_full = find_bar_idx(ohlcv, best["entry_date"])
    exit_idx_full  = find_bar_idx(ohlcv, best["exit_date"])

    view_start    = max(0, entry_idx_full - 40)
    view_end      = min(len(ohlcv) - 1, max(exit_idx_full + 14, entry_idx_full + 18))
    history_start = max(0, view_start - 220)
    raw_bars      = ohlcv[history_start:view_end + 1]

    scale    = 100.0 / best["entry_price"]
    vol_base = median([b.get("volume") or 0 for b in raw_bars]) or 1
    anon_dates = business_dates(len(raw_bars))

    bars = [
        {
            "time":   anon_dates[i],
            "open":   rnd(b["open"]  * scale),
            "high":   rnd(b["high"]  * scale),
            "low":    rnd(b["low"]   * scale),
            "close":  rnd(b["close"] * scale),
            "volume": max(0, round(((b.get("volume") or 0) / vol_base) * 100_000)),
        }
        for i, b in enumerate(raw_bars)
    ]

    entry_idx = entry_idx_full - history_start
    exit_idx  = exit_idx_full  - history_start
    base_idx  = view_start - history_start

    exit_events = []
    for ex in best["exits"]:
        idx = find_bar_idx(ohlcv, ex["exit_date"]) - history_start
        if 0 <= idx < len(bars):
            exit_events.append({
                "idx":    idx,
                "price":  rnd((ex.get("exit_price") or best["entry_price"]) * scale),
                "reason": ex.get("exit_reason") or "exit",
            })

    last_exit = best["exits"][-1] if best["exits"] else {}
    position  = {
        "entry_price": rnd(best["entry_price"] * scale),
        "stop_price":  rnd(best["stop_price"]  * scale),
        "tp1_price":   rnd(best["tp1_price"]   * scale),
        "tp2_price":   rnd(best["tp2_price"]   * scale) if best.get("tp2_price") else None,
        "exit_reason": last_exit.get("exit_reason") or "exit",
        "exit_events": exit_events,
    }

    show_vol = strat_id in ("bb_squeeze", "pivot_breakout")
    show_rsi = strat_id == "reversal"

    # Get params from the matching strategy entry
    matched_strat = next((s for s in au_strats if s.get("id") == strat_id and s.get("regime") == best["_regime"]), {})

    lessons.append({
        "id":              strat_id,
        "regime":          best["_regime"],
        "source_symbol":   sym,
        "params":          matched_strat.get("params") or {},
        "indicator_params": matched_strat.get("indicator_params") or {},
        "showVolumePanel": show_vol,
        "showRsiPanel":    show_rsi,
        "bars":            bars,
        "baseIdx":         base_idx,
        "entryIdx":        entry_idx,
        "exitIdx":         exit_idx,
        "position":        position,
    })

with open(LEARN_DATA, encoding="utf-8") as f:
    learn = json.load(f)

learn["lessons"] = lessons

with open(LEARN_DATA, "w", encoding="utf-8") as f:
    json.dump(learn, f, default=str, separators=(",", ":"))

print(f"\nWrote {len(lessons)} lessons to {LEARN_DATA}")
for l in lessons:
    p = l["position"]
    print(f"  {l['id']:25} {l['regime']:10} exit={p['exit_reason']:15} tp2={'yes' if p['tp2_price'] else 'no'} events={len(p['exit_events'])}")
