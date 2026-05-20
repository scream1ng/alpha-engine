from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import api.main as api_main
import db.models as db_models
from db.models import (
    ActiveStrategyModel,
    Base,
    PaperPositionModel,
    PaperTradeModel,
    PipelineLog,
    ScanSignalModel,
)
from scripts.pipeline import cmd_paper_update


def _session_factory(tmp_path, name: str = "paper.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def test_paper_portfolio_counts_closed_positions_and_marks_open_pnl(tmp_path, monkeypatch) -> None:
    session_factory, _ = _session_factory(tmp_path)
    monkeypatch.setattr(api_main, "SessionLocal", session_factory)
    monkeypatch.setattr(
        api_main,
        "_latest_close_for_symbol",
        lambda market, symbol: {"OPEN.AX": 12.0}.get(symbol),
    )

    db = session_factory()
    try:
        closed_win = PaperPositionModel(
            market="au",
            symbol="WIN.AX",
            strategy="ma_cross",
            regime="uptrend",
            entry_date=date(2026, 5, 1),
            entry_price=10.0,
            sl_price=9.0,
            sl_current=10.0,
            tp1_price=12.0,
            tp2_price=14.0,
            shares=100.0,
            remaining_shares=0.0,
            highest_close=12.0,
            tp1_hit=True,
            tp2_hit=False,
            bars_held=5,
            exit_params={"risk_pct": 0.005},
            status="closed",
            exit_date=date(2026, 5, 5),
            pnl=50.0,
        )
        closed_loss = PaperPositionModel(
            market="au",
            symbol="LOSS.AX",
            strategy="reversal",
            regime="downtrend",
            entry_date=date(2026, 5, 2),
            entry_price=8.0,
            sl_price=7.0,
            sl_current=7.0,
            tp1_price=9.5,
            tp2_price=None,
            shares=80.0,
            remaining_shares=0.0,
            highest_close=8.5,
            tp1_hit=False,
            tp2_hit=False,
            bars_held=3,
            exit_params={"risk_pct": 0.005},
            status="closed",
            exit_date=date(2026, 5, 4),
            pnl=-40.0,
        )
        open_pos = PaperPositionModel(
            market="au",
            symbol="OPEN.AX",
            strategy="bb_squeeze",
            regime="choppy",
            entry_date=date(2026, 5, 3),
            entry_price=10.0,
            sl_price=9.0,
            sl_current=9.5,
            tp1_price=11.5,
            tp2_price=None,
            shares=50.0,
            remaining_shares=50.0,
            highest_close=10.5,
            tp1_hit=False,
            tp2_hit=False,
            bars_held=2,
            exit_params={"risk_pct": 0.005},
            status="open",
            pnl=0.0,
        )
        db.add_all([closed_win, closed_loss, open_pos])
        db.flush()
        db.add_all(
            [
                PaperTradeModel(
                    position_id=closed_win.id,
                    exit_date=date(2026, 5, 4),
                    exit_price=11.0,
                    exit_reason="tp1",
                    shares=50.0,
                    pnl=30.0,
                ),
                PaperTradeModel(
                    position_id=closed_win.id,
                    exit_date=date(2026, 5, 5),
                    exit_price=11.4,
                    exit_reason="ema10_exit",
                    shares=50.0,
                    pnl=20.0,
                ),
                PaperTradeModel(
                    position_id=closed_loss.id,
                    exit_date=date(2026, 5, 4),
                    exit_price=7.5,
                    exit_reason="sl",
                    shares=80.0,
                    pnl=-40.0,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    payload = api_main.paper_portfolio("au")

    assert payload["summary"]["total_pnl"] == 10.0
    assert payload["summary"]["closed_trades"] == 2
    assert payload["summary"]["win_rate"] == 0.5
    assert payload["summary"]["open_pnl"] == 100.0
    assert payload["summary"]["equity_including_open"] == 100110.0
    assert payload["open_positions"][0]["latest_close"] == 12.0
    assert payload["open_positions"][0]["unrealized_pnl"] == 100.0


def test_pipeline_logs_endpoint_returns_market_and_global_rows(tmp_path, monkeypatch) -> None:
    session_factory, _ = _session_factory(tmp_path, "logs.db")
    monkeypatch.setattr(api_main, "SessionLocal", session_factory)

    db = session_factory()
    try:
        db.add_all(
            [
                PipelineLog(
                    market="all",
                    stage="scheduler:equities_scan_paper",
                    outcome="completed",
                    details={"duration_s": 12.5},
                ),
                PipelineLog(
                    market="th",
                    stage="scan",
                    outcome="ok",
                    details={"summary": "scan 2026-05-20 sig=3 active=1 new=2 opened=1"},
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    rows = api_main.pipeline_logs("th", 10, True)

    assert len(rows) == 2
    by_market = {row["market"]: row for row in rows}
    assert by_market["th"]["message"] == "scan 2026-05-20 sig=3 active=1 new=2 opened=1"
    assert "equities_scan_paper completed" in by_market["all"]["message"]


class _FakeAdapter:
    def __init__(self, market_id: str, symbol: str) -> None:
        self.market_id = market_id
        self._symbol = symbol

    def ohlcv_bulk(self, symbols, start, end):
        assert self._symbol in symbols
        idx = pd.date_range(end=end, periods=20, freq="D")
        df = pd.DataFrame(
            {
                "open": [10.0] * len(idx),
                "high": [10.5] * len(idx),
                "low": [9.5] * len(idx),
                "close": [10.0] * len(idx),
            },
            index=idx,
        )
        return {self._symbol: df}


def test_cmd_paper_update_uses_market_scoped_realized_equity(tmp_path, monkeypatch) -> None:
    session_factory, _ = _session_factory(tmp_path, "market_scope.db")
    monkeypatch.setattr(db_models, "SessionLocal", session_factory)

    today = date.today()
    db = session_factory()
    try:
        us_closed = PaperPositionModel(
            market="us",
            symbol="USWIN",
            strategy="ma_cross",
            regime="uptrend",
            entry_date=today - timedelta(days=3),
            entry_price=10.0,
            sl_price=9.0,
            sl_current=9.0,
            tp1_price=12.0,
            tp2_price=None,
            shares=100.0,
            remaining_shares=0.0,
            highest_close=12.0,
            tp1_hit=True,
            tp2_hit=False,
            bars_held=3,
            exit_params={"risk_pct": 0.005},
            status="closed",
            exit_date=today - timedelta(days=1),
            pnl=50000.0,
        )
        db.add(us_closed)
        db.flush()
        db.add(
            PaperTradeModel(
                position_id=us_closed.id,
                exit_date=today - timedelta(days=1),
                exit_price=15.0,
                exit_reason="tp1",
                shares=100.0,
                pnl=50000.0,
            )
        )
        active = ActiveStrategyModel(
            market="au",
            strategy="ma_cross",
            regime="uptrend",
            params={"risk_pct": 0.005},
            promoted_at=today,
            status="active",
            source_combo="f10s20",
        )
        db.add(active)
        db.flush()
        db.add(
            ScanSignalModel(
                market="au",
                scan_date=today,
                symbol="AUSETUP.AX",
                strategy="ma_cross",
                regime_at_signal="uptrend",
                direction="long",
                entry_price=10.0,
                sl_price=9.0,
                tp1_price=12.0,
                tp2_price=None,
                atr_at_entry=1.0,
                rvol_at_entry=1.2,
                source_active_id=active.id,
                status="new",
            )
        )
        db.commit()
    finally:
        db.close()

    cmd_paper_update(_FakeAdapter("au", "AUSETUP.AX"), args=type("Args", (), {})())

    db = session_factory()
    try:
        created = db.query(PaperPositionModel).filter_by(market="au", status="open").one()
        assert created.remaining_shares == 500.0
    finally:
        db.close()


def test_cmd_paper_update_respects_market_constraints_from_risk_policy(tmp_path, monkeypatch) -> None:
    session_factory, _ = _session_factory(tmp_path, "risk_policy.db")
    monkeypatch.setattr(db_models, "SessionLocal", session_factory)

    today = date.today()
    db = session_factory()
    try:
        active = ActiveStrategyModel(
            market="th",
            strategy="ma_cross",
            regime="uptrend",
            params={"risk_pct": 0.005},
            promoted_at=today,
            status="active",
            source_combo="f10s20",
        )
        db.add(active)
        db.flush()
        db.add(
            ScanSignalModel(
                market="th",
                scan_date=today,
                symbol="THSETUP.BK",
                strategy="ma_cross",
                regime_at_signal="uptrend",
                direction="long",
                entry_price=10.0,
                sl_price=9.0,
                tp1_price=12.0,
                tp2_price=None,
                atr_at_entry=1.0,
                rvol_at_entry=1.1,
                source_active_id=active.id,
                status="new",
            )
        )
        db.commit()
    finally:
        db.close()

    cmd_paper_update(_FakeAdapter("th", "THSETUP.BK"), args=type("Args", (), {})())

    db = session_factory()
    try:
        assert db.query(PaperPositionModel).filter_by(market="th", status="open").count() == 0
    finally:
        db.close()


def test_init_db_adds_missing_columns_for_existing_tables(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE strategy_params (
                    id INTEGER PRIMARY KEY,
                    market VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL,
                    params JSON NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE regime_optimise (
                    id INTEGER PRIMARY KEY,
                    market VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL,
                    regime VARCHAR NOT NULL,
                    params JSON NOT NULL
                )
                """
            )
        )

    monkeypatch.setattr(db_models, "engine", engine)
    monkeypatch.setattr(db_models, "DATABASE_URL", str(engine.url))

    db_models.init_db()

    inspector = db_models.inspect(engine)
    strategy_params_cols = {col["name"] for col in inspector.get_columns("strategy_params")}
    regime_optimise_cols = {col["name"] for col in inspector.get_columns("regime_optimise")}

    assert "backtest_annual_return" in strategy_params_cols
    assert "yearly_summary" in strategy_params_cols
    assert "best_combo" in regime_optimise_cols
