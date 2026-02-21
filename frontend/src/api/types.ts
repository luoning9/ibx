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
  }
}

export type StrategyDetail = {
  id: string
  description: string
  trade_type: StrategyTradeType
  symbols: StrategySymbolItem[]
  currency: 'USD'
  upstream_only_activation: boolean
  activated_at: string | null
  expire_in_seconds: number | null
  expire_at: string | null
  status: StrategyStatus
  condition_logic: 'AND' | 'OR'
  conditions_json: Array<Record<string, unknown>>
  trade_action_json: Record<string, unknown> | null
  events: EventItem[]
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
