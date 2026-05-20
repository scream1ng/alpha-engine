from __future__ import annotations
import os
from datetime import date, datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float,
    Integer, JSON, String, Text, UniqueConstraint, ForeignKey, create_engine, func, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///alpha_engine_dev.db")

# JSONB on Postgres gives index support; plain JSON on SQLite
if "postgresql" in DATABASE_URL:
    from sqlalchemy.dialects.postgresql import JSONB as _JsonType
else:
    _JsonType = JSON  # type: ignore[assignment,misc]

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

_COMPAT_MIGRATIONS: dict[str, list[str]] = {
    "strategy_params": [
        "backtest_annual_return REAL",
        "backtest_trade_count   INTEGER",
        "backtest_avg_win       REAL",
        "backtest_avg_loss      REAL",
        "backtest_max_dd        REAL",
        "yearly_summary         JSON",
    ],
    "regime_map": [
        "calmar REAL",
        "max_dd REAL",
    ],
    "research_runs": [
        "markdown_path TEXT",
    ],
    "regime_optimise": [
        "best_combo TEXT",
    ],
    "regime_entry": [
        "calmar_market_close   REAL",
        "calmar_limit_intraday REAL",
        "calmar_limit_fakeout  REAL",
    ],
}


class Base(DeclarativeBase):
    pass


class SignalModel(Base):
    __tablename__ = "signals"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String, nullable=False)
    market          = Column(String, nullable=False)
    strategy        = Column(String, nullable=False)
    direction       = Column(String, nullable=False)
    entry           = Column(Float, nullable=False)
    entry_type      = Column(String, nullable=False)
    sl              = Column(Float, nullable=False)
    tp1             = Column(Float, nullable=False)
    tp2             = Column(Float, nullable=False)
    tp3             = Column(Float)
    atr             = Column(Float, nullable=False)
    rr              = Column(Float, nullable=False)
    score           = Column(Float, nullable=False)
    sl_atr_mult     = Column(Float, nullable=False)
    tp1_atr_mult    = Column(Float, nullable=False)
    tp2_atr_mult    = Column(Float, nullable=False)
    risk_pct        = Column(Float, nullable=False)
    max_bars        = Column(Integer, nullable=False)
    trail_atr_mult  = Column(Float, nullable=False)
    be_trigger_atr_mult = Column(Float, nullable=False)
    meta            = Column(_JsonType, default={})
    generated_at    = Column(Date, nullable=False)
    created_at      = Column(DateTime, default=func.now())


class TradeModel(Base):
    __tablename__ = "trades"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    signal_id   = Column(Integer)
    symbol      = Column(String, nullable=False)
    market      = Column(String, nullable=False)
    strategy    = Column(String, nullable=False)
    direction   = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price  = Column(Float)
    sl_price    = Column(Float, nullable=False)
    tp1_price   = Column(Float, nullable=False)
    tp2_price   = Column(Float, nullable=False)
    size        = Column(Integer, nullable=False)
    entry_date  = Column(Date, nullable=False)
    exit_date   = Column(Date)
    exit_reason = Column(String)
    bars_held   = Column(Integer)
    pnl         = Column(Float)
    is_paper    = Column(Boolean, nullable=False, default=True)
    is_open     = Column(Boolean, nullable=False, default=True)
    created_at  = Column(DateTime, default=func.now())
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())


class StrategyParamsModel(Base):
    __tablename__ = "strategy_params"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    market           = Column(String, nullable=False)
    strategy         = Column(String, nullable=False)
    params           = Column(_JsonType, nullable=False)
    backtest_score         = Column(Float)
    backtest_annual_return = Column(Float)
    backtest_sharpe        = Column(Float)
    backtest_calmar        = Column(Float)
    backtest_pf            = Column(Float)
    backtest_winrate       = Column(Float)
    backtest_trade_count   = Column(Integer)
    backtest_avg_win       = Column(Float)
    backtest_avg_loss      = Column(Float)
    backtest_max_dd        = Column(Float)
    yearly_summary         = Column(_JsonType, default={})
    consistency_pass       = Column(Boolean)
    paper_gate_pass  = Column(Boolean)
    is_live          = Column(Boolean, nullable=False, default=False)
    optimised_at     = Column(DateTime, default=func.now())


