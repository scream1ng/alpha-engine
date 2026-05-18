import json
from datetime import date

from core.active_roster import load_active_roster_payload, write_active_roster_snapshot


def test_write_active_roster_snapshot_merges_market_without_dropping_others(tmp_path) -> None:
    roster_path = tmp_path / "active_roster.json"
    roster_path.write_text(
        json.dumps({
            "generated": "2026-05-01",
            "markets": {
                "th": {
                    "generated": "2026-05-01",
                    "strategies": [{"strategy": "ma_cross", "regime": "uptrend", "source_combo": "f10s20"}],
                }
            },
        }),
        encoding="utf-8",
    )

    payload = write_active_roster_snapshot(
        "au",
        date(2026, 5, 17),
        [{"strategy": "pivot_breakout", "regime": "downtrend", "best_combo": "lb5_original", "params": {"lookback": 5}}],
        path=roster_path,
    )

    assert set(payload["markets"]) == {"au", "th"}
    assert payload["markets"]["au"]["strategies"][0]["source_combo"] == "lb5_original"
    assert payload["markets"]["th"]["strategies"][0]["strategy"] == "ma_cross"


def test_load_active_roster_payload_returns_empty_structure_for_missing_file(tmp_path) -> None:
    payload = load_active_roster_payload(tmp_path / "missing.json")

    assert payload == {"generated": None, "markets": {}}
