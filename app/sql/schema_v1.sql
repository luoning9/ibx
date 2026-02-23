PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ("schema_version", "1");

CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  description TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT "US_STOCK"
    CHECK (length(trim(market)) > 0),
  sec_type TEXT NOT NULL DEFAULT "STK"
    CHECK (length(trim(sec_type)) > 0),
  exchange TEXT NOT NULL DEFAULT "SMART"
    CHECK (length(trim(exchange)) > 0),
  trade_type TEXT NOT NULL
    CHECK (trade_type IN ("buy", "sell", "switch", "open", "close", "spread")),
  currency TEXT NOT NULL DEFAULT "USD"
    CHECK (length(trim(currency)) > 0),
  upstream_only_activation INTEGER NOT NULL DEFAULT 0
    CHECK (upstream_only_activation IN (0, 1)),
  expire_mode TEXT NOT NULL
    CHECK (expire_mode IN ("relative", "absolute")),
  expire_in_seconds INTEGER
    CHECK (expire_in_seconds IS NULL OR (expire_in_seconds BETWEEN 1 AND 604800)),
  expire_at TEXT,
  status TEXT NOT NULL
    CHECK (status IN (
      "PENDING_ACTIVATION", "VERIFYING", "VERIFY_FAILED",
      "ACTIVE", "PAUSED", "TRIGGERED", "ORDER_SUBMITTED",
      "FILLED", "EXPIRED", "CANCELLED", "FAILED"
    )),
  condition_logic TEXT NOT NULL DEFAULT "AND"
    CHECK (condition_logic IN ("AND", "OR")),
  conditions_json TEXT NOT NULL DEFAULT "[]"
    CHECK (json_valid(conditions_json)),
  trade_action_json TEXT
    CHECK (trade_action_json IS NULL OR json_valid(trade_action_json)),
  next_strategy_id TEXT REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE SET NULL,
  next_strategy_note TEXT,
  upstream_strategy_id TEXT,
  is_deleted INTEGER NOT NULL DEFAULT 0
    CHECK (is_deleted IN (0, 1)),
  deleted_at TEXT,
  anchor_price REAL,
  activated_at TEXT,
  logical_activated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
    CHECK (version > 0),
  CHECK (next_strategy_id IS NULL OR next_strategy_id <> id),
  CHECK (upstream_strategy_id IS NULL OR upstream_strategy_id <> id),
  CHECK (
    (expire_mode = "relative" AND expire_in_seconds IS NOT NULL)
    OR
    (expire_mode = "absolute" AND expire_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS strategy_symbols (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  position INTEGER NOT NULL CHECK (position >= 1),
  code TEXT NOT NULL,
  trade_type TEXT NOT NULL
    CHECK (trade_type IN ("buy", "sell", "open", "close", "ref")),
  contract_id INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE (strategy_id, position)
);

CREATE TABLE IF NOT EXISTS strategy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS condition_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  condition_id TEXT NOT NULL,
  state TEXT NOT NULL
    CHECK (state IN ("TRUE", "FALSE", "WAITING", "NOT_EVALUATED")),
  last_value REAL,
  last_evaluated_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (strategy_id, condition_id)
);

CREATE TABLE IF NOT EXISTS strategy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  evaluated_at TEXT NOT NULL,
  condition_met INTEGER NOT NULL CHECK (condition_met IN (0, 1)),
  decision_reason TEXT NOT NULL,
  metrics_json TEXT NOT NULL CHECK (json_valid(metrics_json))
);

CREATE TABLE IF NOT EXISTS strategy_runtime_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  state_key TEXT NOT NULL,
  state_value TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (strategy_id, state_key)
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  ib_order_id TEXT,
  status TEXT NOT NULL,
  qty REAL NOT NULL CHECK (qty > 0),
  avg_fill_price REAL,
  filled_qty REAL NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
  error_message TEXT,
  order_payload_json TEXT NOT NULL CHECK (json_valid(order_payload_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  trade_id TEXT,
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
  reason TEXT NOT NULL,
  order_snapshot_json TEXT NOT NULL CHECK (json_valid(order_snapshot_json)),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  trade_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  result TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_instructions (
  trade_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL REFERENCES strategies(id) ON UPDATE CASCADE ON DELETE CASCADE,
  instruction_summary TEXT NOT NULL,
  status TEXT NOT NULL,
  expire_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  net_liquidation REAL NOT NULL,
  available_funds REAL NOT NULL,
  daily_pnl REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sec_type TEXT NOT NULL CHECK (sec_type IN ("STK", "FUT")),
  symbol TEXT NOT NULL,
  position_qty REAL NOT NULL,
  position_unit TEXT NOT NULL CHECK (position_unit IN ("股", "手")),
  avg_price REAL,
  last_price REAL,
  market_value REAL,
  unrealized_pnl REAL,
  updated_at TEXT NOT NULL,
  UNIQUE (sec_type, symbol)
);

CREATE INDEX IF NOT EXISTS idx_strategies_status_expire_at
  ON strategies (status, expire_at);
CREATE INDEX IF NOT EXISTS idx_strategies_next_strategy_id
  ON strategies (next_strategy_id);

CREATE INDEX IF NOT EXISTS idx_strategy_symbols_strategy_id
  ON strategy_symbols (strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_symbols_code
  ON strategy_symbols (code);

CREATE INDEX IF NOT EXISTS idx_strategy_events_strategy_ts
  ON strategy_events (strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_condition_states_strategy
  ON condition_states (strategy_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_strategy_eval
  ON strategy_runs (strategy_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_runtime_states_strategy_updated
  ON strategy_runtime_states (strategy_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_strategy_status_updated
  ON orders (strategy_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_events_strategy_ts
  ON verification_events (strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_logs_ts
  ON trade_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trade_instructions_status_updated
  ON trade_instructions (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_updated
  ON portfolio_snapshots (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_updated
  ON positions (updated_at DESC);
