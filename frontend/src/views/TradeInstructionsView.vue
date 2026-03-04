<script setup lang="ts">
import { CloseBold } from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  cancelOtherOpenOrder,
  fetchActiveTradeInstructions,
  fetchOtherOpenOrders,
  fetchRecentCompletedTradeInstructions,
  fetchTradeInstructionOrders,
} from '../api/services'
import type { ActiveTradeInstruction, OtherOpenOrder, TradeOrder } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const router = useRouter()
const activeRows = ref<ActiveTradeInstruction[]>([])
const completedRows = ref<ActiveTradeInstruction[]>([])
const otherOpenOrderRows = ref<OtherOpenOrder[]>([])
const loading = ref(false)
const error = ref('')
const cancellingPermIds = ref<Set<number>>(new Set())
const instructionDisplayMode = ref<'active_only' | 'recent_week_all'>('active_only')
const orderRowsByTradeId = ref<Record<string, TradeOrder[]>>({})
const orderLoadingByTradeId = ref<Record<string, boolean>>({})
const orderErrorByTradeId = ref<Record<string, string>>({})
const loadedTradeOrderIds = ref<Set<string>>(new Set())
const TERMINAL_INSTRUCTION_STATUSES = new Set(['FILLED', 'CANCELLED', 'FAILED', 'EXPIRED'])
const TERMINAL_ORDER_STATUSES = new Set(['FILLED', 'CANCELLED', 'FAILED', 'EXPIRED'])

