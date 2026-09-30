-- scanalert schema v1. Portable SQL (SQLite for development, PostgreSQL-compatible).
-- Timestamps are ISO-8601 UTC strings (sortable, lossless); JSON documents are TEXT.
-- Safety: paper_only / simulated columns carry CHECK constraints so live-looking rows cannot be stored.

CREATE TABLE symbols (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    exchange TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    listed_at TEXT,
    delisted_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE market_sessions (
    session_date TEXT PRIMARY KEY,
    is_trading_day INTEGER NOT NULL,
    open_utc TEXT,
    close_utc TEXT,
    early_close INTEGER NOT NULL DEFAULT 0,
    holiday_name TEXT,
    calendar_version TEXT NOT NULL
);

CREATE TABLE raw_market_events (
    event_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    feed TEXT,
    kind TEXT NOT NULL,
    symbol TEXT,
    source_ts TEXT,
    ingest_ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX ix_raw_events_symbol_ts ON raw_market_events (symbol, source_ts);
CREATE INDEX ix_raw_events_kind_ts ON raw_market_events (kind, source_ts);

CREATE TABLE quotes (
    symbol TEXT NOT NULL,
    source_ts TEXT NOT NULL,
    bid DOUBLE PRECISION NOT NULL,
    ask DOUBLE PRECISION NOT NULL,
    bid_size DOUBLE PRECISION,
    ask_size DOUBLE PRECISION,
    ingest_ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT,
    PRIMARY KEY (symbol, source_ts, bid, ask)
);

CREATE TABLE trades (
    symbol TEXT NOT NULL,
    source_ts TEXT NOT NULL,
    trade_id TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    conditions TEXT,
    ingest_ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT,
    PRIMARY KEY (symbol, source_ts, trade_id, price, size)
);

CREATE TABLE bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_ts TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    vwap DOUBLE PRECISION,
    trade_count INTEGER,
    corrected INTEGER NOT NULL DEFAULT 0,
    ingest_ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT,
    PRIMARY KEY (symbol, timeframe, bar_ts, revision)
);
CREATE INDEX ix_bars_ts ON bars (bar_ts);

CREATE TABLE strategies (
    strategy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE strategy_versions (
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version),
    FOREIGN KEY (strategy_id) REFERENCES strategies (strategy_id)
);

CREATE TABLE filters (
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    filter_id TEXT NOT NULL,
    filter_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    field TEXT NOT NULL,
    operator TEXT NOT NULL,
    value_json TEXT,
    unit TEXT,
    session_basis TEXT NOT NULL,
    lookback INTEGER,
    null_policy TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    description TEXT,
    PRIMARY KEY (strategy_id, strategy_version, scope, filter_id),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions (strategy_id, version)
);

CREATE TABLE formula_versions (
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    formula_id TEXT NOT NULL,
    expression TEXT NOT NULL,
    canonical TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (strategy_id, strategy_version, formula_id),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions (strategy_id, version)
);

CREATE TABLE alert_events (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_ts TEXT NOT NULL,
    detected_ts TEXT NOT NULL,
    session TEXT NOT NULL,
    trigger_price DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    spread_bps DOUBLE PRECISION,
    status TEXT NOT NULL,
    status_reason TEXT,
    priority TEXT NOT NULL,
    dedupe_key TEXT,
    expires_at TEXT,
    confirm_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    delivered_ts TEXT,
    delivery_delay_ms INTEGER,
    stale_data INTEGER NOT NULL DEFAULT 0,
    delayed_delivery INTEGER NOT NULL DEFAULT 0,
    data_provider TEXT,
    data_feed TEXT,
    strategy_config_hash TEXT,
    feature_snapshot TEXT NOT NULL,
    filter_snapshot TEXT NOT NULL,
    paper_only INTEGER NOT NULL DEFAULT 1 CHECK (paper_only = 1),
    CHECK (status IN ('working', 'triggered', 'invalidated', 'expired', 'acknowledged', 'suppressed'))
);
CREATE INDEX ix_alerts_source_ts ON alert_events (source_ts);
CREATE INDEX ix_alerts_symbol ON alert_events (symbol, source_ts);
CREATE INDEX ix_alerts_strategy ON alert_events (strategy_id, strategy_version, source_ts);
CREATE INDEX ix_alerts_status ON alert_events (status);
CREATE INDEX ix_alerts_dedupe ON alert_events (dedupe_key);

CREATE TABLE alert_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    reason TEXT,
    error TEXT,
    source_ts TEXT,
    detected_ts TEXT,
    delivered_ts TEXT,
    latency_ms INTEGER,
    kind TEXT NOT NULL DEFAULT 'alert',
    created_at TEXT NOT NULL
);
CREATE INDEX ix_deliveries_event ON alert_deliveries (event_id);

CREATE TABLE backtest_runs (
    run_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    date_start TEXT,
    date_end TEXT,
    data_provider TEXT,
    data_feed TEXT,
    params_json TEXT NOT NULL,
    strategy_snapshot TEXT NOT NULL,
    report_json TEXT,
    error TEXT,
    label TEXT NOT NULL DEFAULT 'BACKTEST - HISTORICAL SIMULATION - NOT A PREDICTION',
    simulated INTEGER NOT NULL DEFAULT 1 CHECK (simulated = 1)
);
CREATE INDEX ix_backtest_runs_strategy ON backtest_runs (strategy_id, created_at);

CREATE TABLE backtest_trades (
    run_id TEXT NOT NULL,
    trade_no INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_ts TEXT NOT NULL,
    exit_price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    gross_pnl DOUBLE PRECISION NOT NULL,
    costs DOUBLE PRECISION NOT NULL,
    net_pnl DOUBLE PRECISION NOT NULL,
    exit_reason TEXT NOT NULL,
    event_id TEXT,
    detail_json TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1 CHECK (simulated = 1),
    PRIMARY KEY (run_id, trade_no),
    FOREIGN KEY (run_id) REFERENCES backtest_runs (run_id)
);

CREATE TABLE paper_intents (
    intent_id TEXT PRIMARY KEY,
    event_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    entry_model TEXT NOT NULL,
    stop_model TEXT NOT NULL,
    target_model TEXT NOT NULL,
    time_exit_minutes INTEGER,
    est_spread_bps DOUBLE PRECISION,
    est_slippage_bps DOUBLE PRECISION,
    max_modeled_loss DOUBLE PRECISION,
    strategy_id TEXT,
    strategy_version INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    detail_json TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1 CHECK (simulated = 1),
    submitted_to_broker INTEGER NOT NULL DEFAULT 0 CHECK (submitted_to_broker = 0)
);
CREATE INDEX ix_paper_intents_event ON paper_intents (event_id);

CREATE TABLE paper_fills (
    fill_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION,
    fill_ts TEXT NOT NULL,
    model TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reject_reason TEXT,
    detail_json TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1 CHECK (simulated = 1),
    FOREIGN KEY (intent_id) REFERENCES paper_intents (intent_id)
);
CREATE INDEX ix_paper_fills_intent ON paper_fills (intent_id);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    detail TEXT
);
CREATE INDEX ix_audit_ts ON audit_log (ts);

CREATE TABLE user_preferences (
    user_id TEXT PRIMARY KEY,
    prefs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
