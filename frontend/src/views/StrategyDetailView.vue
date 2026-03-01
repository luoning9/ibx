<script setup lang="ts">
import axios from 'axios'
import { CaretRight, CloseBold, Link, RefreshRight, VideoPause } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'

import {
  activateStrategy,
  cancelStrategy,
  fetchGeneratedStrategyDescription,
  fetchStrategyDetail,
  patchStrategyBasic,
  pauseStrategy,
  resumeStrategy,
} from '../api/services'
import type { StrategyDetail } from '../api/types'
import { formatIsoDateTime } from '../utils/format'

const route = useRoute()
const router = useRouter()

const detail = ref<StrategyDetail | null>(null)
const loading = ref(false)
const error = ref('')
const generatedDescription = ref('')
const descriptionEditing = ref(false)
const descriptionDraft = ref('')
const descriptionEditWrapRef = ref<HTMLElement | null>(null)

const strategyId = computed(() => String(route.params.id || ''))
type ConditionViewItem = {
  id: string
  expression: string
  naturalLanguage: string
  stateText: string
  stateClass: string
  meta: string[]
}
type FollowupField = {
  label: string
  value: string
}

async function applyGeneratedDescriptionIfEmpty(data: StrategyDetail) {
  generatedDescription.value = ''
  if (String(data.description || '').trim()) {
    return data
  }
  try {
    const generated = await fetchGeneratedStrategyDescription(data.id)
    const generatedText = String(generated.description || '').trim()
    generatedDescription.value = generatedText
    if (generatedText) {
      return {
        ...data,
        description: generatedText,
      }
    }
  } catch {
    generatedDescription.value = ''
  }
  return data
}

