from __future__ import annotations
import os
from datetime import date, datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float,
    Integer, JSON, String, Text, create_engine, func,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///alpha_engine_dev.db")

# _JsonType on Postgres gives index support; plain JSON on SQLite
if "postgresql" in DATABASE_URL:
    from sqlalchemy.dialects.postgresql import _JsonType as _JsonType
else:
    _JsonType = JSON  # type: ignore[assignment,misc]

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    if "sqlite" in DATABASE_URL:
        from sqlalchemy import text
        new_cols = [
            "backtest_annual_return REAL",
            "backtest_trade_count   INTEGER",
            "backtest_avg_win       REAL",
            "backtest_avg_loss      REAL",
            "backtest_max_dd        REAL",
            "yearly_summary         JSON",
        ]
        with engine.connect() as conn:
            for col_def in new_cols:
                try:
                    conn.execute(text(f"ALTER TABLE strategy_params ADD COLUMN {col_def}"))
                    conn.commit()
                except Exception:
                    pass  # column already exists
