import http from './http'
import type {
  ActiveTradeInstruction,
  EventItem,
  PortfolioSummary,
  PositionItem,
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

export async function cancelStrategy(strategyId: string) {
  await http.post(`/strategies/${strategyId}/cancel`)
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
