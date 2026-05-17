"""Build uptrend lesson entries for learning_data.json.

Picks bb_squeeze|uptrend and ma_cross|uptrend from AU chart_data.json,
finds the most representative trade, scales prices to ~100, anonymizes dates,
then appends to docs/learning_data.json.
"""
import json
import re
from datetime import date, timedelta

CHART_DATA = "docs/chart_data.json"
OHLCV_AU   = "docs/ohlcv_AU.json"
LEARN_DATA  = "docs/learning_data.json"

TARGETS = [
    {"id": "bb_squeeze",  "regime": "uptrend"},
    {"id": "ma_cross",    "regime": "uptrend"},
]

# ── scoring (mirrors learningRealRepresentativeScore in JS) ──────────────────
def score_position(pos):
    bars_held   = pos.get("bars_held") or 0
    exits       = pos.get("exits", [])
    last_exit   = exits[-1] if exits else {}
    final_reason = str(last_exit.get("exit_reason") or "").lower()
    has_runner  = any(re.search(r"ema|trail|tp2", str(e.get("exit_reason") or "").lower()) for e in exits)
    sc = 100.0 if pos.get("pnl", 0) > 0 else -40.0
    sc += 10 if pos.get("stop_price") is not None else 0
    sc += 10 if pos.get("tp1_price") is not None else 0
    sc += 50 if has_runner else 0
    if re.search(r"ema|trail", final_reason):
        sc += 70
    elif final_reason == "tp2":
        sc += 35
    elif final_reason == "tp1":
        sc -= 30
    sc -= abs(bars_held - 22) * 0.9
    ep = pos.get("entry_price") or 0
    sc -= 20 if 0 < ep < 0.1 else 0
    sc -= 10 if ep > 300 else 0
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
                    "position_id": pid,
                    "symbol": sym,
                    "entry_date": t.get("entry_date", ""),
                    "exit_date": t.get("exit_date", ""),
                    "entry_price": t.get("entry_price"),
                    "stop_price": t.get("sl_price"),
                    "tp1_price": t.get("tp1_price"),
                    "tp2_price": t.get("tp2_price"),
                    "pnl": 0.0,
                    "bars_held": 0,
                    "exits": [],
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
                "exit_date": t.get("exit_date", ""),
                "exit_price": t.get("exit_price"),
                "exit_reason": t.get("exit_reason", ""),
            })
    for row in pos_map.values():
        row["exits"].sort(key=lambda e: e["exit_date"])
    return sorted(pos_map.values(), key=lambda p: score_position(p), reverse=True)

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
    return nums[mid] if len(nums) % 2 else (nums[mid-1] + nums[mid]) / 2

def rnd(v):
    return round(v, 2) if v is not None else None

# ── main ─────────────────────────────────────────────────────────────────────
with open(CHART_DATA, encoding="utf-8") as f:
    chart = json.load(f)

with open(OHLCV_AU, encoding="utf-8") as f:
    ohlcv_raw = json.load(f)
# ohlcv_AU.json is {symbol: [{time,open,high,low,close,volume}, ...]}
ohlcv_map = ohlcv_raw.get("ohlcv", ohlcv_raw) if isinstance(ohlcv_raw, dict) else {}

with open(LEARN_DATA, encoding="utf-8") as f:
    learn = json.load(f)

au_strats = {
    (s.get("id"), s.get("regime")): s
    for s in chart.get("markets", {}).get("AU", chart).get("strategies", [])
}

# track liquidity info from existing lessons
existing_liq = {(l["id"], l["regime"]): l.get("source_liquidity") for l in learn.get("lessons", [])}

new_lessons = []

