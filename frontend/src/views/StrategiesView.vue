<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { cancelStrategy, fetchStrategies } from '../api/services'
import type { StrategySummary } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const rows = ref<StrategySummary[]>([])
const loading = ref(false)
const error = ref('')
const router = useRouter()

function statusType(status: string) {
  if (status === 'ACTIVE') return 'success'
  if (status === 'PENDING_ACTIVATION') return 'info'
  if (status === 'CANCELLED' || status === 'FAILED' || status === 'EXPIRED') return 'danger'
  return 'warning'
}

async function loadStrategies() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await fetchStrategies()
  } catch (err) {
    error.value = `加载策略列表失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

async function onCancel(row: StrategySummary) {
  try {
    await ElMessageBox.confirm(`确认取消策略 ${row.id} 吗？`, '取消策略', {
      type: 'warning',
      confirmButtonText: '确认',
      cancelButtonText: '返回',
    })
  } catch {
    return
  }

  try {
    await cancelStrategy(row.id)
    ElMessage.success(`已取消策略 ${row.id}`)
    await loadStrategies()
  } catch (err) {
    ElMessage.error(`取消失败：${String(err)}`)
  }
}

function openDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

function onRowClick(row: StrategySummary) {
  openDetail(row.id)
}

onMounted(loadStrategies)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">策略列表</span>
          <el-space>
            <el-button size="small" @click="loadStrategies">刷新</el-button>
            <el-button type="primary" size="small">新建策略</el-button>
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
        :data="rows"
        size="small"
        :row-class-name="() => 'clickable-row'"
        @row-click="onRowClick"
      >
        <el-table-column prop="id" label="ID" width="100" />
        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" effect="dark">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="策略描述（自然语言）" min-width="360" />
        <el-table-column label="最近更新" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="过期时间" width="180">
          <template #default="{ row }">{{ formatIsoDateTime(row.expire_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="170">
          <template #default="{ row }">
            <el-space>
              <el-button size="small" @click.stop="openDetail(row.id)">详情</el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="!row.capabilities?.can_cancel"
                @click.stop="onCancel(row)"
              >
                取消
              </el-button>
            </el-space>
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

:deep(.clickable-row) {
  cursor: pointer;
}
</style>
