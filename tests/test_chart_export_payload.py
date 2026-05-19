from scripts.pipeline import _merge_chart_export_payload


def _market_payload(market: str, generated: str) -> dict:
    return {
        "generated": generated,
        "market": market,
        "current_regime": "uptrend",
        "regime_map": {},
        "regime_periods": [],
        "strategies": [],
        "ohlcv": {},
    }


def test_merge_chart_export_payload_upgrades_single_market_payload() -> None:
    existing = _market_payload("TH", "2026-05-12")
    au_output = _market_payload("AU", "2026-05-13")

    merged = _merge_chart_export_payload(existing, au_output)

    assert merged["version"] == 2
    assert merged["default_market"] == "AU"
    assert set(merged["markets"]) == {"AU", "TH"}
    assert merged["markets"]["TH"]["market"] == "TH"
    assert merged["markets"]["AU"]["generated"] == "2026-05-13"


def test_merge_chart_export_payload_replaces_existing_market_snapshot() -> None:
    existing = {
        "version": 2,
        "default_market": "TH",
        "markets": {
            "TH": _market_payload("TH", "2026-05-11"),
            "AU": _market_payload("AU", "2026-05-12"),
        },
    }
    th_output = _market_payload("TH", "2026-05-13")

    merged = _merge_chart_export_payload(existing, th_output)

    assert merged["default_market"] == "TH"
    assert merged["markets"]["TH"]["generated"] == "2026-05-13"
    assert merged["markets"]["AU"]["generated"] == "2026-05-12"
