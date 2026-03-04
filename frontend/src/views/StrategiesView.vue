<script setup lang="ts">
import { CaretRight, CopyDocument } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { activateStrategy, copyStrategy, deleteStrategy, fetchStrategies, stopStrategy } from '../api/services'
import type { StrategySummary } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const rows = ref<StrategySummary[]>([])
const loading = ref(false)
const error = ref('')
const activatingId = ref('')
const stoppingId = ref('')
const copyingId = ref('')
const deleteEnabled = ref(false)
const router = useRouter()

function statusType(status: string) {
  if (status === 'ACTIVE') return 'success'
  if (status === 'VERIFYING') return 'warning'
  if (status === 'PENDING_ACTIVATION') return 'info'
  if (status === 'VERIFY_FAILED' || status === 'CANCELLED' || status === 'FAILED' || status === 'EXPIRED') return 'danger'
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

async function onDelete(row: StrategySummary) {
  try {
    await ElMessageBox.confirm(`确认删除策略 ${row.id} 吗？`, '删除策略', {
      type: 'warning',
      confirmButtonText: '确认删除',
      cancelButtonText: '返回',
    })
  } catch {
    return
  }

  try {
    await deleteStrategy(row.id)
    ElMessage.success(`已删除策略 ${row.id}`)
    await loadStrategies()
  } catch (err) {
    ElMessage.error(`删除失败：${String(err)}`)
  }
}

async function onActivate(row: StrategySummary) {
  activatingId.value = row.id
  try {
    await activateStrategy(row.id)
    ElMessage.success(`已发起激活：${row.id}`)
    await loadStrategies()
  } catch (err) {
    ElMessage.error(`激活失败：${String(err)}`)
  } finally {
    activatingId.value = ''
  }
}

async function onStop(row: StrategySummary) {
  stoppingId.value = row.id
  try {
    await stopStrategy(row.id)
    ElMessage.success(`已停止策略：${row.id}`)
    await loadStrategies()
  } catch (err) {
    ElMessage.error(`停止失败：${String(err)}`)
  } finally {
    stoppingId.value = ''
  }
}

async function onCopy(row: StrategySummary) {
  copyingId.value = row.id
  try {
    const created = await copyStrategy(row.id)
    ElMessage.success(`复制成功：${row.id} -> ${created.id}`)
    router.push(`/strategies/${created.id}`)
  } catch (err) {
    ElMessage.error(`复制失败：${String(err)}`)
  } finally {
    copyingId.value = ''
  }
}

function openDetail(strategyId: string) {
  router.push(`/strategies/${strategyId}`)
}

function onRowClick(row: StrategySummary) {
  openDetail(row.id)
}

function createStrategyEntry() {
  router.push('/strategies/new')
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
            <el-button type="primary" size="small" @click="createStrategyEntry">新建策略</el-button>
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
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-space>
              <el-tooltip content="激活策略" placement="top">
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :disabled="!row.capabilities?.can_activate"
                  :loading="activatingId === row.id"
                  title="激活"
                  aria-label="激活"
                  @click.stop="onActivate(row)"
                >
                  <el-icon v-if="row.capabilities?.can_activate"><CaretRight /></el-icon>
                </el-button>
              </el-tooltip>
              <el-tooltip
                :content="row.capabilities?.can_stop ? '停止并回到待激活' : ''"
                :disabled="!row.capabilities?.can_stop"
                placement="top"
              >
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :disabled="!row.capabilities?.can_stop"
                  :loading="stoppingId === row.id"
                  :title="row.capabilities?.can_stop ? '停止' : ''"
                  :aria-label="row.capabilities?.can_stop ? '停止' : ''"
                  @click.stop="onStop(row)"
                >
                  <span v-if="row.capabilities?.can_stop" class="stop-char-icon" aria-hidden="true">■</span>
                </el-button>
              </el-tooltip>
              <el-tooltip content="复制策略" placement="top">
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :loading="copyingId === row.id"
                  @click.stop="onCopy(row)"
                >
                  <el-icon><CopyDocument /></el-icon>
                </el-button>
              </el-tooltip>
            </el-space>
          </template>
        </el-table-column>
        <el-table-column width="90" align="center">
          <template #header>
            <el-checkbox v-model="deleteEnabled">删除</el-checkbox>
          </template>
          <template #default="{ row }">
            <el-button
              v-if="deleteEnabled && row.capabilities?.can_delete"
              size="small"
              type="danger"
              @click.stop="onDelete(row)"
            >
              删除
            </el-button>
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

.ops-icon-btn :deep(.el-icon) {
  font-size: 15px;
}

.ops-icon-btn.el-button {
  color: var(--el-text-color-regular);
  border-color: var(--el-border-color);
  background-color: transparent;
}

.ops-icon-btn.el-button:hover,
.ops-icon-btn.el-button:focus-visible {
  color: var(--el-text-color-regular);
  border-color: var(--el-border-color);
  background-color: transparent;
}

.ops-icon-btn.el-button.is-disabled,
.ops-icon-btn.el-button.is-disabled:hover {
  border-color: var(--el-border-color-light);
  background-color: transparent;
}

.stop-char-icon {
  font-size: 11px;
  line-height: 1;
}
</style>
