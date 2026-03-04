<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  cancelOtherOpenOrder,
  fetchActiveTradeInstructions,
  fetchOtherOpenOrders,
  fetchRecentCompletedTradeInstructions,
} from '../api/services'
import type { ActiveTradeInstruction, OtherOpenOrder } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const router = useRouter()
const activeRows = ref<ActiveTradeInstruction[]>([])
const completedRows = ref<ActiveTradeInstruction[]>([])
const otherOpenOrderRows = ref<OtherOpenOrder[]>([])
const loading = ref(false)
const error = ref('')
const cancellingPermIds = ref<Set<number>>(new Set())
const instructionDisplayMode = ref<'active_only' | 'recent_week_all'>('active_only')
const TERMINAL_INSTRUCTION_STATUSES = new Set(['FILLED', 'CANCELLED', 'FAILED', 'EXPIRED'])

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
  } catch (err) {
    error.value = `加载交易指令失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

function openStrategyDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

function openTradeInstructionDetail(tradeId: string) {
  const normalized = String(tradeId || '').trim()
  if (!normalized) return
  router.push(`/trade-instructions/${encodeURIComponent(normalized)}`)
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
      >
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="策略/指令" width="210" class-name="strategy-id-col" label-class-name="strategy-id-header">
          <template #default="{ row }">
            <div class="trade-id-row">
              <el-link class="trade-id-link" type="primary" @click="openTradeInstructionDetail(row.trade_id)">
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

:deep(.instruction-row--terminal > td.el-table__cell) {
  background-color: rgba(148, 163, 184, 0.14);
}

:deep(.el-table__body tr.instruction-row--terminal:hover > td.el-table__cell) {
  background-color: rgba(148, 163, 184, 0.2);
}
</style>