class StrategyCandidateModel(Base):
    __tablename__ = "strategy_candidates"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    market               = Column(String, nullable=False)
    strategy             = Column(String, nullable=False)
    candidate_source     = Column(String, nullable=False)
    candidate_status     = Column(String, nullable=False)
    params               = Column(_JsonType, nullable=False)
    gate_hits            = Column(Integer, nullable=False, default=0)
    gate_misses          = Column(_JsonType, default=[])
    is_annual_return     = Column(Float)
    is_calmar            = Column(Float)
    is_profit_factor     = Column(Float)
    is_win_rate          = Column(Float)
    is_trade_count       = Column(Integer)
    is_max_drawdown      = Column(Float)
    oos_annual_return    = Column(Float)
    oos_calmar           = Column(Float)
    oos_profit_factor    = Column(Float)
    oos_win_rate         = Column(Float)
    oos_trade_count      = Column(Integer)
    oos_max_drawdown     = Column(Float)
    oos_pass             = Column(Boolean, nullable=False, default=False)
    evaluated_at         = Column(DateTime, default=func.now())


class RegimeMapModel(Base):
    __tablename__ = "regime_map"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    market       = Column(String, nullable=False)
    strategy     = Column(String, nullable=False)
    regime       = Column(String, nullable=False)   # uptrend / choppy / downtrend
    wr           = Column(Float)
    calmar       = Column(Float)
    max_dd       = Column(Float)
    trade_count  = Column(Integer)
    yearly       = Column(_JsonType, default={})    # {year: {ret_pct, trade_count, wr}}
    acceptable   = Column(Boolean, nullable=False, default=False)
    evaluated_at = Column(DateTime, default=func.now())


class RegimeLabelModel(Base):
    __tablename__ = "regime_labels"

    id       = Column(Integer, primary_key=True, autoincrement=True)
    market   = Column(String, nullable=False)
    date     = Column(Date, nullable=False)
    regime   = Column(String, nullable=False)
    saved_at = Column(DateTime, default=func.now())


class RegimeOptimiseModel(Base):
    __tablename__ = "regime_optimise"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    market           = Column(String, nullable=False)
    strategy         = Column(String, nullable=False)
    regime           = Column(String, nullable=False)
    params           = Column(_JsonType, nullable=False)
    is_calmar        = Column(Float)
    is_win_rate      = Column(Float)
    is_trade_count   = Column(Integer)
    is_annual_return = Column(Float)
    oos_calmar       = Column(Float)
    oos_win_rate     = Column(Float)
    oos_trade_count  = Column(Integer)
    oos_annual_return = Column(Float)
    oos_pass         = Column(Boolean, nullable=False, default=False)
    best_combo       = Column(String)
    optimised_at     = Column(DateTime, default=func.now())


class PaperPositionModel(Base):
    __tablename__ = "paper_positions"

    id               = Column(Integer, primary_key=True)
    market           = Column(String, nullable=False, index=True)
    symbol           = Column(String, nullable=False)
    strategy         = Column(String, nullable=False)
    regime           = Column(String, nullable=False)
    entry_date       = Column(Date, nullable=False)
    entry_price      = Column(Float, nullable=False)
    sl_price         = Column(Float, nullable=False)
    sl_current       = Column(Float, nullable=False)      # trail-adjusted live SL
    tp1_price        = Column(Float, nullable=False)
    tp2_price        = Column(Float, nullable=True)
    shares           = Column(Float, nullable=False)      # original position size
    remaining_shares = Column(Float, nullable=False)      # after partial exits
    highest_close    = Column(Float, nullable=False)      # for trail stop calc
    tp1_hit          = Column(Boolean, default=False)
    tp2_hit          = Column(Boolean, default=False)
    bars_held        = Column(Integer, default=0)
    exit_params      = Column(_JsonType)                  # stored from active strategy
    status           = Column(String, default="open")     # open|closed
    exit_date        = Column(Date, nullable=True)
    pnl              = Column(Float, default=0.0)
    source_signal_id = Column(Integer, ForeignKey("scan_signals.id"))


