<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchEvents } from '../api/services'
import type { EventItem } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const rows = ref<EventItem[]>([])
const loading = ref(false)
const error = ref('')

async function loadEvents() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await fetchEvents()
  } catch (err) {
    error.value = `加载运行事件失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

onMounted(loadEvents)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">运行事件</span>
          <el-button size="small" @click="loadEvents">刷新</el-button>
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
      <el-table v-loading="loading" :data="rows" size="small">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.timestamp) }}</template>
        </el-table-column>
        <el-table-column prop="strategy_id" label="strategy_id" width="120" />
        <el-table-column prop="event_type" label="事件类型" width="160" />
        <el-table-column prop="detail" label="详情" min-width="420" />
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
</style>
