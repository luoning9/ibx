<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchStrategyDetail } from '../api/services'
import type { StrategyDetail } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const route = useRoute()
const router = useRouter()

const detail = ref<StrategyDetail | null>(null)
const loading = ref(false)
const error = ref('')

const strategyId = computed(() => String(route.params.id || ''))

async function loadDetail() {
  if (!strategyId.value) return
  loading.value = true
  error.value = ''
  try {
    detail.value = await fetchStrategyDetail(strategyId.value)
  } catch (err) {
    error.value = `加载策略详情失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

watch(strategyId, loadDetail)
onMounted(loadDetail)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">策略详情：{{ strategyId }}</span>
          <el-space>
            <el-button size="small" @click="loadDetail">刷新</el-button>
            <el-button size="small" @click="router.push('/strategies')">返回列表</el-button>
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

      <el-skeleton :loading="loading" animated :rows="6">
        <template #default>
          <el-descriptions v-if="detail" :column="2" size="small" border>
            <el-descriptions-item label="状态">{{ detail.status }}</el-descriptions-item>
            <el-descriptions-item label="描述">{{ detail.description }}</el-descriptions-item>
            <el-descriptions-item label="交易类型">{{ detail.trade_type }}</el-descriptions-item>
            <el-descriptions-item label="产品列表">
              <el-space wrap>
                <el-tag v-for="item in detail.symbols" :key="`${item.code}-${item.trade_type}`" size="small">
                  {{ item.code }} ({{ item.trade_type }})
                </el-tag>
              </el-space>
            </el-descriptions-item>
            <el-descriptions-item label="activated_at">
              {{ formatIsoDateTime(detail.activated_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="expire_at">
              {{ formatIsoDateTime(detail.expire_at) }}
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </el-skeleton>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">事件日志</span>
      </template>
      <el-table v-if="detail" :data="detail.events" size="small" v-loading="loading">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column prop="event_type" label="事件类型" width="160" />
        <el-table-column prop="detail" label="详情" min-width="360" />
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">触发条件（原始）</span>
      </template>
      <pre class="json-box">{{ JSON.stringify(detail?.conditions_json ?? [], null, 2) }}</pre>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">后续动作（原始）</span>
      </template>
      <pre class="json-box">{{ JSON.stringify(detail?.trade_action_json ?? null, null, 2) }}</pre>
    </el-card>
  </div>
</template>

<style scoped>
.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.json-box {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

.mb-12 {
  margin-bottom: 12px;
}
</style>
