export type StrategyStatus =
  | 'PENDING_ACTIVATION'
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

export type StrategySymbolItem = {
  code: string
  trade_type: SymbolTradeType
}

export type StrategySummary = {
  id: string
  status: StrategyStatus
  description: string
  updated_at: string
  expire_at: string | null
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

export type StrategyDetail = {
  id: string
  description: string
  trade_type: StrategyTradeType
  symbols: StrategySymbolItem[]
  currency: 'USD'
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
  trade_type: StrategyTradeType
  symbols: StrategySymbolItem[]
  currency?: 'USD'
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
  trade_type?: StrategyTradeType
  symbols?: StrategySymbolItem[]
  upstream_only_activation?: boolean
  expire_mode?: 'relative' | 'absolute'
  expire_in_seconds?: number | null
  expire_at?: string | null
}