for spec in TARGETS:
    key = (spec["id"], spec["regime"])
    strat = au_strats.get(key)
    if not strat:
        print(f"SKIP {key}: not in chart_data")
        continue

    positions = extract_positions(strat.get("symbols", []))
    # Filter: entry_price > 0, has stop and tp1
    candidates = [p for p in positions if (p.get("entry_price") or 0) > 0.1 and p.get("stop_price") and p.get("tp1_price")]
    if not candidates:
        print(f"SKIP {key}: no valid positions")
        continue

    # Find best position that has OHLCV data
    pos = None
    for p in candidates:
        if ohlcv_map.get(p["symbol"]):
            pos = p
            break
    if not pos:
        print(f"SKIP {key}: no OHLCV for any candidate symbol")
        continue

    sym = pos["symbol"]
    ohlcv = ohlcv_map[sym]
    print(f"{key}: using {sym}, score={score_position(pos):.1f}, pnl={pos['pnl']:.2f}, bars={pos['bars_held']}, exit={pos['exits'][-1]['exit_reason'] if pos['exits'] else 'none'}")

    entry_idx_full = find_bar_idx(ohlcv, pos["entry_date"])
    exit_idx_full  = find_bar_idx(ohlcv, pos["exit_date"])

    view_start   = max(0, entry_idx_full - 40)
    view_end     = min(len(ohlcv) - 1, max(exit_idx_full + 14, entry_idx_full + 18))
    history_start = max(0, view_start - 220)
    raw_bars     = ohlcv[history_start:view_end + 1]

    scale    = 100.0 / pos["entry_price"]
    vol_base = median([b.get("volume") or 0 for b in raw_bars]) or 1
    anon_dates = business_dates(len(raw_bars))

    bars = [
        {
            "time":   anon_dates[i],
            "open":   rnd(b["open"]  * scale),
            "high":   rnd(b["high"]  * scale),
            "low":    rnd(b["low"]   * scale),
            "close":  rnd(b["close"] * scale),
            "volume": max(0, round(((b.get("volume") or 0) / vol_base) * 100000)),
        }
        for i, b in enumerate(raw_bars)
    ]

    entry_idx = entry_idx_full - history_start
    exit_idx  = exit_idx_full  - history_start
    base_idx  = view_start - history_start

    exit_events = []
    for ex in pos["exits"]:
        idx = find_bar_idx(ohlcv, ex["exit_date"]) - history_start
        if 0 <= idx < len(bars):
            exit_events.append({
                "idx":    idx,
                "price":  rnd((ex.get("exit_price") or pos["entry_price"]) * scale),
                "reason": ex.get("exit_reason") or "exit",
            })

    last_exit = pos["exits"][-1] if pos["exits"] else {}
    position = {
        "entry_price": rnd(pos["entry_price"] * scale),
        "stop_price":  rnd(pos["stop_price"]  * scale),
        "tp1_price":   rnd(pos["tp1_price"]   * scale),
        "tp2_price":   rnd(pos["tp2_price"]   * scale) if pos.get("tp2_price") else None,
        "exit_reason": last_exit.get("exit_reason") or "exit",
        "exit_events": exit_events,
    }

    # Panels: bb_squeeze shows volume; ma_cross shows nothing extra
    show_vol = spec["id"] in ("bb_squeeze", "pivot_breakout")
    show_rsi = spec["id"] == "reversal"

    # indicator_params
    params = strat.get("params") or {}
    indicator_params = strat.get("indicator_params") or {}

    lesson = {
        "id":               spec["id"],
        "regime":           spec["regime"],
        "source_symbol":    sym,
        "source_liquidity": existing_liq.get(key),
        "params":           params,
        "indicator_params": indicator_params,
        "showVolumePanel":  show_vol,
        "showRsiPanel":     show_rsi,
        "bars":             bars,
        "baseIdx":          base_idx,
        "entryIdx":         entry_idx,
        "exitIdx":          exit_idx,
        "position":         position,
    }
    new_lessons.append(lesson)
    print(f"  Built: {len(bars)} bars, entryIdx={entry_idx}, exitIdx={exit_idx}, exit={position['exit_reason']}")

if new_lessons:
    # Remove any existing entries for same (id, regime) to avoid dupes
    existing = [l for l in learn.get("lessons", []) if not any(l["id"] == n["id"] and l["regime"] == n["regime"] for n in new_lessons)]
    learn["lessons"] = existing + new_lessons
    with open(LEARN_DATA, "w", encoding="utf-8") as f:
        json.dump(learn, f, default=str, separators=(",", ":"))
    print(f"\nWrote {len(learn['lessons'])} total lessons to {LEARN_DATA}")
else:
    print("No lessons added.")