async function loadDetail() {
  if (!strategyId.value) return
  loading.value = true
  error.value = ''
  try {
    const data = await fetchStrategyDetail(strategyId.value)
    detail.value = await applyGeneratedDescriptionIfEmpty(data)
  } catch (err) {
    error.value = `加载策略详情失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

const displayDescription = computed(() => {
  const explicit = String(detail.value?.description || '').trim()
  if (explicit) return explicit
  const generated = String(generatedDescription.value || '').trim()
  return generated || '-'
})

function startDescriptionEdit() {
  const current = detail.value
  if (!current) return
  if (!current.editable) {
    ElMessage.warning(current.editable_reason || '当前状态不可编辑描述')
    return
  }
  descriptionDraft.value = String(current.description || '').trim()
  descriptionEditing.value = true
}

function cancelDescriptionEdit() {
  descriptionEditing.value = false
  descriptionDraft.value = ''
}

function handleDocumentMouseDown(event: MouseEvent) {
  if (!descriptionEditing.value) return
  const wrap = descriptionEditWrapRef.value
  if (!wrap) return
  const target = event.target
  if (target instanceof Node && wrap.contains(target)) return
  cancelDescriptionEdit()
}

async function submitDescriptionEdit() {
  const current = detail.value
  if (!current) return
  const nextDescription = String(descriptionDraft.value || '').trim()
  if (nextDescription === String(current.description || '').trim()) {
    descriptionEditing.value = false
    return
  }
  try {
    const updated = await patchStrategyBasic(current.id, { description: nextDescription })
    detail.value = await applyGeneratedDescriptionIfEmpty(updated)
    descriptionEditing.value = false
    ElMessage.success('描述已更新')
  } catch (err) {
    error.value = toActionError('更新描述', err)
  }
}

function pretty(value: unknown) {
  return JSON.stringify(value ?? null, null, 2)
}

function asString(value: unknown) {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function asObject(value: unknown) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function formatConditionValue(value: unknown) {
  if (value == null) return ''
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function buildConditionExpression(condition: Record<string, unknown>) {
  const explicit = asString(condition.expression)
  if (explicit) return explicit

  const metric = asString(condition.metric)
  const operator = asString(condition.operator)
  const value = formatConditionValue(condition.value)
  const product = asString(condition.product) || asString(condition.product_a)
  const productB = asString(condition.product_b)

  if (metric && product && productB && operator && value) {
    return `${metric.toLowerCase()}(${product}, ${productB}) ${operator} ${value}`
  }
  if (metric && product && operator && value) {
    return `${metric.toLowerCase()}(${product}) ${operator} ${value}`
  }

  const fallback = [metric, operator, value].filter(Boolean).join(' ')
  return fallback || 'N/A'
}

function mapConditionState(state: string | undefined) {
  switch (state) {
    case 'TRUE':
      return { text: '已满足', className: 'is-true' }
    case 'FALSE':
      return { text: '未满足', className: 'is-false' }
    case 'WAITING':
      return { text: '等待中', className: 'is-waiting' }
    default:
      return { text: '未评估', className: 'is-not-evaluated' }
  }
}

const conditionViewItems = computed<ConditionViewItem[]>(() => {
  const strategy = detail.value
  if (!strategy) return []

  const runtimeById = new Map(strategy.conditions_runtime.map((item) => [item.condition_id, item]))
  return strategy.conditions_json.map((raw, index) => {
    const condition = asObject(raw)
    const id = asString(condition.condition_id) || `c${index + 1}`
    const runtime = runtimeById.get(id)
    const stateInfo = mapConditionState(runtime?.state)

    const meta: string[] = [`ID: ${id}`]
    const conditionType = asString(condition.condition_type)
    const metric = asString(condition.metric)
    const triggerMode = asString(condition.trigger_mode)
    const evalWindow = asString(condition.evaluation_window)
    const priceBasis = asString(condition.window_price_basis)
    if (conditionType) meta.push(conditionType)
    if (metric) meta.push(metric)
    if (triggerMode) meta.push(triggerMode)
    if (evalWindow) meta.push(`window: ${evalWindow}`)
    if (priceBasis) meta.push(`price_basis: ${priceBasis}`)

    return {
      id,
      expression: buildConditionExpression(condition),
      naturalLanguage: asString(condition.condition_nl) || '无自然语言描述',
      stateText: stateInfo.text,
      stateClass: stateInfo.className,
      meta,
    }
  })
})

const hasConditions = computed(() => conditionViewItems.value.length > 0)
const hasTradeAction = computed(() => Boolean(detail.value?.trade_action_json))
const hasNextStrategy = computed(() => Boolean(detail.value?.next_strategy))
const hasFollowupConfigured = computed(() => hasTradeAction.value || hasNextStrategy.value)
const tradeAction = computed(() => asObject(detail.value?.trade_action_json))

function toUpper(value: unknown) {
  return asString(value).trim().toUpperCase()
}

function formatValue(value: unknown, fallback = '-') {
  if (value == null) return fallback
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  const text = asString(value).trim()
  return text || fallback
}

function actionTypeLabel(actionType: string) {
  if (actionType === 'STOCK_TRADE') return '股票买卖（STOCK_TRADE）'
  if (actionType === 'FUT_POSITION') return '期货开平（FUT_POSITION）'
  if (actionType === 'FUT_ROLL') return '期货展期（FUT_ROLL）'
  return actionType || '-'
}

function sideLabel(side: string) {
  if (side === 'BUY') return '买入（BUY）'
  if (side === 'SELL') return '卖出（SELL）'
  return side || '-'
}

function positionEffectLabel(effect: string) {
  if (effect === 'OPEN') return '开仓（OPEN）'
  if (effect === 'CLOSE') return '平仓（CLOSE）'
  return effect || '-'
}

function orderTypeLabel(orderType: string) {
  if (orderType === 'MKT') return '市价（MKT）'
  if (orderType === 'LMT') return '限价（LMT）'
  return orderType || '-'
}

function statusTagType(statusRaw: string) {
  const status = statusRaw.trim().toUpperCase()
  if (status === 'ACTIVE' || status === 'TRIGGERED' || status === 'FILLED') return 'success'
  if (status === 'VERIFYING') return 'warning'
  if (status === 'PAUSED' || status === 'ORDER_SUBMITTED') return 'warning'
  if (status === 'PENDING_ACTIVATION') return 'info'
  if (status === 'VERIFY_FAILED' || status === 'EXPIRED' || status === 'CANCELLED' || status === 'FAILED') return 'danger'
  return 'info'
}

const followupStatusText = computed(() => {
  if (!hasFollowupConfigured.value) return '后续动作状态：未设置'
  if (hasTradeAction.value) {
    return `后续动作状态：${detail.value?.trade_action_runtime?.trade_status || 'NOT_SET'}`
  }
  if (hasNextStrategy.value) return '后续动作状态：仅激活下游策略'
  return '后续动作状态：未设置'
})

const tradeActionSummary = computed(() => {
  if (!hasTradeAction.value) return '未配置交易动作（仅激活下游策略）'

  const action = tradeAction.value
  const actionType = toUpper(action.action_type)
  const symbol = formatValue(action.symbol, '')
  const quantity = formatValue(action.quantity, '')

  if (actionType === 'STOCK_TRADE') {
    const side = sideLabel(toUpper(action.side))
    const orderType = orderTypeLabel(toUpper(action.order_type))
    return `${side} ${quantity || '-'} 股 ${symbol || '-'}（${orderType}）`
  }
  if (actionType === 'FUT_POSITION') {
    const effect = positionEffectLabel(toUpper(action.position_effect))
    const side = sideLabel(toUpper(action.side))
    const orderType = orderTypeLabel(toUpper(action.order_type))
    const contract = formatValue(action.contract, symbol || '-')
    return `${effect} ${side} ${quantity || '-'} 手 ${contract}（${orderType}）`
  }
  if (actionType === 'FUT_ROLL') {
    const closeContract = formatValue(action.close_contract)
    const openContract = formatValue(action.open_contract)
    return `展期 ${closeContract} -> ${openContract}，数量 ${quantity || '-'} 手`
  }

  return `动作类型：${actionTypeLabel(actionType)}`
})

const tradeActionType = computed(() => toUpper(tradeAction.value.action_type))
const isFutPositionAction = computed(() => tradeActionType.value === 'FUT_POSITION')
const isFutRollAction = computed(() => tradeActionType.value === 'FUT_ROLL')
const showNormalTradeFields = computed(() => tradeActionType.value !== 'FUT_ROLL')

function quantityUnitByActionType(actionType: string) {
  return actionType === 'STOCK_TRADE' ? '股' : '手'
}

function limitPriceValue(orderType: unknown, limitPrice: unknown) {
  return toUpper(orderType) === 'LMT' ? formatValue(limitPrice) : '不适用'
}

function boolLabel(value: unknown, fallback = '否') {
  if (typeof value === 'boolean') return value ? '是' : '否'
  const normalized = toUpper(value)
  if (normalized === 'TRUE') return '是'
  if (normalized === 'FALSE') return '否'
  return fallback
}

const tradeRuntimeFields = computed<FollowupField[]>(() => {
  if (!detail.value?.trade_action_runtime) return []
  const runtime = detail.value.trade_action_runtime
  const fields: FollowupField[] = []
  if (runtime.trade_id) fields.push({ label: 'trade_id', value: runtime.trade_id })
  if (runtime.last_error) fields.push({ label: '最近错误', value: runtime.last_error })
  return fields
})

function openNextStrategy() {
  const nextId = detail.value?.next_strategy?.id
  if (!nextId) return
  router.push(`/strategies/${nextId}`)
}

function openUpstreamStrategy() {
  const upstreamId = detail.value?.upstream_strategy?.id
  if (!upstreamId) return
  router.push(`/strategies/${upstreamId}`)
}

function toActionError(actionLabel: string, err: unknown) {
  if (axios.isAxiosError(err)) {
    const payload = err.response?.data as { detail?: unknown } | undefined
    const detailPayload = payload?.detail
    if (detailPayload && typeof detailPayload === 'object') {
      const detailObj = detailPayload as Record<string, unknown>
      const code = typeof detailObj.code === 'string' ? detailObj.code : ''
      if (code === 'STRATEGY_LOCKED') {
        const lockUntilRaw = typeof detailObj.lock_until === 'string' ? detailObj.lock_until : ''
        const lockUntilText = lockUntilRaw ? `（锁到期：${formatIsoDateTime(lockUntilRaw)}）` : ''
        return `${actionLabel}失败：策略正在执行中，暂时不能修改状态，请稍后重试${lockUntilText}`
      }
      const apiMsg =
        (typeof detailObj.message === 'string' && detailObj.message) ||
        (typeof detailObj.detail === 'string' && detailObj.detail) ||
        ''
      if (apiMsg) return `${actionLabel}失败：${apiMsg}`
    }
    if (typeof detailPayload === 'string' && detailPayload) {
      return `${actionLabel}失败：${detailPayload}`
    }
  }
  return `${actionLabel}失败：${String(err)}`
}

async function doActivate() {
  if (!detail.value) return
  try {
    await activateStrategy(detail.value.id)
    ElMessage.success(`策略 ${detail.value.id} 已进入校验中`)
    await loadDetail()
  } catch (err) {
    error.value = toActionError('激活', err)
  }
}

async function doPause() {
  if (!detail.value) return
  try {
    await pauseStrategy(detail.value.id)
    ElMessage.success(`策略 ${detail.value.id} 已暂停`)
    await loadDetail()
  } catch (err) {
    error.value = toActionError('暂停', err)
  }
}

async function doResume() {
  if (!detail.value) return
  try {
    await resumeStrategy(detail.value.id)
    ElMessage.success(`策略 ${detail.value.id} 已恢复`)
    await loadDetail()
  } catch (err) {
    error.value = toActionError('恢复', err)
  }
}

async function doCancel() {
  if (!detail.value) return
  try {
    await cancelStrategy(detail.value.id)
    ElMessage.success(`策略 ${detail.value.id} 已取消`)
    await loadDetail()
  } catch (err) {
    error.value = toActionError('取消', err)
  }
}

function goEditBasic() {
  router.push(`/strategies/${strategyId.value}/edit/basic`)
}

function goEditConditions() {
  router.push(`/strategies/${strategyId.value}/edit/conditions`)
}

function goEditActions() {
  router.push(`/strategies/${strategyId.value}/edit/actions`)
}

function openRunningLogs() {
  if (!strategyId.value) return
  router.push({ path: '/events', query: { strategy_id: strategyId.value } })
}

function scrollToSection(section: 'conditions' | 'actions') {
  const targetId = section === 'conditions' ? 'conditions-section' : 'actions-section'
  const el = document.getElementById(targetId)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function parseAnchor(value: unknown): 'conditions' | 'actions' | null {
  const raw = Array.isArray(value) ? value[0] : value
  if (raw === 'conditions' || raw === 'actions') return raw
  return null
}

async function applyAnchorFromQuery() {
  const anchor = parseAnchor(route.query.anchor)
  if (!anchor || !detail.value) return
  await nextTick()
  scrollToSection(anchor)
  const nextQuery: LocationQueryRaw = { ...route.query }
  delete nextQuery.anchor
  router.replace({ path: route.path, query: nextQuery }).catch(() => undefined)
}

watch(
  () => [detail.value?.id, route.query.anchor],
  () => {
    void applyAnchorFromQuery()
  },
)

watch(strategyId, loadDetail)
onMounted(() => {
  void loadDetail()
  document.addEventListener('mousedown', handleDocumentMouseDown, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', handleDocumentMouseDown, true)
})
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row detail-header-row">
          <div class="detail-header-main">
            <div class="detail-title-row">
              <span class="card-title">策略详情：{{ strategyId }}</span>
              <div class="detail-anchor-group">
                <el-button size="small" class="detail-anchor-btn" @click="scrollToSection('conditions')">
                  触发条件
                </el-button>
                <el-button size="small" class="detail-anchor-btn" @click="scrollToSection('actions')">
                  后续动作
                </el-button>
                <el-button
                  v-if="detail?.upstream_strategy?.id"
                  size="small"
                  class="detail-anchor-btn detail-upstream-btn"
                  :icon="Link"
                  title="上游策略"
                  aria-label="上游策略"
                  @click="openUpstreamStrategy"
                >
                  上游
                </el-button>
              </div>
            </div>
            <div ref="descriptionEditWrapRef" class="detail-description-wrap">
              <el-input
                v-if="descriptionEditing"
                v-model="descriptionDraft"
                size="small"
                class="detail-description-input"
                autofocus
                maxlength="300"
                @keydown.enter.prevent="submitDescriptionEdit"
                @keydown.esc.prevent="cancelDescriptionEdit"
              />
              <span
                v-else
                class="detail-description"
                title="双击编辑，回车提交"
                @dblclick="startDescriptionEdit"
              >
                {{ displayDescription }}
              </span>
            </div>
          </div>
          <el-space>
            <el-button size="small" @click="loadDetail">刷新</el-button>
            <el-button size="small" @click="openRunningLogs">运行日志</el-button>
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
          <div v-if="detail" class="detail-top">
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="market">
                {{ detail.market }} ({{ detail.sec_type }}/{{ detail.exchange }})
              </el-descriptions-item>
              <el-descriptions-item label="交易类型">{{ detail.trade_type }}</el-descriptions-item>
              <el-descriptions-item label="产品列表">
                <el-space wrap>
                  <el-tag
                    v-for="item in detail.symbols"
                    :key="`${item.code}-${item.trade_type}`"
                    size="small"
                  >
                    {{ item.code }} ({{ item.trade_type }})
                  </el-tag>
                </el-space>
              </el-descriptions-item>
              <el-descriptions-item label="activated_at">
                {{ formatIsoDateTime(detail.activated_at) }}
                <template v-if="detail.logical_activated_at">
                  （logical: {{ formatIsoDateTime(detail.logical_activated_at) }}）
                </template>
              </el-descriptions-item>
              <el-descriptions-item label="expire_at">
                {{ formatIsoDateTime(detail.expire_at) }}
              </el-descriptions-item>
              <el-descriptions-item label="capabilities">
                <el-space>
                  <el-tag :type="detail.capabilities.can_activate ? 'success' : 'info'">activate</el-tag>
                  <el-tag :type="detail.capabilities.can_pause ? 'success' : 'info'">pause</el-tag>
                  <el-tag :type="detail.capabilities.can_resume ? 'success' : 'info'">resume</el-tag>
                  <el-tag :type="detail.capabilities.can_cancel ? 'danger' : 'info'">cancel</el-tag>
                  <el-tag :type="detail.capabilities.can_delete ? 'warning' : 'info'">delete</el-tag>
                </el-space>
              </el-descriptions-item>
            </el-descriptions>
            <div class="ops-row">
              <el-space class="ops-left" wrap>
                <span class="ops-status">策略状态：{{ detail.status }}</span>
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :icon="CaretRight"
                  title="激活"
                  aria-label="激活"
                  @click="doActivate"
                  :disabled="!detail.capabilities.can_activate"
                />
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :icon="VideoPause"
                  title="暂停"
                  aria-label="暂停"
                  @click="doPause"
                  :disabled="!detail.capabilities.can_pause"
                />
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  circle
                  :icon="RefreshRight"
                  title="恢复"
                  aria-label="恢复"
                  @click="doResume"
                  :disabled="!detail.capabilities.can_resume"
                />
                <el-button
                  class="ops-icon-btn"
                  size="small"
                  type="danger"
                  plain
                  circle
                  :icon="CloseBold"
                  title="取消"
                  aria-label="取消"
                  @click="doCancel"
                  :disabled="!detail.capabilities.can_cancel"
                />
              </el-space>
              <el-button class="ops-edit-btn" size="small" @click="goEditBasic" :disabled="!detail.editable">
                编辑基本信息
              </el-button>
            </div>
          </div>
        </template>
      </el-skeleton>
    </el-card>

    <!-- Runtime snapshot from strategy_runs, shown right after base strategy info. -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">strategy_runs</span>
          <span class="card-tools">运行时记录</span>
        </div>
      </template>
      <template v-if="detail?.strategy_run">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="first_evaluated_at">
            {{ formatIsoDateTime(detail.strategy_run.first_evaluated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="evaluated_at">
            {{ formatIsoDateTime(detail.strategy_run.evaluated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="suggested_next_monitor_at">
            {{ formatIsoDateTime(detail.strategy_run.suggested_next_monitor_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="updated_at">
            {{ formatIsoDateTime(detail.strategy_run.updated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="check_count">
            {{ detail.strategy_run.check_count }}
          </el-descriptions-item>
          <el-descriptions-item label="condition_met">
            {{ detail.strategy_run.condition_met ? 'true' : 'false' }}
          </el-descriptions-item>
          <el-descriptions-item label="last_outcome">
            {{ detail.strategy_run.last_outcome }}
          </el-descriptions-item>
          <el-descriptions-item label="decision_reason">
            {{ detail.strategy_run.decision_reason }}
          </el-descriptions-item>
        </el-descriptions>
        <!-- Keep nested monitoring-end map visible for debugging condition data windows. -->
        <details class="strategy-run-raw-details">
          <summary>查看 last_monitoring_data_end_at</summary>
          <pre class="json-box raw-json-box">{{ pretty(detail.strategy_run.last_monitoring_data_end_at) }}</pre>
        </details>
      </template>
      <div v-else class="strategy-run-empty">
        暂无 strategy_runs 记录（运行后自动生成）
      </div>
    </el-card>

    <el-card id="conditions-section" shadow="never">
      <template #header>
        <div class="card-header-row conditions-header-row">
          <span class="card-title">触发条件</span>
          <span class="card-tools conditions-status">条件组状态：{{ detail?.trigger_group_status || 'NOT_CONFIGURED' }}</span>
          <el-button class="conditions-edit-btn" size="small" @click="goEditConditions" :disabled="!detail?.editable">
            {{ (detail?.conditions_json?.length || 0) > 0 ? '编辑' : '设置触发条件' }}
          </el-button>
        </div>
      </template>
      <div class="conditions-content">
        <template v-if="hasConditions">
          <div class="condition-view-list">
            <template v-for="(item, idx) in conditionViewItems" :key="`${item.id}-${idx}`">
              <div class="condition-view-card">
                <div class="condition-top-row">
                  <div class="condition-expression">
                    <span class="condition-label">规则表达式：</span>
                    <code>{{ item.expression }}</code>
                  </div>
                  <span class="condition-state" :class="item.stateClass">{{ item.stateText }}</span>
                </div>
                <div class="condition-nl">
                  <span class="condition-label">自然语言：</span>
                  {{ item.naturalLanguage }}
                </div>
                <div class="condition-meta">
                  <span v-for="(meta, metaIdx) in item.meta" :key="`${item.id}-meta-${metaIdx}`" class="meta-pill">
                    {{ meta }}
                  </span>
                </div>
              </div>
              <div v-if="idx < conditionViewItems.length - 1" class="condition-logic-separator">
                {{ detail?.condition_logic || 'AND' }}
              </div>
            </template>
          </div>

          <details class="conditions-raw-details">
            <summary>查看原始条件 JSON</summary>
            <pre class="json-box raw-json-box">{{ pretty(detail?.conditions_json ?? []) }}</pre>
            <pre class="json-box raw-json-box runtime-box">{{ pretty(detail?.conditions_runtime ?? []) }}</pre>
          </details>
        </template>
        <div v-else class="conditions-empty">尚未设置触发条件</div>
      </div>
    </el-card>

    <el-card id="actions-section" shadow="never">
      <template #header>
        <div class="card-header-row actions-header-row">
          <span class="card-title">后续动作</span>
          <span class="card-tools actions-status">{{ followupStatusText }}</span>
          <el-button class="actions-edit-btn" size="small" @click="goEditActions" :disabled="!detail?.editable">
            {{ hasFollowupConfigured ? '编辑' : '设置后续动作' }}
          </el-button>
        </div>
      </template>
      <div class="actions-content">
        <template v-if="hasFollowupConfigured">
          <div class="actions-grid">
            <div class="action-sub-card">
              <div class="action-sub-header">
                <span class="action-sub-title">后续策略</span>
                <el-tag
                  size="small"
                  :type="statusTagType(detail?.next_strategy?.status || 'NOT_SET')"
                  effect="dark"
                >
                  {{ detail?.next_strategy?.status || 'NOT_SET' }}
                </el-tag>
              </div>
              <template v-if="detail?.next_strategy">
                <div class="action-field">
                  <span class="action-field-label">策略ID</span>
                  <el-link type="primary" @click.prevent="openNextStrategy">{{ detail.next_strategy.id }}</el-link>
                </div>
                <div class="action-field">
                  <span class="action-field-label">说明</span>
                  <span>{{ detail.next_strategy.description || '-' }}</span>
                </div>
              </template>
              <div v-else class="actions-empty-sub">未配置下游策略</div>
            </div>

            <div class="action-sub-card">
              <div class="action-sub-header">
                <span class="action-sub-title">交易指令</span>
                <el-tag
                  size="small"
                  :type="statusTagType(detail?.trade_action_runtime?.trade_status || 'NOT_SET')"
                  effect="dark"
                >
                  {{ detail?.trade_action_runtime?.trade_status || 'NOT_SET' }}
                </el-tag>
              </div>
              <template v-if="hasTradeAction">
                <div class="action-summary">{{ tradeActionSummary }}</div>
                <div class="trade-readonly-layout">
                  <div class="trade-row trade-row-2">
                    <div class="trade-inline-field">
                      <span class="trade-field-label">动作类型</span>
                      <span class="trade-field-value">{{ actionTypeLabel(tradeActionType) }}</span>
                    </div>
                    <div class="trade-inline-field">
                      <span class="trade-field-label">交易方向</span>
                      <span class="trade-field-value">{{ sideLabel(toUpper(tradeAction.side)) }}</span>
                    </div>
                  </div>

                  <div class="trade-row trade-row-2">
                    <div class="trade-inline-field">
                      <span class="trade-field-label">产品代码</span>
                      <span class="trade-field-value">{{ formatValue(tradeAction.symbol) }}</span>
                    </div>
                    <div class="trade-inline-field">
                      <span class="trade-field-label">下单数量</span>
                      <span class="trade-field-value">
                        {{ formatValue(tradeAction.quantity) }} {{ quantityUnitByActionType(tradeActionType) }}
                      </span>
                    </div>
                  </div>

                  <template v-if="showNormalTradeFields">
                    <div v-if="isFutPositionAction" class="trade-row trade-row-2">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">期货合约（可选）</span>
                        <span class="trade-field-value">{{ formatValue(tradeAction.contract, formatValue(tradeAction.symbol)) }}</span>
                      </div>
                      <div class="trade-inline-field">
                        <span class="trade-field-label">开平类型</span>
                        <span class="trade-field-value">{{ positionEffectLabel(toUpper(tradeAction.position_effect)) }}</span>
                      </div>
                    </div>

                    <div class="trade-row trade-row-2">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">订单类型</span>
                        <span class="trade-field-value">{{ orderTypeLabel(toUpper(tradeAction.order_type)) }}</span>
                      </div>
                      <div class="trade-inline-field">
                        <span class="trade-field-label">限价（LMT）</span>
                        <span class="trade-field-value">{{ limitPriceValue(tradeAction.order_type, tradeAction.limit_price) }}</span>
                      </div>
                    </div>
                  </template>

                  <template v-if="isFutRollAction">
                    <div class="trade-row trade-row-2">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">待平合约（近月）</span>
                        <span class="trade-field-value">{{ formatValue(tradeAction.close_contract) }}</span>
                      </div>
                      <div class="trade-inline-field">
                        <span class="trade-field-label">目标合约（远月）</span>
                        <span class="trade-field-value">{{ formatValue(tradeAction.open_contract) }}</span>
                      </div>
                    </div>

                    <div class="trade-row trade-row-2">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">平仓订单类型</span>
                        <span class="trade-field-value">{{ orderTypeLabel(toUpper(tradeAction.close_order_type)) }}</span>
                      </div>
                      <div class="trade-inline-field">
                        <span class="trade-field-label">开仓订单类型</span>
                        <span class="trade-field-value">{{ orderTypeLabel(toUpper(tradeAction.open_order_type)) }}</span>
                      </div>
                    </div>

                    <div class="trade-row trade-row-2">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">平仓限价</span>
                        <span class="trade-field-value">
                          {{ limitPriceValue(tradeAction.close_order_type, tradeAction.close_limit_price) }}
                        </span>
                      </div>
                      <div class="trade-inline-field">
                        <span class="trade-field-label">开仓限价</span>
                        <span class="trade-field-value">
                          {{ limitPriceValue(tradeAction.open_order_type, tradeAction.open_limit_price) }}
                        </span>
                      </div>
                    </div>

                    <div class="trade-row trade-row-1">
                      <div class="trade-inline-field">
                        <span class="trade-field-label">腿间最大滑点（USD）</span>
                        <span class="trade-field-value">{{ formatValue(tradeAction.max_leg_slippage_usd, '未设置') }}</span>
                      </div>
                    </div>
                  </template>

                  <div class="trade-row trade-row-2">
                    <div class="trade-inline-field">
                      <span class="trade-field-label">隔夜交易</span>
                      <span class="trade-field-value">{{ boolLabel(tradeAction.allow_overnight) }}</span>
                    </div>
                    <div class="trade-inline-field">
                      <span class="trade-field-label">到期撤单</span>
                      <span class="trade-field-value">{{ boolLabel(tradeAction.cancel_on_expiry) }}</span>
                    </div>
                  </div>
                </div>

                <div v-if="tradeRuntimeFields.length > 0" class="runtime-list">
                  <div v-for="(field, fieldIdx) in tradeRuntimeFields" :key="`runtime-field-${fieldIdx}`" class="runtime-inline-field">
                    <span class="action-field-label">{{ field.label }}</span>
                    <span>{{ field.value }}</span>
                  </div>
                </div>
              </template>
              <div v-else class="actions-empty-sub">未配置交易动作（仅激活下游策略）</div>
            </div>
          </div>

          <details class="actions-raw-details">
            <summary>查看原始后续动作 JSON</summary>
            <pre class="json-box raw-json-box">{{ pretty(detail?.trade_action_json ?? null) }}</pre>
            <pre class="json-box raw-json-box runtime-box">{{ pretty(detail?.next_strategy ?? null) }}</pre>
          </details>
        </template>
        <div v-else class="actions-empty">尚未设置后续动作</div>
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

.detail-header-row {
  align-items: flex-start;
}

.detail-header-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.detail-title-row {
  display: inline-flex;
  align-items: center;
  gap: 0;
  min-width: 0;
}

.detail-anchor-group {
  margin-left: 24px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.detail-anchor-btn {
  padding: 0 4px;
  height: 20px;
  font-size: 12px;
  border-radius: 6px;
  border: none;
  background: var(--el-fill-color-dark);
  color: var(--el-text-color-regular);
  transition: background-color 0.18s ease, color 0.18s ease;
}

.detail-anchor-btn:hover {
  background: var(--el-fill-color-darker);
  color: var(--el-color-primary);
}

.detail-upstream-btn {
  min-width: 72px;
}

.detail-description {
  display: block;
  width: 100%;
  margin-top: 4px;
  color: #9fb0c3;
  font-size: 13px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: text;
}

.detail-description-wrap {
  width: 100%;
  margin-top: 4px;
  min-height: 24px;
}

.detail-description-input {
  width: 100%;
}

.json-box {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
}

.mb-12 {
  margin-bottom: 12px;
}

.ops-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.ops-left {
  flex: 1;
  min-width: 0;
}

.ops-edit-btn {
  margin-left: auto;
}

.ops-status {
  color: #9fb0c3;
  font-size: 13px;
}

.ops-icon-btn.el-button.is-circle {
  width: 48px;
  height: 48px;
  padding: 0;
}

.ops-icon-btn :deep(.el-icon) {
  font-size: 24px;
}

.detail-top {
  display: flex;
  flex-direction: column;
}

.card-tools {
  color: #9fb0c3;
  font-size: 12px;
}

.runtime-box {
  margin-top: 10px;
}

.conditions-header-row {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 12px;
}

.conditions-status {
  justify-self: center;
  text-align: center;
  white-space: nowrap;
}

.conditions-edit-btn {
  justify-self: end;
}

.actions-header-row {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 12px;
}

.actions-status {
  justify-self: center;
  text-align: center;
  white-space: nowrap;
}

.actions-edit-btn {
  justify-self: end;
}

.conditions-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.condition-view-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.condition-view-card {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 12px;
  background: var(--el-fill-color-light);
}

.condition-top-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.condition-expression {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.condition-expression code {
  white-space: pre-wrap;
  word-break: break-word;
}

.condition-label {
  color: var(--el-text-color-secondary);
}

.condition-nl {
  margin-top: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.condition-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.meta-pill {
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--el-border-color);
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-blank);
}

.condition-state {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid transparent;
  white-space: nowrap;
}

.condition-state.is-true {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success-light-5);
}

.condition-state.is-false {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border-color: var(--el-color-danger-light-5);
}

.condition-state.is-waiting {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  border-color: var(--el-color-warning-light-5);
}

.condition-state.is-not-evaluated {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color);
  border-color: var(--el-border-color);
}

.condition-logic-separator {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.conditions-raw-details summary {
  cursor: pointer;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.raw-json-box {
  margin-top: 8px;
}

.strategy-run-empty {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 8px 0;
}

.strategy-run-raw-details {
  margin-top: 10px;
}

.strategy-run-raw-details summary {
  cursor: pointer;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.conditions-empty {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 8px 0;
}

.actions-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.actions-grid {
  display: grid;
  grid-template-columns: minmax(240px, 0.95fr) minmax(320px, 1.35fr);
  gap: 12px;
}

.action-sub-card {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 12px;
  background: var(--el-fill-color-light);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.action-sub-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.action-sub-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.action-summary {
  font-size: 13px;
  color: var(--el-text-color-primary);
  font-weight: 600;
}

.trade-readonly-layout {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.trade-row {
  display: grid;
  gap: 10px;
}

.trade-row-1 {
  grid-template-columns: 1fr;
}

.trade-row-2 {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.trade-inline-field {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}

.trade-field-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.trade-field-value {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
  color: var(--el-text-color-regular);
  font-size: 12px;
}

.action-field-label {
  color: var(--el-text-color-secondary);
}

.runtime-list {
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color);
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 10px;
}

.runtime-inline-field {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.action-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.actions-empty-sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.actions-empty {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 8px 0;
}

.actions-raw-details summary {
  cursor: pointer;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

@media (max-width: 900px) {
  .actions-grid {
    grid-template-columns: 1fr;
  }

  .trade-row-2,
  .runtime-inline-field,
  .runtime-list {
    grid-template-columns: 1fr;
  }

  .trade-inline-field {
    grid-template-columns: 1fr;
    align-items: flex-start;
  }
}
</style>
