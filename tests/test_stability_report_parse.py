from scripts.pipeline import _parse_stability_winners


def test_parse_stability_winners_reads_optimised_verdict_table(tmp_path) -> None:
    report = tmp_path / "commodity_stability_latest.md"
    report.write_text(
        "\n".join([
            "# COMMODITY Stability Report",
            "",
            "## Optimised Winners",
            "",
            "| Strategy | Regime | Base Cal | Opt Cal | Episodes | Active | Pos | Median Ep | Worst Ep | Best Share | Verdict |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
            "| ma_cross | choppy | 4.55 | 4.91 | 31 | 4 | 3 | +1.1% | -0.5% | 72% | mixed |",
            "| reversal | uptrend | 1.27 | 1.26 | 27 | 10 | 7 | +0.4% | -1.1% | 63% | stable |",
            "",
        ]),
        encoding="utf-8",
    )

    rows = _parse_stability_winners(str(report))

    assert rows == [
        {
            "strategy": "ma_cross",
            "regime": "choppy",
            "base_cal": 4.55,
            "opt_cal": 4.91,
            "episodes": 31,
            "active": 4,
            "positive": 3,
            "median_episode": "+1.1%",
            "worst_episode": "-0.5%",
            "best_share": "72%",
            "verdict": "mixed",
        },
        {
            "strategy": "reversal",
            "regime": "uptrend",
            "base_cal": 1.27,
            "opt_cal": 1.26,
            "episodes": 27,
            "active": 10,
            "positive": 7,
            "median_episode": "+0.4%",
            "worst_episode": "-1.1%",
            "best_share": "63%",
            "verdict": "stable",
        },
    ]
