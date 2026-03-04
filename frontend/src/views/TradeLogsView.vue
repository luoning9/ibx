<script setup lang="ts">
import { RefreshRight } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchTradeLogs } from '../api/services'
import type { TradeLogItem } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const route = useRoute()
const router = useRouter()
const allRows = ref<TradeLogItem[]>([])
const loading = ref(false)
const error = ref('')
const refreshMode = ref<'manual' | '5s' | '10s' | '30s'>('manual')
const currentPage = ref(1)
const pageSize = 100
let refreshTimer: number | null = null

const tradeIdFilter = computed(() => {
  const raw = route.query.trade_id
  if (Array.isArray(raw)) return String(raw[0] || '').trim()
  return String(raw || '').trim()
})

const strategyIdForFilteredTrade = computed(() => {
  if (!tradeIdFilter.value) return ''
  const strategyIds = Array.from(
    new Set(
      allRows.value
        .map((row) => String(row.strategy_id || '').trim())
        .filter((id) => Boolean(id)),
    ),
  )
  if (strategyIds.length === 1) return strategyIds[0]
  return ''
})
const pageTitle = computed(() =>
  tradeIdFilter.value ? `交易${tradeIdFilter.value}的交易日志` : '所有交易日志',
)
const totalRows = computed(() => allRows.value.length)
const pagedRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return allRows.value.slice(start, start + pageSize)
})

function resultType(result: string) {
  if (result === 'PASSED' || result === 'FILLED') return 'success'
  if (result === 'REJECTED' || result === 'FAILED') return 'danger'
  if (result === 'ORDER_SUBMITTED') return 'primary'
  return 'warning'
}

async function loadTradeLogs() {
  loading.value = true
  error.value = ''
  try {
    allRows.value = await fetchTradeLogs(tradeIdFilter.value || undefined)
  } catch (err) {
    error.value = `加载交易日志失败：${String(err)}`
  } finally {
    loading.value = false
  }
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
    void loadTradeLogs()
  }, intervalMs)
}

function clearTradeFilter() {
  const nextQuery = { ...route.query }
  delete nextQuery.trade_id
  router.replace({ query: nextQuery })
}

function applyTradeFilter(tradeId: string) {
  const normalized = (tradeId || '').trim()
  if (!normalized) return
  if (normalized === tradeIdFilter.value) return
  router.replace({
    query: {
      ...route.query,
      trade_id: normalized,
    },
  })
}

function openStrategyDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

watch(tradeIdFilter, () => {
  currentPage.value = 1
  void loadTradeLogs()
}, { immediate: true })

watch(refreshMode, () => {
  setupRefreshTimer()
  if (refreshMode.value !== 'manual') {
    void loadTradeLogs()
  }
})

watch(totalRows, () => {
  const maxPage = Math.max(1, Math.ceil(totalRows.value / pageSize))
  if (currentPage.value > maxPage) {
    currentPage.value = maxPage
  }
})

onBeforeUnmount(() => {
  clearRefreshTimer()
})
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">
            {{ pageTitle }}
            <template v-if="tradeIdFilter && strategyIdForFilteredTrade">
              （<el-link type="primary" @click="openStrategyDetail(strategyIdForFilteredTrade)"
                >策略 {{ strategyIdForFilteredTrade }}</el-link
              >）
            </template>
          </span>
          <el-space>
            <el-button v-if="tradeIdFilter" size="small" @click="clearTradeFilter">所有日志</el-button>
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
                @click="loadTradeLogs"
              >
                <el-icon :class="{ 'refresh-icon--spinning': refreshMode !== 'manual' }">
                  <RefreshRight />
                </el-icon>
              </el-button>
            </el-tooltip>
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
      <el-table v-loading="loading" :data="pagedRows" size="small">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column v-if="!tradeIdFilter" label="trade_id" width="190">
          <template #default="{ row }">
            <el-tooltip content="点击只看该 trade 日志" placement="top">
              <el-button class="trade-id-btn" text size="small" @click="applyTradeFilter(row.trade_id)">
                {{ row.trade_id }}
              </el-button>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="stage" label="阶段" width="140" />
        <el-table-column label="结果" width="170">
          <template #default="{ row }">
            <el-tag :type="resultType(row.result)" effect="dark">{{ row.result }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" min-width="260" />
      </el-table>
      <div class="pagination-wrap">
        <el-pagination
          v-model:current-page="currentPage"
          layout="total, prev, pager, next"
          :total="totalRows"
          :page-size="pageSize"
          :pager-count="7"
          :disabled="loading"
          background
        />
      </div>
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

.trade-id-btn {
  padding: 0;
  font-weight: 400;
  text-decoration: underline;
}

.trade-id-btn :deep(span) {
  font-weight: 400;
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

.pagination-wrap {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
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
