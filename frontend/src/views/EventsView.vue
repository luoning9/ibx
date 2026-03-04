<script setup lang="ts">
import { RefreshRight } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchEvents } from '../api/services'
import type { EventItem } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

type DetailToken = {
  kind: 'text' | 'trade' | 'strategy'
  text: string
  id?: string
}

const DETAIL_ID_PATTERN = /\b([TS]-[A-Za-z0-9-]+)\b/g

const allRows = ref<EventItem[]>([])
const loading = ref(false)
const error = ref('')
const refreshMode = ref<'manual' | '5s' | '10s' | '30s'>('manual')
const currentPage = ref(1)
const pageSize = 100
const route = useRoute()
const router = useRouter()
let refreshTimer: number | null = null

const strategyIdFilter = computed(() => {
  const raw = route.query.strategy_id
  if (Array.isArray(raw)) return String(raw[0] || '').trim()
  return String(raw || '').trim()
})

const pageTitle = computed(() =>
  strategyIdFilter.value ? `策略${strategyIdFilter.value}的运行日志` : '所有运行日志',
)
const totalRows = computed(() => allRows.value.length)
const pagedRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return allRows.value.slice(start, start + pageSize)
})

async function loadEvents() {
  loading.value = true
  error.value = ''
  try {
    allRows.value = await fetchEvents(strategyIdFilter.value || undefined)
  } catch (err) {
    error.value = `加载运行日志失败：${String(err)}`
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
    void loadEvents()
  }, intervalMs)
}

function clearStrategyFilter() {
  const nextQuery = { ...route.query }
  delete nextQuery.strategy_id
  router.replace({ query: nextQuery })
}

function applyStrategyFilter(strategyId: string) {
  const normalized = (strategyId || '').trim()
  if (!normalized) return
  if (normalized === strategyIdFilter.value) return
  router.replace({
    query: {
      ...route.query,
      strategy_id: normalized,
    },
  })
}

function openStrategyDetail(strategyId: string) {
  const normalized = (strategyId || '').trim()
  if (!normalized) return
  void router.push(`/strategies/${encodeURIComponent(normalized)}`)
}

function openTradeInstructionDetail(tradeId: string) {
  const normalized = (tradeId || '').trim().toUpperCase()
  if (!normalized) return
  void router.push(`/trade-instructions/${encodeURIComponent(normalized)}`)
}

function openTradeLogsPage() {
  void router.push('/trade-logs')
}

function parseDetailTokens(detail: string): DetailToken[] {
  const source = String(detail || '')
  if (!source) return [{ kind: 'text', text: '' }]
  const tokens: DetailToken[] = []
  let cursor = 0
  for (const match of source.matchAll(DETAIL_ID_PATTERN)) {
    const matchedText = String(match[1] || match[0] || '')
    if (!matchedText) continue
    const start = match.index ?? 0
    if (start > cursor) {
      tokens.push({ kind: 'text', text: source.slice(cursor, start) })
    }
    const normalizedId = matchedText.trim().toUpperCase()
    tokens.push({
      kind: normalizedId.startsWith('T-') ? 'trade' : 'strategy',
      text: matchedText,
      id: normalizedId,
    })
    cursor = start + matchedText.length
  }
  if (cursor < source.length) {
    tokens.push({ kind: 'text', text: source.slice(cursor) })
  }
  return tokens.length > 0 ? tokens : [{ kind: 'text', text: source }]
}

watch(strategyIdFilter, () => {
  currentPage.value = 1
  void loadEvents()
}, { immediate: true })

watch(refreshMode, () => {
  setupRefreshTimer()
  if (refreshMode.value !== 'manual') {
    void loadEvents()
  }
})

onBeforeUnmount(() => {
  clearRefreshTimer()
})

watch(totalRows, () => {
  const maxPage = Math.max(1, Math.ceil(totalRows.value / pageSize))
  if (currentPage.value > maxPage) {
    currentPage.value = maxPage
  }
})
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <div class="title-with-link">
            <span class="card-title">
              <template v-if="strategyIdFilter">
                策略
                <el-button
                  class="title-strategy-link"
                  text
                  size="small"
                  @click="openStrategyDetail(strategyIdFilter)"
                >
                  {{ strategyIdFilter }}
                </el-button>
                的运行日志
              </template>
              <template v-else>
                {{ pageTitle }}
              </template>
            </span>
            <el-link type="primary" @click="openTradeLogsPage">查看交易日志</el-link>
          </div>
          <el-space>
            <el-button v-if="strategyIdFilter" size="small" @click="clearStrategyFilter">所有日志</el-button>
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
                @click="loadEvents"
              >
                <el-icon class="refresh-icon" :class="{ 'refresh-icon--spinning': refreshMode !== 'manual' }">
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
        <el-table-column v-if="!strategyIdFilter" label="strategy_id" width="140">
          <template #default="{ row }">
            <el-tooltip content="点击只看该策略日志" placement="top">
              <el-button
                class="strategy-id-btn"
                text
                size="small"
                @click="applyStrategyFilter(row.strategy_id)"
              >
                {{ row.strategy_id }}
              </el-button>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="event_type" label="事件类型" width="160" />
        <el-table-column label="详情" min-width="420">
          <template #default="{ row }">
            <span class="detail-cell">
              <template v-for="(token, idx) in parseDetailTokens(row.detail)" :key="idx">
                <el-link
                  v-if="token.kind === 'trade'"
                  type="primary"
                  @click="openTradeInstructionDetail(token.id || token.text)"
                >
                  {{ token.text }}
                </el-link>
                <el-link
                  v-else-if="token.kind === 'strategy'"
                  type="primary"
                  @click="openStrategyDetail(token.id || token.text)"
                >
                  {{ token.text }}
                </el-link>
                <span v-else>{{ token.text }}</span>
              </template>
            </span>
          </template>
        </el-table-column>
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

.title-with-link {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.mb-12 {
  margin-bottom: 12px;
}

.strategy-id-btn {
  padding: 0;
  font-weight: 400;
  text-decoration: underline;
}

.strategy-id-btn :deep(span) {
  font-weight: 400;
}

.title-strategy-link {
  padding: 0 2px;
  margin: 0 1px;
  text-decoration: underline;
}

.detail-cell {
  white-space: pre-wrap;
  word-break: break-word;
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
