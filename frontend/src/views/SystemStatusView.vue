<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { fetchSystemStatus } from '../api/services'
import type { SystemProviderStatus, SystemStatus } from '../api/types'

const loading = ref(false)
const error = ref('')
const status = ref<SystemStatus | null>(null)
const expandedDetailKeys = ref<Record<string, boolean>>({})
const router = useRouter()

const providerRows = computed(() => {
  const providers = status.value?.providers ?? {}
  return Object.entries(providers).map(([key, value]) => ({
    key,
    ...value,
  }))
})

function toLoadError(err: unknown) {
  if (axios.isAxiosError(err)) {
    const payload = err.response?.data as { detail?: unknown } | undefined
    const detailPayload = payload?.detail
    if (typeof detailPayload === 'string' && detailPayload) return `加载系统状态失败：${detailPayload}`
  }
  return `加载系统状态失败：${String(err)}`
}

function detailText(provider: SystemProviderStatus) {
  const details = provider.details ?? {}
  if (Object.keys(details).length === 0) return '-'
  return JSON.stringify(details, null, 2)
}

function hasDetails(provider: SystemProviderStatus) {
  return detailText(provider) !== '-'
}

function isExpanded(providerKey: string) {
  return Boolean(expandedDetailKeys.value[providerKey])
}

function toggleDetails(providerKey: string) {
  expandedDetailKeys.value[providerKey] = !expandedDetailKeys.value[providerKey]
}

async function loadStatus() {
  loading.value = true
  error.value = ''
  try {
    status.value = await fetchSystemStatus()
  } catch (err) {
    error.value = toLoadError(err)
  } finally {
    loading.value = false
  }
}

function openMarketDataProbe() {
  void router.push('/market-data-probe')
}

function openPositions() {
  void router.push('/positions')
}

onMounted(loadStatus)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">系统状态</span>
          <el-space>
            <el-button text size="small" @click="openPositions">持仓情况</el-button>
            <el-button text size="small" @click="openMarketDataProbe">行情测试</el-button>
            <el-button size="small" @click="loadStatus">刷新</el-button>
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
      <el-descriptions v-if="status" :column="2" border size="small">
        <el-descriptions-item label="网关模式">
          <el-tag :type="status.gateway.trading_mode === 'live' ? 'danger' : 'success'">
            {{ status.gateway.trading_mode }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="网关地址">
          {{ status.gateway.host }}:{{ status.gateway.api_port }}
        </el-descriptions-item>
        <el-descriptions-item label="paper/live 端口">
          {{ status.gateway.paper_port }} / {{ status.gateway.live_port }}
        </el-descriptions-item>
        <el-descriptions-item label="账户">
          {{ status.gateway.account_code || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="Worker 运行状态">
          <el-tag :type="status.worker.running ? 'success' : 'info'">
            {{ status.worker.running ? 'running' : 'stopped' }}
          </el-tag>
          <el-tag v-if="!status.worker.enabled" type="warning" class="ml-8">disabled</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Worker 线程">
          {{ status.worker.live_threads }} / {{ status.worker.configured_threads }}
        </el-descriptions-item>
        <el-descriptions-item label="队列长度">
          {{ status.worker.queue_length }} / {{ status.worker.queue_maxsize }}
        </el-descriptions-item>
        <el-descriptions-item label="进行中任务">
          {{ status.worker.inflight_tasks }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">Provider 运行情况</span>
      </template>
      <el-table v-loading="loading" :data="providerRows" size="small">
        <el-table-column prop="key" label="provider" width="140" />
        <el-table-column prop="configured" label="configured" width="120" />
        <el-table-column prop="runtime_mode" label="runtime mode" width="140" />
        <el-table-column prop="runtime_class" label="runtime class" min-width="180" />
        <el-table-column label="details" min-width="380">
          <template #default="{ row }">
            <div class="details-cell">
              <template v-if="hasDetails(row)">
                <el-button
                  text
                  size="small"
                  class="details-toggle-btn"
                  @click="toggleDetails(row.key)"
                >
                  {{ isExpanded(row.key) ? '收起详情' : '展开详情' }}
                </el-button>
                <el-collapse-transition>
                  <pre v-if="isExpanded(row.key)" class="details-json">{{ detailText(row) }}</pre>
                </el-collapse-transition>
              </template>
              <span v-else>-</span>
            </div>
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

.ml-8 {
  margin-left: 8px;
}

.details-json {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  color: #9fb0c3;
}

.details-cell {
  min-height: 24px;
}

.details-toggle-btn {
  padding-left: 0;
}
</style>