function toEpochMillis(value: string | null | undefined) {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

const instructionRows = computed<ActiveTradeInstruction[]>(() => {
  if (instructionDisplayMode.value === 'active_only') {
    return activeRows.value
  }

  // Merge active + recent terminal rows and keep latest row by trade_id.
  const byTradeId = new Map<string, ActiveTradeInstruction>()
  for (const row of [...activeRows.value, ...completedRows.value]) {
    const existing = byTradeId.get(row.trade_id)
    if (!existing || toEpochMillis(row.updated_at) >= toEpochMillis(existing.updated_at)) {
      byTradeId.set(row.trade_id, row)
    }
  }
  return [...byTradeId.values()].sort(
    (a, b) => toEpochMillis(b.updated_at) - toEpochMillis(a.updated_at),
  )
})

function resultType(result: string) {
  if (result === 'PASSED' || result === 'FILLED') return 'success'
  if (result === 'REJECTED' || result === 'FAILED') return 'danger'
  if (result === 'ORDER_SUBMITTED') return 'primary'
  return 'warning'
}

function formatOrderProgress(row: ActiveTradeInstruction) {
  const total = Number.isFinite(row.order_count) ? row.order_count : 0
  const filled = Number.isFinite(row.filled_order_count) ? row.filled_order_count : 0
  return `${filled}/${total}`
}

async function loadTradeData() {
  loading.value = true
  error.value = ''
  try {
    const [activeData, completedData, otherOpenOrders] = await Promise.all([
      fetchActiveTradeInstructions(),
      fetchRecentCompletedTradeInstructions(),
      fetchOtherOpenOrders(),
    ])
    activeRows.value = activeData
    completedRows.value = completedData
    otherOpenOrderRows.value = otherOpenOrders
    orderRowsByTradeId.value = {}
    orderLoadingByTradeId.value = {}
    orderErrorByTradeId.value = {}
    loadedTradeOrderIds.value = new Set()
  } catch (err) {
    error.value = `加载交易指令失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

function openStrategyDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

function openTradeLogsByTradeId(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  if (!normalized) return
  router.push({ path: '/trade-logs', query: { trade_id: normalized } })
}

async function openTradeOrders(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  if (!normalized) return
  const hasLoaded = loadedTradeOrderIds.value.has(normalized)
  if (hasLoaded) return
  orderLoadingByTradeId.value = {
    ...orderLoadingByTradeId.value,
    [normalized]: true,
  }
  orderErrorByTradeId.value = {
    ...orderErrorByTradeId.value,
    [normalized]: '',
  }
  try {
    const rows = await fetchTradeInstructionOrders(normalized)
    orderRowsByTradeId.value = {
      ...orderRowsByTradeId.value,
      [normalized]: rows,
    }
    loadedTradeOrderIds.value = new Set([...loadedTradeOrderIds.value, normalized])
  } catch (err) {
    orderRowsByTradeId.value = {
      ...orderRowsByTradeId.value,
      [normalized]: [],
    }
    orderErrorByTradeId.value = {
      ...orderErrorByTradeId.value,
      [normalized]: `加载订单明细失败：${String(err)}`,
    }
  } finally {
    orderLoadingByTradeId.value = {
      ...orderLoadingByTradeId.value,
      [normalized]: false,
    }
  }
}

function getTradeOrders(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  return orderRowsByTradeId.value[normalized] || []
}

function isTradeOrderLoading(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  return Boolean(orderLoadingByTradeId.value[normalized])
}

function tradeOrderError(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  return orderErrorByTradeId.value[normalized] || ''
}

async function refreshTradeOrders(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  if (!normalized) return
  loadedTradeOrderIds.value.delete(normalized)
  await openTradeOrders(normalized)
}

function onInstructionExpandChange(row: ActiveTradeInstruction, expandedRows: ActiveTradeInstruction[]) {
  const expanded = expandedRows.some((item) => item.trade_id === row.trade_id)
  if (!expanded) return
  void openTradeOrders(row.trade_id)
}

function formatQtyPair(quantity: number, filledQty: number) {
  const q = Number.isFinite(quantity) ? quantity : 0
  const f = Number.isFinite(filledQty) ? filledQty : 0
  return `${q}/${f}`
}

function canCancelOpenOrder(row: OtherOpenOrder) {
  return Boolean(row.can_cancel)
}

function canCancelActiveInstruction(row: ActiveTradeInstruction) {
  const permId = Number(row.perm_id)
  const normalizedStatus = String(row.status || '').trim().toUpperCase()
  return Number.isFinite(permId) && permId > 0 && !TERMINAL_INSTRUCTION_STATUSES.has(normalizedStatus)
}

function orderPermId(order: TradeOrder) {
  const value = Number(order.ib_order_id)
  if (!Number.isFinite(value) || value <= 0) return null
  return value
}

function canCancelOrder(order: TradeOrder) {
  const normalizedStatus = String(order.status || '').trim().toUpperCase()
  return orderPermId(order) !== null && !TERMINAL_ORDER_STATUSES.has(normalizedStatus)
}

function instructionRowClassName({ row }: { row: ActiveTradeInstruction }) {
  const normalizedStatus = String(row.status || '').trim().toUpperCase()
  return TERMINAL_INSTRUCTION_STATUSES.has(normalizedStatus) ? 'instruction-row--terminal' : ''
}

async function cancelByPermId(
  permIdRaw: number | null | undefined,
  confirmLabel?: string,
) {
  const permId = Number(permIdRaw)
  if (!Number.isFinite(permId) || permId <= 0) {
    ElMessage.error('无效 permId，无法撤单')
    return
  }
  if (cancellingPermIds.value.has(permId)) {
    return
  }

  try {
    const confirmDetail = confirmLabel || `permId=${permId}`
    await ElMessageBox.confirm(
      `确认撤销该交易指令吗？\n${confirmDetail}`,
      '确认撤单',
      {
        confirmButtonText: '确认撤单',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }

  cancellingPermIds.value.add(permId)
  try {
    await cancelOtherOpenOrder(permId)
    ElMessage.success(`撤单请求已发送（permId=${permId}）`)
    await loadTradeData()
  } catch (err) {
    ElMessage.error(`撤单失败：${String(err)}`)
  } finally {
    cancellingPermIds.value.delete(permId)
  }
}

async function cancelActiveInstruction(row: ActiveTradeInstruction) {
  await cancelByPermId(row.perm_id, `trade_id=${row.trade_id}`)
}

async function cancelOpenOrder(row: OtherOpenOrder) {
  await cancelByPermId(row.perm_id)
}

async function cancelTradeOrder(order: TradeOrder) {
  const permId = orderPermId(order)
  if (!permId) {
    ElMessage.error('当前订单没有可用 permId，无法撤单')
    return
  }
  await cancelByPermId(permId)
}

onMounted(loadTradeData)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">当前交易指令</span>
          <el-space>
            <el-select v-model="instructionDisplayMode" size="small" class="instruction-mode-select">
              <el-option label="仅显示未终态指令" value="active_only" />
              <el-option label="所有指令(最近一周)" value="recent_week_all" />
            </el-select>
            <el-button size="small" @click="loadTradeData">刷新</el-button>
          </el-space>
        </div>
      </template>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
        class="mb-12"
      />
      <el-table
        v-loading="loading"
        :data="instructionRows"
        size="small"
        :row-class-name="instructionRowClassName"
        row-key="trade_id"
        @expand-change="onInstructionExpandChange"
      >
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="策略/指令" width="210" class-name="strategy-id-col" label-class-name="strategy-id-header">
          <template #default="{ row }">
            <div class="trade-id-row">
              <el-link class="trade-id-link" type="primary" @click="openTradeLogsByTradeId(row.trade_id)">
                {{ row.trade_id }}
              </el-link>
            </div>
            <div class="strategy-id-row">
              <el-link class="strategy-id-link" type="primary" @click="openStrategyDetail(row.strategy_id)">
                {{ row.strategy_id }}
              </el-link>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="instruction_summary" label="指令摘要" min-width="220" />
        <el-table-column label="订单进度" width="96">
          <template #default="{ row }">
            <span>{{ formatOrderProgress(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <el-tag :type="resultType(row.status)" effect="dark">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="有效期" width="170">
          <template #default="{ row }">{{ formatIsoDateTime(row.expire_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button
              v-if="canCancelActiveInstruction(row)"
              size="small"
              type="danger"
              plain
              :loading="cancellingPermIds.has(row.perm_id ?? -1)"
              :disabled="cancellingPermIds.has(row.perm_id ?? -1)"
              @click="cancelActiveInstruction(row)"
            >
              撤单
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column type="expand" width="52" align="right">
          <template #default="{ row }">
            <div class="order-expand-wrap">
              <div class="order-expand-content">
                <div class="order-expand-header">
                  <span class="order-expand-title">trade {{ row.trade_id }} 的订单列表</span>
                  <el-button text size="small" @click="refreshTradeOrders(row.trade_id)">刷新</el-button>
                </div>
                <el-alert
                  v-if="tradeOrderError(row.trade_id)"
                  :title="tradeOrderError(row.trade_id)"
                  type="error"
                  show-icon
                  :closable="false"
                  class="mb-12"
                />
                <el-table
                  v-loading="isTradeOrderLoading(row.trade_id)"
                  :data="getTradeOrders(row.trade_id)"
                  size="small"
                  border
                  :fit="false"
                  class="order-detail-table"
                >
                  <el-table-column prop="sequence_no" label="顺序" width="72" />
                  <el-table-column prop="leg_role" label="类型" width="120" />
                  <el-table-column prop="status" label="状态" width="140" />
                <el-table-column label="permId" width="130">
                  <template #default="{ row: orderRow }">{{ orderPermId(orderRow) ?? '-' }}</template>
                </el-table-column>
                  <el-table-column label="数量/已成交" width="120">
                    <template #default="{ row: orderRow }">{{ formatQtyPair(orderRow.qty, orderRow.filled_qty) }}</template>
                  </el-table-column>
                <el-table-column label="均价" width="120">
                  <template #default="{ row: orderRow }">{{ orderRow.avg_fill_price ?? '-' }}</template>
                </el-table-column>
                <el-table-column label="操作" width="92" align="center">
                  <template #default="{ row: orderRow }">
                    <el-tooltip v-if="canCancelOrder(orderRow)" content="撤单" placement="top">
                      <el-button
                        class="order-cancel-icon-btn"
                        size="small"
                        type="danger"
                        plain
                        circle
                        :icon="CloseBold"
                        :loading="cancellingPermIds.has(orderPermId(orderRow) ?? -1)"
                        :disabled="cancellingPermIds.has(orderPermId(orderRow) ?? -1)"
                        @click="cancelTradeOrder(orderRow)"
                      />
                    </el-tooltip>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
              </el-table>
                <div
                  v-if="!isTradeOrderLoading(row.trade_id) && !tradeOrderError(row.trade_id) && getTradeOrders(row.trade_id).length === 0"
                  class="order-empty"
                >
                  暂无订单数据
                </div>
              </div>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">系统中其它交易指令</span>
          <span class="card-tools">仅显示 permId 不在当前有效交易指令中的 open orders</span>
        </div>
      </template>
      <el-table v-loading="loading" :data="otherOpenOrderRows" size="small">
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column prop="perm_id" label="permId" width="120" />
        <el-table-column label="client id" width="100">
          <template #default="{ row }">{{ row.client_id ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="标的" width="140">
          <template #default="{ row }"><span>{{ row.symbol }}</span>&nbsp;&nbsp;<span>({{ row.sec_type }})</span></template>
        </el-table-column>
        <el-table-column prop="side" label="方向" width="90" />
        <el-table-column prop="order_type" label="订单类型" width="100" />
        <el-table-column label="数量/已成交" width="110">
          <template #default="{ row }">{{ formatQtyPair(row.quantity, row.filled_qty) }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="170" />
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="canCancelOpenOrder(row)"
              size="small"
              type="danger"
              plain
              :loading="cancellingPermIds.has(row.perm_id)"
              :disabled="cancellingPermIds.has(row.perm_id)"
              @click="cancelOpenOrder(row)"
            >
              撤单
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

  </div>
</template>

<style scoped>
.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.mb-12 {
  margin-bottom: 12px;
}

:deep(.strategy-id-header .cell),
:deep(.strategy-id-col .cell),
:deep(.strategy-id-link) {
  white-space: nowrap;
}

.trade-id-link {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 14px;
  font-weight: 600;
}

.trade-id-row {
  margin-top: 2px;
}

.strategy-id-link {
  font-size: 12px;
  font-weight: 400;
}

.strategy-id-row {
  margin-top: 2px;
}

.instruction-mode-select {
  width: 172px;
}

.order-expand-wrap {
  padding: 6px 8px 10px 16px;
  display: flex;
  justify-content: flex-end;
}

.order-expand-content {
  width: fit-content;
  max-width: 100%;
  margin-left: auto;
}

.order-expand-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.order-expand-title {
  font-size: 13px;
  font-weight: 600;
}

.order-empty {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: right;
}

.order-detail-table {
  margin-left: auto;
}

.order-cancel-icon-btn :deep(.el-icon) {
  font-size: 14px;
}

:deep(.instruction-row--terminal > td.el-table__cell) {
  background-color: rgba(148, 163, 184, 0.14);
}

:deep(.el-table__body tr.instruction-row--terminal:hover > td.el-table__cell) {
  background-color: rgba(148, 163, 184, 0.2);
}
</style>
