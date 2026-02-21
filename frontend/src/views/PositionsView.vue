<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchPortfolioSummary, fetchPositions } from '../api/services'
import type { PortfolioSummary, PositionItem } from '../api/types'
import { formatCurrency, formatIsoDateTime, formatNumber, formatSignedCurrency } from '../utils/format'

const summary = ref<PortfolioSummary | null>(null)
const rows = ref<PositionItem[]>([])
const loading = ref(false)
const error = ref('')

async function loadPositions() {
  loading.value = true
  error.value = ''
  try {
    const [summaryData, positionsData] = await Promise.all([
      fetchPortfolioSummary(),
      fetchPositions(),
    ])
    summary.value = summaryData
    rows.value = positionsData
  } catch (err) {
    error.value = `加载持仓失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

onMounted(loadPositions)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">账户持仓总览</span>
          <el-button size="small" @click="loadPositions">刷新</el-button>
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
      <el-row :gutter="16">
        <el-col :xs="12" :sm="12" :md="6">
          <div class="kv">
            <span>账户净值</span>
            <strong>{{ formatCurrency(summary?.net_liquidation) }}</strong>
          </div>
        </el-col>
        <el-col :xs="12" :sm="12" :md="6">
          <div class="kv">
            <span>可用资金</span>
            <strong>{{ formatCurrency(summary?.available_funds) }}</strong>
          </div>
        </el-col>
        <el-col :xs="12" :sm="12" :md="6">
          <div class="kv">
            <span>当日浮盈亏</span>
            <strong>{{ formatSignedCurrency(summary?.daily_pnl) }}</strong>
          </div>
        </el-col>
        <el-col :xs="12" :sm="12" :md="6">
          <div class="kv">
            <span>更新时间</span>
            <strong>{{ formatIsoDateTime(summary?.updated_at) }}</strong>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">持仓明细</span>
      </template>
      <el-table v-loading="loading" :data="rows" size="small">
        <el-table-column prop="sec_type" label="sec_type" width="100" />
        <el-table-column prop="symbol" label="symbol" width="110" />
        <el-table-column label="持仓" width="120">
          <template #default="{ row }">
            {{ formatNumber(row.position_qty, 2) }} {{ row.position_unit }}
          </template>
        </el-table-column>
        <el-table-column label="均价" width="120">
          <template #default="{ row }">{{ formatNumber(row.avg_price, 2) }}</template>
        </el-table-column>
        <el-table-column label="最新价" width="120">
          <template #default="{ row }">{{ formatNumber(row.last_price, 2) }}</template>
        </el-table-column>
        <el-table-column label="市值 / 名义" width="170">
          <template #default="{ row }">{{ formatCurrency(row.market_value) }}</template>
        </el-table-column>
        <el-table-column label="未实现盈亏" min-width="160">
          <template #default="{ row }">{{ formatSignedCurrency(row.unrealized_pnl) }}</template>
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

.kv {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 0;
}

.kv span {
  color: #9fb0c3;
  font-size: 12px;
}

.kv strong {
  font-size: 14px;
  font-weight: 600;
}
</style>
