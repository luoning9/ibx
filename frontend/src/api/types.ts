export type StrategyStatus =
  | 'PENDING_ACTIVATION'
  | 'VERIFYING'
  | 'VERIFY_FAILED'
  | 'ACTIVE'
  | 'PAUSED'
  | 'TRIGGERED'
  | 'ORDER_SUBMITTED'
  | 'FILLED'
  | 'EXPIRED'
  | 'CANCELLED'
  | 'FAILED'

export type StrategyTradeType = 'buy' | 'sell' | 'switch' | 'open' | 'close' | 'spread'
export type SymbolTradeType = 'buy' | 'sell' | 'open' | 'close' | 'ref'
export type StrategyMarket = string

export type StrategySymbolItem = {
  code: string
  trade_type: SymbolTradeType
  contract_id: number | null
}

export type StrategySummary = {
  id: string
  status: StrategyStatus
  description: string
  updated_at: string
  expire_at: string | null
  upstream_strategy_id: string | null
  capabilities: {
    can_activate: boolean
    can_pause: boolean
    can_resume: boolean
    can_cancel: boolean
    can_delete: boolean
  }
}

export type CapabilityReasons = {
  can_activate: string | null
  can_pause: string | null
  can_resume: string | null
  can_cancel: string | null
  can_delete: string | null
}

export type ConditionState = 'TRUE' | 'FALSE' | 'WAITING' | 'NOT_EVALUATED'
export type TriggerGroupStatus = 'NOT_CONFIGURED' | 'MONITORING' | 'TRIGGERED' | 'EXPIRED'

export type ConditionRuntimeItem = {
  condition_id: string
  state: ConditionState
  last_value: number | null
  last_evaluated_at: string | null
}

export type TradeActionRuntime = {
  trade_status: string
  trade_id: string | null
  last_error: string | null
}

export type NextStrategyProjection = {
  id: string
  description: string | null
  status: StrategyStatus | 'NOT_SET' | 'UNKNOWN'
}

export type StrategyRunSummary = {
  // Mirrors backend StrategyRunSummaryOut.
  first_evaluated_at: string
  evaluated_at: string
  suggested_next_monitor_at: string | null
  condition_met: boolean
  decision_reason: string
  last_outcome: string
  run_count: number
  last_monitoring_data_end_at: Record<string, Record<string, string>>
  updated_at: string
}

export type StrategyDetail = {
  id: string
  description: string
  market: StrategyMarket
  sec_type: string
  exchange: string
  trade_type: StrategyTradeType
  symbols: StrategySymbolItem[]
  upstream_only_activation: boolean
  activated_at: string | null
  logical_activated_at: string | null
  expire_in_seconds: number | null
  expire_at: string | null
  status: StrategyStatus
  editable: boolean
  editable_reason: string | null
  capabilities: StrategySummary['capabilities']
  capability_reasons: CapabilityReasons
  condition_logic: 'AND' | 'OR'
  conditions_json: Array<Record<string, unknown>>
  trigger_group_status: TriggerGroupStatus
  conditions_runtime: ConditionRuntimeItem[]
  trade_action_json: Record<string, unknown> | null
  trade_action_runtime: TradeActionRuntime
  next_strategy: NextStrategyProjection | null
  upstream_strategy: NextStrategyProjection | null
  strategy_run: StrategyRunSummary | null
  anchor_price: number | null
  events: EventItem[]
  created_at: string
  updated_at: string
}

export type EventItem = {
  timestamp: string
  event_type: string
  detail: string
  strategy_id?: string
}

export type PortfolioSummary = {
  net_liquidation: number
  available_funds: number
  unrealized_pnl: number
  realized_pnl: number
  daily_pnl: number
  updated_at: string
}

export type PositionItem = {
  sec_type: 'STK' | 'FUT'
  symbol: string
  position_qty: number
  position_unit: '股' | '手'
  avg_price: number | null
  last_price: number | null
  market_value: number | null
  unrealized_pnl: number | null
  updated_at: string
}

export type ActiveTradeInstruction = {
  updated_at: string
  strategy_id: string
  trade_id: string
  instruction_summary: string
  status: string
  expire_at: string | null
}

export type TradeLogItem = {
  timestamp: string
  strategy_id: string
  trade_id: string
  stage: string
  result: string
  detail: string
}

export type StrategyCreatePayload = {
  id?: string
  idempotency_key?: string
  description: string
  market?: StrategyMarket
  trade_type: StrategyTradeType
  symbols: StrategySymbolItem[]
  upstream_only_activation?: boolean
  expire_mode: 'relative' | 'absolute'
  expire_in_seconds?: number | null
  expire_at?: string | null
  condition_logic?: 'AND' | 'OR'
  conditions?: Array<Record<string, unknown>>
  trade_action_json?: Record<string, unknown> | null
  next_strategy_id?: string | null
  next_strategy_note?: string | null
}

export type StrategyBasicPatchPayload = {
  description?: string
  market?: StrategyMarket
  trade_type?: StrategyTradeType
  symbols?: StrategySymbolItem[]
  upstream_only_activation?: boolean
  logical_activated_at?: string | null
  expire_mode?: 'relative' | 'absolute'
  expire_in_seconds?: number | null
  expire_at?: string | null
}

export type ConditionRulePair = {
  trigger_mode: string
  operator: '>=' | '<='
}

export type ConditionRulesResponse = {
  trigger_mode_windows: Record<
    string,
    Record<
      string,
      {
        base_bar: string
        confirm_consecutive: number
        confirm_ratio: number
        include_partial_bar: boolean
        missing_data_policy: string
      }
    >
  >
  metric_trigger_operator_rules: {
    allowed_windows: Record<string, string[]>
    allowed_rules: Record<string, ConditionRulePair[]>
  }
}

export type SystemGatewayStatus = {
  trading_mode: 'paper' | 'live'
  host: string
  api_port: number
  paper_port: number
  live_port: number
  account_code: string | null
}

export type SystemProviderStatus = {
  configured: string
  runtime_class: string | null
  runtime_mode: string | null
  details: Record<string, unknown>
}

export type SystemWorkerStatus = {
  enabled: boolean
  running: boolean
  monitor_interval_seconds: number
  configured_threads: number
  live_threads: number
  scanner_alive: boolean
  queue_length: number
  queue_maxsize: number
  inflight_tasks: number
}

export type SystemStatus = {
  gateway: SystemGatewayStatus
  worker: SystemWorkerStatus
  providers: Record<string, SystemProviderStatus>
}

export type MarketProfile = {
  market: string
  sec_type: string
  exchange: string
  currency: string
  allowed_trade_types: string[]
}

export type MarketDataProbePayload = {
  code: string
  market: string
  contract_month?: string | null
  start_time: string
  end_time: string
  bar_size: string
  what_to_show: string
  use_rth: boolean
  include_partial_bar: boolean
  max_bars?: number | null
  page_size?: number | null
}

export type MarketDataBar = {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number | null
  wap: number | null
  count: number | null
}

export type MarketDataProbeResponse = {
  provider_class: string
  request: Record<string, unknown>
  bars: MarketDataBar[]
  meta: Record<string, unknown>
}
