<script setup lang="ts">
import { CloseBold, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  cancelOtherOpenOrder,
  fetchActiveTradeInstructions,
  fetchRecentCompletedTradeInstructions,
  fetchTradeLogs,
  fetchTradeInstructionOrders,
} from '../api/services'
import type { ActiveTradeInstruction, TradeLogItem, TradeOrder } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const route = useRoute()
const router = useRouter()

const instruction = ref<ActiveTradeInstruction | null>(null)
const orders = ref<TradeOrder[]>([])
const tradeLogs = ref<TradeLogItem[]>([])
const loading = ref(false)
const cancellingPermIds = ref<Set<number>>(new Set())
const refreshMode = ref<'manual' | '5s' | '10s' | '30s'>('manual')
let refreshTimer: number | null = null

const TERMINAL_ORDER_STATUSES = new Set(['FILLED', 'CANCELLED', 'FAILED', 'EXPIRED'])

const tradeId = computed(() => {
  const raw = route.params.tradeId
  if (Array.isArray(raw)) return String(raw[0] || '').trim()
  return String(raw || '').trim()
})

function toEpochMillis(value: string | null | undefined) {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function resultType(result: string) {
  if (result === 'PASSED' || result === 'FILLED') return 'success'
  if (result === 'REJECTED' || result === 'FAILED') return 'danger'
  if (result === 'ORDER_SUBMITTED') return 'primary'
  return 'warning'
}

function formatQtyPair(quantity: number, filledQty: number) {
  const q = Number.isFinite(quantity) ? quantity : 0
  const f = Number.isFinite(filledQty) ? filledQty : 0
  return `${q}/${f}`
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

async function loadDetail() {
  if (!tradeId.value) return
  loading.value = true
  try {
    const [activeRows, completedRows, orderRows, logRows] = await Promise.all([
      fetchActiveTradeInstructions(),
      fetchRecentCompletedTradeInstructions(),
      fetchTradeInstructionOrders(tradeId.value),
      fetchTradeLogs(tradeId.value),
    ])
    const byTradeId = new Map<string, ActiveTradeInstruction>()
    for (const row of [...activeRows, ...completedRows]) {
      const existing = byTradeId.get(row.trade_id)
      if (!existing || toEpochMillis(row.updated_at) >= toEpochMillis(existing.updated_at)) {
        byTradeId.set(row.trade_id, row)
      }
    }
    instruction.value = byTradeId.get(tradeId.value) || null
    orders.value = orderRows
    tradeLogs.value = logRows
  } catch (err) {
    ElMessage.error(`加载交易指令详情失败：${String(err)}`)
  } finally {
    loading.value = false
  }
}

function openStrategyDetail() {
  const strategyId = String(instruction.value?.strategy_id || '').trim()
  if (!strategyId) return
  router.push(`/strategies/${strategyId}`)
}

function clearRefreshTimer() {
  if (refreshTimer === null) return
  window.clearInterval(refreshTimer)
  refreshTimer = null
}

function setupRefreshTimer() {
  clearRefreshTimer()
  if (refreshMode.value === 'manual') return
  const intervalMs =
    refreshMode.value === '5s'
      ? 5000
      : refreshMode.value === '10s'
        ? 10000
        : 30000
  refreshTimer = window.setInterval(() => {
    if (loading.value) return
    void loadDetail()
  }, intervalMs)
}

async function cancelByPermId(permIdRaw: number | null | undefined) {
  const permId = Number(permIdRaw)
  if (!Number.isFinite(permId) || permId <= 0) {
    ElMessage.error('无效 permId，无法撤单')
    return
  }
  if (cancellingPermIds.value.has(permId)) return

  try {
    await ElMessageBox.confirm(
      `确认撤销该订单吗？\ntrade_id=${tradeId.value}\npermId=${permId}`,
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
    await loadDetail()
  } catch (err) {
    ElMessage.error(`撤单失败：${String(err)}`)
  } finally {
    cancellingPermIds.value.delete(permId)
  }
}

async function cancelTradeOrder(order: TradeOrder) {
  const permId = orderPermId(order)
  if (!permId) {
    ElMessage.error('当前订单没有可用 permId，无法撤单')
    return
  }
  await cancelByPermId(permId)
}

watch(tradeId, () => {
  void loadDetail()
})

watch(refreshMode, () => {
  setupRefreshTimer()
  if (refreshMode.value !== 'manual') {
    void loadDetail()
  }
})

onMounted(() => {
  void loadDetail()
})

onBeforeUnmount(() => {
  clearRefreshTimer()
})
</script>

<template>
  <div class="page-stack">
    <el-card class="header-only-card" shadow="never">
      <template #header>
        <div class="card-header-row">
          <div class="header-main">
            <div class="title-with-status">
              <span class="card-title">交易指令详情：{{ tradeId || '-' }}</span>
              <el-tag v-if="instruction" :type="resultType(instruction.status)" effect="dark">
                {{ instruction.status }}
              </el-tag>
            </div>
            <span class="instruction-summary" :title="instruction?.instruction_summary || '无指令摘要'">
              {{ instruction?.instruction_summary || '无指令摘要'
              }}<template v-if="instruction">（订单进度 {{ instruction.filled_order_count }}/{{ instruction.order_count }}）</template>
            </span>
          </div>
          <el-space>
            <el-button size="small" @click="openStrategyDetail">所属策略</el-button>
            <el-select v-model="refreshMode" size="small" class="refresh-mode-select">
              <el-option label="手动" value="manual" />
              <el-option label="5s" value="5s" />
              <el-option label="10s" value="10s" />
              <el-option label="30s" value="30s" />
            </el-select>
            <el-tooltip :content="refreshMode === 'manual' ? '刷新' : `自动刷新中（${refreshMode}）`" placement="top">
              <el-button
                class="refresh-icon-btn"
                size="small"
                circle
                :title="refreshMode === 'manual' ? '刷新' : `自动刷新中（${refreshMode}）`"
                aria-label="刷新"
                @click="loadDetail"
              >
                <el-icon :class="{ 'refresh-icon--spinning': refreshMode !== 'manual' }">
                  <RefreshRight />
                </el-icon>
              </el-button>
            </el-tooltip>
          </el-space>
        </div>
      </template>
    </el-card>

    <el-card class="compact-header-card" shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">订单明细</span>
        </div>
      </template>
      <el-table v-loading="loading" :data="orders" size="small" border>
        <el-table-column prop="sequence_no" label="顺序" width="72" />
        <el-table-column prop="leg_role" label="类型" width="120" />
        <el-table-column prop="status" label="状态" width="140" />
        <el-table-column label="permId" width="130">
          <template #default="{ row }">{{ orderPermId(row) ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="数量/已成交" width="120">
          <template #default="{ row }">{{ formatQtyPair(row.qty, row.filled_qty) }}</template>
        </el-table-column>
        <el-table-column label="均价" width="120">
          <template #default="{ row }">{{ row.avg_fill_price ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="更新时间" min-width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="92" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="canCancelOrder(row)" content="撤单" placement="top">
              <el-button
                class="order-cancel-icon-btn"
                size="small"
                type="danger"
                plain
                circle
                :icon="CloseBold"
                :loading="cancellingPermIds.has(orderPermId(row) ?? -1)"
                :disabled="cancellingPermIds.has(orderPermId(row) ?? -1)"
                @click="cancelTradeOrder(row)"
              />
            </el-tooltip>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!loading && orders.length === 0" class="orders-empty">暂无订单数据</div>
    </el-card>

    <el-card class="compact-header-card" shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">交易日志</span>
        </div>
      </template>
      <el-table v-loading="loading" :data="tradeLogs" size="small" border>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column prop="stage" label="阶段" width="140" />
        <el-table-column label="结果" width="170">
          <template #default="{ row }">
            <el-tag :type="resultType(row.result)" effect="dark">{{ row.result }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" min-width="260" />
      </el-table>
      <div v-if="!loading && tradeLogs.length === 0" class="orders-empty">暂无交易日志</div>
    </el-card>
  </div>
</template>

<style scoped>
.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.title-with-status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.instruction-summary {
  margin-top: 4px;
  color: #9fb0c3;
  font-size: 13px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.orders-empty {
  margin-top: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.header-only-card :deep(.el-card__body) {
  display: none;
}

.compact-header-card :deep(.el-card__header) {
  padding: 4px 10px;
}

.compact-header-card .card-header-row {
  min-height: 20px;
}

.order-cancel-icon-btn :deep(.el-icon) {
  font-size: 14px;
}

.refresh-mode-select {
  width: 92px;
}

.refresh-icon-btn :deep(.el-icon) {
  font-size: 15px;
}

.refresh-icon--spinning {
  animation: refresh-spin 1.15s linear infinite;
}

@keyframes refresh-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
