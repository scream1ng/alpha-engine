-- AlphaEngine PostgreSQL schema

CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT        NOT NULL,
    market          TEXT        NOT NULL,
    strategy        TEXT        NOT NULL,
    direction       TEXT        NOT NULL,
    entry           NUMERIC     NOT NULL,
    entry_type      TEXT        NOT NULL,
    sl              NUMERIC     NOT NULL,
    tp1             NUMERIC     NOT NULL,
    tp2             NUMERIC     NOT NULL,
    tp3             NUMERIC,
    atr             NUMERIC     NOT NULL,
    rr              NUMERIC     NOT NULL,
    score           NUMERIC     NOT NULL,
    sl_atr_mult     NUMERIC     NOT NULL,
    tp1_atr_mult    NUMERIC     NOT NULL,
    tp2_atr_mult    NUMERIC     NOT NULL,
    risk_pct        NUMERIC     NOT NULL,
    max_bars        INTEGER     NOT NULL,
    trail_atr_mult  NUMERIC     NOT NULL,
    be_trigger_atr_mult NUMERIC NOT NULL,
    meta            JSONB       DEFAULT '{}',
    generated_at    DATE        NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT      REFERENCES signals(id),
    symbol          TEXT        NOT NULL,
    market          TEXT        NOT NULL,
    strategy        TEXT        NOT NULL,
    direction       TEXT        NOT NULL,
    entry_price     NUMERIC     NOT NULL,
    exit_price      NUMERIC,
    sl_price        NUMERIC     NOT NULL,
    tp1_price       NUMERIC     NOT NULL,
    tp2_price       NUMERIC     NOT NULL,
    size            INTEGER     NOT NULL,
    entry_date      DATE        NOT NULL,
    exit_date       DATE,
    exit_reason     TEXT,
    bars_held       INTEGER,
    pnl             NUMERIC,
    is_paper        BOOLEAN     NOT NULL DEFAULT TRUE,
    is_open         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strategy_params (
    id              BIGSERIAL PRIMARY KEY,
    market          TEXT        NOT NULL,
    strategy        TEXT        NOT NULL,
    params          JSONB       NOT NULL,
    backtest_score  NUMERIC,
    backtest_sharpe NUMERIC,
    backtest_calmar NUMERIC,
    backtest_pf     NUMERIC,
    backtest_winrate NUMERIC,
    yearly_summary  JSONB       DEFAULT '{}',
    consistency_pass BOOLEAN,
    paper_gate_pass  BOOLEAN,
    is_live         BOOLEAN     NOT NULL DEFAULT FALSE,
    optimised_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (market, strategy)
);

CREATE TABLE IF NOT EXISTS strategy_candidates (
    id                  BIGSERIAL PRIMARY KEY,
    market              TEXT        NOT NULL,
    strategy            TEXT        NOT NULL,
    candidate_source    TEXT        NOT NULL,
    candidate_status    TEXT        NOT NULL,
    params              JSONB       NOT NULL,
    gate_hits           INTEGER     NOT NULL DEFAULT 0,
    gate_misses         JSONB       DEFAULT '[]',
    is_annual_return    NUMERIC,
    is_calmar           NUMERIC,
    is_profit_factor    NUMERIC,
    is_win_rate         NUMERIC,
    is_trade_count      INTEGER,
    is_max_drawdown     NUMERIC,
    oos_annual_return   NUMERIC,
    oos_calmar          NUMERIC,
    oos_profit_factor   NUMERIC,
    oos_win_rate        NUMERIC,
    oos_trade_count     INTEGER,
    oos_max_drawdown    NUMERIC,
    oos_pass            BOOLEAN     NOT NULL DEFAULT FALSE,
    evaluated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_candidates_market_status ON strategy_candidates(market, candidate_status, strategy);

CREATE TABLE IF NOT EXISTS pipeline_logs (
    id          BIGSERIAL   PRIMARY KEY,
    market      TEXT        NOT NULL,
    stage       TEXT        NOT NULL,   -- data_load | indicators | scan | rank | risk | route
    outcome     TEXT        NOT NULL,   -- ok | skip | error
    details     JSONB       DEFAULT '{}',
    logged_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_market_date  ON signals(market, generated_at);
CREATE INDEX IF NOT EXISTS idx_trades_market_symbol ON trades(market, symbol);
CREATE INDEX IF NOT EXISTS idx_trades_open          ON trades(is_open) WHERE is_open = TRUE;
CREATE INDEX IF NOT EXISTS idx_logs_market_stage    ON pipeline_logs(market, stage, logged_at);