class PaperTradeModel(Base):
    __tablename__ = "paper_trades"

    id          = Column(Integer, primary_key=True)
    position_id = Column(Integer, ForeignKey("paper_positions.id"))
    exit_date   = Column(Date, nullable=False)
    exit_price  = Column(Float, nullable=False)
    exit_reason = Column(String, nullable=False)  # sl|tp1|tp2|ema10_exit|trail|time_stop
    shares      = Column(Float, nullable=False)
    pnl         = Column(Float, nullable=False)


class ScanSignalModel(Base):
    __tablename__ = "scan_signals"

    id               = Column(Integer, primary_key=True)
    market           = Column(String, nullable=False, index=True)
    scan_date        = Column(Date, nullable=False, index=True)
    symbol           = Column(String, nullable=False)
    strategy         = Column(String, nullable=False)
    regime_at_signal = Column(String, nullable=False)
    direction        = Column(String, default="long")
    entry_price      = Column(Float, nullable=False)
    sl_price         = Column(Float, nullable=False)
    tp1_price        = Column(Float, nullable=False)
    tp2_price        = Column(Float, nullable=True)
    atr_at_entry     = Column(Float)
    rvol_at_entry    = Column(Float)
    source_active_id = Column(Integer, ForeignKey("active_strategies.id"))
    status           = Column(String, default="new")  # new|opened|expired|skipped
    __table_args__ = (UniqueConstraint("market", "scan_date", "symbol", "strategy"),)


class ActiveStrategyModel(Base):
    __tablename__ = "active_strategies"

    id               = Column(Integer, primary_key=True)
    market           = Column(String, nullable=False, index=True)
    strategy         = Column(String, nullable=False)
    regime           = Column(String, nullable=False)
    params           = Column(_JsonType, nullable=False)
    indicator_params = Column(_JsonType)
    promoted_at      = Column(Date, nullable=False)
    retired_at       = Column(Date, nullable=True)
    status           = Column(String, default="active")  # active|retired
    source_combo     = Column(String)
    notes            = Column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("market", "strategy", "regime", "promoted_at"),)


class RegimeEntryModel(Base):
    __tablename__ = "regime_entry"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    market                = Column(String, nullable=False)
    strategy              = Column(String, nullable=False)
    regime                = Column(String, nullable=False)
    best_entry            = Column(String, nullable=False)  # market_close / limit_intraday / limit_fakeout
    calmar_market_close   = Column(Float)
    calmar_limit_intraday = Column(Float)
    calmar_limit_fakeout  = Column(Float)
    optimised_at          = Column(DateTime, default=func.now())


class ResearchRunModel(Base):
    __tablename__ = "research_runs"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    market            = Column(String, nullable=False)
    as_of             = Column(Date, nullable=False)
    symbols_requested = Column(Integer)
    symbols_loaded    = Column(Integer, nullable=False, default=0)
    export_path       = Column(String)
    markdown_path     = Column(String)
    summary           = Column(_JsonType, default={})
    completed_at      = Column(DateTime, default=func.now())


class PipelineLog(Base):
    __tablename__ = "pipeline_logs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    market     = Column(String, nullable=False)
    stage      = Column(String, nullable=False)
    outcome    = Column(String, nullable=False)
    details    = Column(_JsonType, default={})
    logged_at  = Column(DateTime, default=func.now())


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _apply_compat_migrations() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table_name, column_defs in _COMPAT_MIGRATIONS.items():
            if not inspector.has_table(table_name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for col_def in column_defs:
                col_name = col_def.split()[0]
                if col_name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_def}"))
                existing.add(col_name)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_compat_migrations()
