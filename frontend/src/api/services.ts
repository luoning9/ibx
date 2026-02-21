import http from './http'
import type {
  ActiveTradeInstruction,
  EventItem,
  PortfolioSummary,
  PositionItem,
  StrategyBasicPatchPayload,
  StrategyCreatePayload,
  StrategyDetail,
  StrategySummary,
  TradeLogItem,
} from './types'

export async function fetchStrategies() {
  const { data } = await http.get<StrategySummary[]>('/strategies')
  return data
}

export async function fetchStrategyDetail(strategyId: string) {
  const { data } = await http.get<StrategyDetail>(`/strategies/${strategyId}`)
  return data
}

export async function createStrategy(payload: StrategyCreatePayload) {
  const { data } = await http.post<StrategyDetail>('/strategies', payload)
  return data
}

export async function patchStrategyBasic(strategyId: string, payload: StrategyBasicPatchPayload) {
  const { data } = await http.patch<StrategyDetail>(`/strategies/${strategyId}/basic`, payload)
  return data
}

export async function putStrategyConditions(
  strategyId: string,
  payload: { condition_logic: 'AND' | 'OR'; conditions: Array<Record<string, unknown>> },
) {
  const { data } = await http.put<StrategyDetail>(`/strategies/${strategyId}/conditions`, payload)
  return data
}

export async function putStrategyActions(
  strategyId: string,
  payload: {
    trade_action_json?: Record<string, unknown> | null
    next_strategy_id?: string | null
    next_strategy_note?: string | null
  },
) {
  const { data } = await http.put<StrategyDetail>(`/strategies/${strategyId}/actions`, payload)
  return data
}

export async function cancelStrategy(strategyId: string) {
  await http.post(`/strategies/${strategyId}/cancel`)
}

export async function deleteStrategy(strategyId: string) {
  await http.delete(`/strategies/${strategyId}`)
}

export async function activateStrategy(strategyId: string) {
  await http.post(`/strategies/${strategyId}/activate`)
}

export async function pauseStrategy(strategyId: string) {
  await http.post(`/strategies/${strategyId}/pause`)
}

export async function resumeStrategy(strategyId: string) {
  await http.post(`/strategies/${strategyId}/resume`)
}

export async function fetchEvents() {
  const { data } = await http.get<EventItem[]>('/events')
  return data
}

export async function fetchPortfolioSummary() {
  const { data } = await http.get<PortfolioSummary>('/portfolio-summary')
  return data
}

export async function fetchPositions() {
  const { data } = await http.get<PositionItem[]>('/positions')
  return data
}

export async function fetchActiveTradeInstructions() {
  const { data } = await http.get<ActiveTradeInstruction[]>('/trade-instructions/active')
  return data
}

export async function fetchTradeLogs() {
  const { data } = await http.get<TradeLogItem[]>('/trade-logs')
  return data
}
