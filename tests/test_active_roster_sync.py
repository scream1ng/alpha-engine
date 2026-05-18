from core.active_roster import diff_active_roster


def test_diff_active_roster_detects_adds_and_retires() -> None:
    current = [
        {"strategy": "ma_cross", "regime": "uptrend", "params": {"fast": 10}, "source_combo": "old_combo"},
    ]
    desired = [
        {"strategy": "pivot_breakout", "regime": "downtrend", "params": {"lookback": 5}, "best_combo": "lb5_original"},
    ]

    diff = diff_active_roster(current, desired)

    assert len(diff["added"]) == 1
    assert diff["added"][0]["strategy"] == "pivot_breakout"
    assert len(diff["retired"]) == 1
    assert diff["retired"][0]["strategy"] == "ma_cross"
    assert not diff["changed"]


def test_diff_active_roster_detects_combo_change_for_same_pair() -> None:
    current = [
        {
            "strategy": "pivot_breakout",
            "regime": "downtrend",
            "params": {"lookback": 3},
            "source_combo": "lb3_original",
        },
    ]
    desired = [
        {
            "strategy": "pivot_breakout",
            "regime": "downtrend",
            "params": {"lookback": 5},
            "best_combo": "lb5_original",
            "is_calmar": 4.2,
        },
    ]

    diff = diff_active_roster(current, desired)

    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["old_combo"] == "lb3_original"
    assert diff["changed"][0]["new_combo"] == "lb5_original"
    assert not diff["added"]
    assert not diff["retired"]


def test_diff_active_roster_ignores_unchanged_pair() -> None:
    current = [
        {
            "strategy": "bb_squeeze",
            "regime": "choppy",
            "params": {"kc_mult": 2.0},
            "source_combo": "kc2p0_original",
        },
    ]
    desired = [
        {
            "strategy": "bb_squeeze",
            "regime": "choppy",
            "params": {"kc_mult": 2.0},
            "best_combo": "kc2p0_original",
        },
    ]

    diff = diff_active_roster(current, desired)

    assert len(diff["unchanged"]) == 1
    assert not diff["added"]
    assert not diff["changed"]
    assert not diff["retired"]
