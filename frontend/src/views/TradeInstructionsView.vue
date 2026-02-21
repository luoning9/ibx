<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { fetchActiveTradeInstructions, fetchTradeLogs } from '../api/services'
import type { ActiveTradeInstruction, TradeLogItem } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const router = useRouter()
const activeRows = ref<ActiveTradeInstruction[]>([])
const logs = ref<TradeLogItem[]>([])
const loading = ref(false)
const error = ref('')

function resultType(result: string) {
  if (result === 'PASSED' || result === 'FILLED') return 'success'
  if (result === 'REJECTED' || result === 'FAILED') return 'danger'
  if (result === 'ORDER_SUBMITTED') return 'primary'
  return 'warning'
}

async function loadTradeData() {
  loading.value = true
  error.value = ''
  try {
    const [activeData, logData] = await Promise.all([
      fetchActiveTradeInstructions(),
      fetchTradeLogs(),
    ])
    activeRows.value = activeData
    logs.value = logData
  } catch (err) {
    error.value = `加载交易指令失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

function openStrategyDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

onMounted(loadTradeData)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">当前有效交易指令</span>
          <el-space>
            <span class="card-tools">仅显示未终态指令</span>
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
      <el-table v-loading="loading" :data="activeRows" size="small">
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="strategy_id" width="120" class-name="strategy-id-col" label-class-name="strategy-id-header">
          <template #default="{ row }">
            <el-link class="strategy-id-link" type="primary" @click="openStrategyDetail(row.strategy_id)">
              {{ row.strategy_id }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="trade_id" label="trade_id" width="190" />
        <el-table-column prop="instruction_summary" label="指令摘要" min-width="260" />
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <el-tag :type="resultType(row.status)" effect="dark">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="有效期" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.expire_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">交易指令日志（核验 + 执行）</span>
      </template>
      <el-table v-loading="loading" :data="logs" size="small">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column prop="strategy_id" label="strategy_id" width="120" />
        <el-table-column prop="trade_id" label="trade_id" width="190" />
        <el-table-column prop="stage" label="阶段" width="140" />
        <el-table-column label="结果" width="170">
          <template #default="{ row }">
            <el-tag :type="resultType(row.result)" effect="dark">{{ row.result }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" min-width="260" />
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
</style>
