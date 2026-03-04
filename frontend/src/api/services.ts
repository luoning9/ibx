import http from './http'
import type {
  ActiveTradeInstruction,
  ConditionRulesResponse,
  EventItem,
  MarketProfile,
  MarketDataProbePayload,
  MarketDataProbeResponse,
  PortfolioSummary,
  PositionItem,
  OtherOpenOrder,
  OpenOrderCancelResult,
  StrategyActionsPayload,
  StrategyBasicPatchPayload,
  StrategyCreatePayload,
  StrategyDescriptionResult,
  StrategyDetail,
  SystemStatus,
  StrategySummary,
  TradeOrder,
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

export async function copyStrategy(strategyId: string) {
  const { data } = await http.post<StrategyDetail>(`/strategies/${strategyId}/copy`)
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

export async function fetchGeneratedStrategyDescription(strategyId: string) {
  const normalized = String(strategyId || '').trim()
  const { data } = await http.get<StrategyDescriptionResult>(
    `/strategies/${encodeURIComponent(normalized)}/description/generate`,
  )
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
  payload: StrategyActionsPayload,
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

export async function fetchEvents(strategyId?: string) {
  const normalized = (strategyId || '').trim()
  const path = normalized ? `/strategies/${encodeURIComponent(normalized)}/events` : '/events'
  const { data } = await http.get<EventItem[]>(path)
  return data
}

export async function fetchConditionRules() {
  const { data } = await http.get<ConditionRulesResponse>('/condition-rules')
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

export async function fetchRecentCompletedTradeInstructions() {
  const { data } = await http.get<ActiveTradeInstruction[]>('/trade-instructions/completed-recent')
  return data
}

export async function fetchOtherOpenOrders() {
  const { data } = await http.get<OtherOpenOrder[]>('/trade-instructions/open-orders/others')
  return data
}

export async function cancelOtherOpenOrder(permId: number) {
  const { data } = await http.post<OpenOrderCancelResult>(`/trade-instructions/open-orders/${permId}/cancel`)
  return data
}

export async function fetchTradeLogs(tradeId?: string) {
  const normalized = (tradeId || '').trim()
  const { data } = await http.get<TradeLogItem[]>('/trade-logs', {
    params: normalized ? { trade_id: normalized } : undefined,
  })
  return data
}

export async function fetchTradeInstructionOrders(tradeId: string) {
  const normalized = (tradeId || '').trim()
  const { data } = await http.get<TradeOrder[]>(
    `/trade-instructions/${encodeURIComponent(normalized)}/orders`,
  )
  return data
}

export async function fetchSystemStatus() {
  const { data } = await http.get<SystemStatus>('/system-status')
  return data
}

export async function fetchMarkets() {
  const { data } = await http.get<MarketProfile[]>('/markets')
  return data
}

export async function probeMarketData(payload: MarketDataProbePayload) {
  const { data } = await http.post<MarketDataProbeResponse>('/market-data/probe', payload)
  return data
}
