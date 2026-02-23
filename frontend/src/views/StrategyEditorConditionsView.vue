<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchConditionRules, fetchStrategyDetail, putStrategyConditions } from '../api/services'
import type { ConditionRulesResponse } from '../api/types'

const route = useRoute()
const router = useRouter()

const strategyId = computed(() => String(route.params.id || ''))
const MAX_CONDITIONS = 2

const CONDITION_TYPES = ['SINGLE_PRODUCT', 'PAIR_PRODUCTS'] as const
const EVALUATION_WINDOWS = ['1m', '5m', '30m', '1h', '2h', '4h', '1d', '2d'] as const

type ConditionType = (typeof CONDITION_TYPES)[number]
type EvaluationWindow = (typeof EVALUATION_WINDOWS)[number]
type Metric = 'PRICE' | 'DRAWDOWN_PCT' | 'RALLY_PCT' | 'VOLUME_RATIO' | 'AMOUNT_RATIO' | 'SPREAD'
type ValueType = 'USD' | 'RATIO'
type TriggerMode =
  | 'LEVEL_INSTANT'
  | 'LEVEL_CONFIRM'
  | 'CROSS_UP_INSTANT'
  | 'CROSS_UP_CONFIRM'
  | 'CROSS_DOWN_INSTANT'
  | 'CROSS_DOWN_CONFIRM'
type Operator = '>=' | '<='
type TriggerRuleId = `${string}|>=` | `${string}|<=`

type TriggerRuleDef = {
  id: TriggerRuleId
  triggerMode: TriggerMode
  operator: Operator
  label: string
}

type MetricOption = {
  metric: Metric
  label: string
}

const METRIC_OPTIONS_BY_TYPE: Record<ConditionType, MetricOption[]> = {
  SINGLE_PRODUCT: [
    { metric: 'PRICE', label: '价格（PRICE）' },
    {
      metric: 'DRAWDOWN_PCT',
      label: '回撤比例（DRAWDOWN_PCT，基准=激活后最高价）',
    },
    {
      metric: 'RALLY_PCT',
      label: '上涨比例（RALLY_PCT，基准=激活后最低价）',
    },
  ],
  PAIR_PRODUCTS: [
    { metric: 'VOLUME_RATIO', label: '成交量比值（VOLUME_RATIO）' },
    { metric: 'AMOUNT_RATIO', label: '成交额比值（AMOUNT_RATIO）' },
    { metric: 'SPREAD', label: '价差（SPREAD）' },
  ],
}

const DEFAULT_ALLOWED_WINDOWS: Record<Metric, EvaluationWindow[]> = {
  PRICE: ['1m', '5m', '30m', '1h'],
  DRAWDOWN_PCT: ['1m', '5m', '30m', '1h'],
  RALLY_PCT: ['1m', '5m', '30m', '1h'],
  SPREAD: ['1m', '5m', '30m', '1h'],
  VOLUME_RATIO: ['1h', '2h', '4h', '1d', '2d'],
  AMOUNT_RATIO: ['1h', '2h', '4h', '1d', '2d'],
}

const DEFAULT_ALLOWED_RULES: Record<Metric, Array<{ trigger_mode: TriggerMode; operator: Operator }>> = {
  PRICE: [
    { trigger_mode: 'LEVEL_INSTANT', operator: '>=' },
    { trigger_mode: 'LEVEL_INSTANT', operator: '<=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '<=' },
    { trigger_mode: 'CROSS_UP_INSTANT', operator: '>=' },
    { trigger_mode: 'CROSS_UP_CONFIRM', operator: '>=' },
    { trigger_mode: 'CROSS_DOWN_INSTANT', operator: '<=' },
    { trigger_mode: 'CROSS_DOWN_CONFIRM', operator: '<=' },
  ],
  DRAWDOWN_PCT: [
    { trigger_mode: 'LEVEL_INSTANT', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
  ],
  RALLY_PCT: [
    { trigger_mode: 'LEVEL_INSTANT', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
  ],
  VOLUME_RATIO: [
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '<=' },
  ],
  AMOUNT_RATIO: [
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '<=' },
  ],
  SPREAD: [
    { trigger_mode: 'LEVEL_CONFIRM', operator: '>=' },
    { trigger_mode: 'LEVEL_CONFIRM', operator: '<=' },
    { trigger_mode: 'CROSS_UP_CONFIRM', operator: '>=' },
    { trigger_mode: 'CROSS_DOWN_CONFIRM', operator: '<=' },
  ],
}

const DEFAULT_TRIGGER_MODE_WINDOWS: Record<TriggerMode, EvaluationWindow[]> = {
  LEVEL_INSTANT: ['1m', '5m', '30m', '1h'],
  CROSS_UP_INSTANT: ['1m', '5m', '30m', '1h'],
  CROSS_DOWN_INSTANT: ['1m', '5m', '30m', '1h'],
  LEVEL_CONFIRM: ['5m', '30m', '1h', '2h', '4h', '1d', '2d'],
  CROSS_UP_CONFIRM: ['5m', '30m', '1h', '2h', '4h', '1d', '2d'],
  CROSS_DOWN_CONFIRM: ['5m', '30m', '1h', '2h', '4h', '1d', '2d'],
}

const conditionRules = ref<ConditionRulesResponse | null>(null)

const METRIC_VALUE_TYPES: Record<Metric, ValueType> = {
  PRICE: 'USD',
  SPREAD: 'USD',
  DRAWDOWN_PCT: 'RATIO',
  RALLY_PCT: 'RATIO',
  VOLUME_RATIO: 'RATIO',
  AMOUNT_RATIO: 'RATIO',
}

type ConditionRow = {
  conditionId: string
  conditionType: ConditionType
  product: string
  productB: string
  evaluationWindow: EvaluationWindow
  metric: Metric
  triggerRuleId: TriggerRuleId
  valueText: string
}

const loading = ref(false)
const saving = ref(false)
const error = ref('')

const form = reactive({
  condition_logic: 'AND' as 'AND' | 'OR',
})

const conditionRows = ref<ConditionRow[]>([])
const nextConditionId = ref(1)
const symbolOptions = ref<Array<{ code: string; label: string }>>([])
const symbolCodeSet = computed(() => new Set(symbolOptions.value.map((item) => item.code)))
const canAddCondition = computed(() => conditionRows.value.length < MAX_CONDITIONS)

function asString(value: unknown) {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function asNumber(value: unknown) {
  if (typeof value === 'number') return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return NaN
}

function getMetricOptions(conditionType: ConditionType) {
  return METRIC_OPTIONS_BY_TYPE[conditionType]
}

function buildTriggerRuleId(triggerMode: TriggerMode, operator: Operator): TriggerRuleId {
  return `${triggerMode}|${operator}`
}

function buildTriggerRuleLabel(triggerMode: TriggerMode, operator: Operator) {
  const opText = operator === '>=' ? '高于' : '低于'
  if (triggerMode === 'LEVEL_INSTANT') return `一旦达到/${opText}阈值（${triggerMode} + ${operator}）`
  if (triggerMode === 'LEVEL_CONFIRM') return `确认达到/${opText}阈值（${triggerMode} + ${operator}）`
  if (triggerMode === 'CROSS_UP_INSTANT') return `一旦上穿阈值（${triggerMode} + ${operator}）`
  if (triggerMode === 'CROSS_UP_CONFIRM') return `确认上穿阈值（${triggerMode} + ${operator}）`
  if (triggerMode === 'CROSS_DOWN_INSTANT') return `一旦下穿阈值（${triggerMode} + ${operator}）`
  return `确认下穿阈值（${triggerMode} + ${operator}）`
}

function asTriggerMode(value: string): TriggerMode | null {
  if (
    value === 'LEVEL_INSTANT' ||
    value === 'LEVEL_CONFIRM' ||
    value === 'CROSS_UP_INSTANT' ||
    value === 'CROSS_UP_CONFIRM' ||
    value === 'CROSS_DOWN_INSTANT' ||
    value === 'CROSS_DOWN_CONFIRM'
  ) {
    return value
  }
  return null
}

function normalizeRulePairList(metric: Metric) {
  const fromApi = conditionRules.value?.metric_trigger_operator_rules.allowed_rules?.[metric]
  const fallback = DEFAULT_ALLOWED_RULES[metric]
  const source = Array.isArray(fromApi) && fromApi.length > 0 ? fromApi : fallback
  const out: Array<{ trigger_mode: TriggerMode; operator: Operator }> = []
  for (const item of source) {
    const triggerMode = asTriggerMode(String(item.trigger_mode || ''))
    const operator = item.operator
    if (!triggerMode) continue
    if (operator !== '>=' && operator !== '<=') continue
    out.push({ trigger_mode: triggerMode, operator })
  }
  if (out.length === 0) return fallback
  return out
}

function getAllowedRules(metric: Metric): TriggerRuleDef[] {
  return normalizeRulePairList(metric).map((item) => ({
    id: buildTriggerRuleId(item.trigger_mode, item.operator),
    triggerMode: item.trigger_mode,
    operator: item.operator,
    label: buildTriggerRuleLabel(item.trigger_mode, item.operator),
  }))
}

function getTriggerModeWindows(triggerMode: TriggerMode): EvaluationWindow[] {
  const windowsFromApi = conditionRules.value?.trigger_mode_windows?.[triggerMode]
  if (windowsFromApi && typeof windowsFromApi === 'object') {
    const parsed = Object.keys(windowsFromApi).filter((item): item is EvaluationWindow =>
      (EVALUATION_WINDOWS as readonly string[]).includes(item),
    )
    if (parsed.length > 0) return parsed
  }
  return DEFAULT_TRIGGER_MODE_WINDOWS[triggerMode]
}

function getMetricAllowedWindows(metric: Metric): EvaluationWindow[] {
  const windowsFromApi = conditionRules.value?.metric_trigger_operator_rules.allowed_windows?.[metric]
  if (Array.isArray(windowsFromApi) && windowsFromApi.length > 0) {
    const parsed = windowsFromApi.filter((item): item is EvaluationWindow =>
      (EVALUATION_WINDOWS as readonly string[]).includes(item),
    )
    if (parsed.length > 0) return parsed
  }
  return DEFAULT_ALLOWED_WINDOWS[metric]
}

function getDefaultMetric(conditionType: ConditionType) {
  return getMetricOptions(conditionType)[0]!.metric
}

function getDefaultRuleId(metric: Metric) {
  return getAllowedRules(metric)[0]!.id
}

function getValueType(metric: Metric): ValueType {
  return METRIC_VALUE_TYPES[metric]
}

function getEvaluationWindowOptions(metric: Metric, triggerRuleId?: TriggerRuleId): EvaluationWindow[] {
  const metricWindows = getMetricAllowedWindows(metric)
  if (!triggerRuleId) return metricWindows
  const triggerMode = asTriggerMode(triggerRuleId.split('|')[0] || '')
  if (!triggerMode) return metricWindows
  const triggerModeWindows = getTriggerModeWindows(triggerMode)
  const set = new Set(triggerModeWindows)
  const merged = metricWindows.filter((item) => set.has(item))
  return merged.length > 0 ? merged : metricWindows
}

function getDefaultEvaluationWindow(metric: Metric, triggerRuleId?: TriggerRuleId): EvaluationWindow {
  return getEvaluationWindowOptions(metric, triggerRuleId)[0]!
}

function getValuePlaceholder(metric: Metric) {
  return getValueType(metric) === 'RATIO' ? '例如 10' : '例如 88.5'
}

function getValueUnit(metric: Metric) {
  return getValueType(metric) === 'RATIO' ? '%' : '$'
}

function deriveTriggerRuleId(
  metric: Metric,
  triggerModeRaw: string,
  operatorRaw: string,
  fallback: TriggerRuleId,
): TriggerRuleId {
  const triggerMode = asTriggerMode(triggerModeRaw)
  if (!triggerMode) return fallback
  if (operatorRaw !== '>=' && operatorRaw !== '<=') return fallback
  const candidate = buildTriggerRuleId(triggerMode, operatorRaw)
  return getAllowedRules(metric).some((item) => item.id === candidate) ? candidate : fallback
}

function syncNextConditionId() {
  const maxId = conditionRows.value.reduce((max, row) => {
    const matched = row.conditionId.match(/^c(\d+)$/i)
    if (!matched) return max
    return Math.max(max, Number(matched[1]))
  }, 0)
  nextConditionId.value = maxId + 1
}

function createConditionId() {
  const id = `c${nextConditionId.value}`
  nextConditionId.value += 1
  return id
}

function normalizeRow(row: ConditionRow) {
  const metricOptions = getMetricOptions(row.conditionType)
  if (!metricOptions.some((item) => item.metric === row.metric)) {
    row.metric = metricOptions[0]!.metric
  }

  const allowedRules = getAllowedRules(row.metric)
  if (!allowedRules.some((item) => item.id === row.triggerRuleId)) {
    row.triggerRuleId = allowedRules[0]!.id
  }

  const allowedWindows = getEvaluationWindowOptions(row.metric, row.triggerRuleId)
  if (!allowedWindows.includes(row.evaluationWindow)) {
    row.evaluationWindow = getDefaultEvaluationWindow(row.metric, row.triggerRuleId)
  }
}

function addConditionRow(initial?: Partial<ConditionRow>) {
  if (conditionRows.value.length >= MAX_CONDITIONS) {
    ElMessage.warning(`最多只能设置 ${MAX_CONDITIONS} 个条件`)
    return
  }

  const conditionType = initial?.conditionType || 'SINGLE_PRODUCT'
  const metric = initial?.metric && getMetricOptions(conditionType).some((item) => item.metric === initial.metric)
    ? initial.metric
    : getDefaultMetric(conditionType)

  const row: ConditionRow = {
    conditionId: initial?.conditionId || createConditionId(),
    conditionType,
    product: initial?.product || '',
    productB: initial?.productB || '',
    evaluationWindow: initial?.evaluationWindow || getDefaultEvaluationWindow(metric, initial?.triggerRuleId),
    metric,
    triggerRuleId: initial?.triggerRuleId && getAllowedRules(metric).some((item) => item.id === initial.triggerRuleId)
      ? initial.triggerRuleId
      : getDefaultRuleId(metric),
    valueText: initial?.valueText || '',
  }

  normalizeRow(row)
  conditionRows.value.push(row)
  syncNextConditionId()
}

function removeConditionRow(index: number) {
  conditionRows.value.splice(index, 1)
  if (conditionRows.value.length === 0) addConditionRow()
}

function onConditionTypeChange(row: ConditionRow) {
  normalizeRow(row)
}

function onMetricChange(row: ConditionRow) {
  row.triggerRuleId = getDefaultRuleId(row.metric)
  row.valueText = ''
  normalizeRow(row)
}

function onTriggerRuleChange(row: ConditionRow) {
  normalizeRow(row)
}

function mapValueFromBackend(metric: Metric, rawValue: unknown) {
  const numeric = asNumber(rawValue)
  if (!Number.isFinite(numeric)) return asString(rawValue)

  if (getValueType(metric) === 'RATIO') {
    const percent = numeric * 100
    return String(Number(percent.toFixed(6)))
  }

  return String(numeric)
}

function parseRowValue(metric: Metric, valueText: string) {
  const numeric = Number(valueText)
  if (!Number.isFinite(numeric)) {
    throw new Error(`条件 ${metric} 的 value 必须是数字`)
  }

  if (getValueType(metric) === 'RATIO') {
    return numeric / 100
  }

  return numeric
}

function buildConditionPayload(row: ConditionRow) {
  const conditionId = row.conditionId.trim()
  if (!conditionId) throw new Error('condition_id 不能为空')

  const ruleDef = getAllowedRules(row.metric).find((item) => item.id === row.triggerRuleId)
  if (!ruleDef) throw new Error(`条件 ${conditionId} 的触发判定无效`)
  const value = parseRowValue(row.metric, row.valueText.trim())

  const payload: Record<string, unknown> = {
    condition_id: conditionId,
    condition_type: row.conditionType,
    metric: row.metric,
    trigger_mode: ruleDef.triggerMode,
    evaluation_window: row.evaluationWindow,
    window_price_basis: 'CLOSE',
    operator: ruleDef.operator,
    value,
  }

  if (row.conditionType === 'SINGLE_PRODUCT') {
    const product = row.product.trim().toUpperCase()
    if (!product) throw new Error(`条件 ${conditionId} 缺少 product`)
    if (!symbolCodeSet.value.has(product)) throw new Error(`条件 ${conditionId} 的 product 不在策略 symbols 中`)
    payload.product = product
  } else {
    const product = row.product.trim().toUpperCase()
    const productB = row.productB.trim().toUpperCase()
    if (!product || !productB) throw new Error(`条件 ${conditionId} 缺少 product 或 product_b`)
    if (!symbolCodeSet.value.has(product)) throw new Error(`条件 ${conditionId} 的 product 不在策略 symbols 中`)
    if (!symbolCodeSet.value.has(productB)) throw new Error(`条件 ${conditionId} 的 product_b 不在策略 symbols 中`)
    payload.product = product
    payload.product_b = productB
  }

  return payload
}

async function loadDetail() {
  if (!strategyId.value) return
  loading.value = true
  error.value = ''
  try {
    const detail = await fetchStrategyDetail(strategyId.value)
    const symbolTypeMap = new Map<string, Set<string>>()
    for (const item of detail.symbols) {
      const code = asString(item.code).trim().toUpperCase()
      const tradeType = asString(item.trade_type).trim().toLowerCase()
      if (!code) continue
      if (!symbolTypeMap.has(code)) symbolTypeMap.set(code, new Set<string>())
      if (tradeType) symbolTypeMap.get(code)?.add(tradeType)
    }
    symbolOptions.value = Array.from(symbolTypeMap.entries()).map(([code, tradeTypes]) => {
      const suffix = tradeTypes.size > 0 ? `（${Array.from(tradeTypes).join(', ')}）` : ''
      return { code, label: `${code}${suffix}` }
    })
    symbolOptions.value.sort((a, b) => a.code.localeCompare(b.code))
    form.condition_logic = detail.condition_logic
    conditionRows.value = []
    nextConditionId.value = 1

    const backendConditions = detail.conditions_json ?? []
    if (backendConditions.length > MAX_CONDITIONS) {
      ElMessage.warning(`当前策略条件数超过 ${MAX_CONDITIONS} 条，编辑页仅展示前 ${MAX_CONDITIONS} 条`)
    }
    for (const rawCondition of backendConditions.slice(0, MAX_CONDITIONS)) {
      const condition = rawCondition as Record<string, unknown>
      const conditionTypeRaw = asString(condition.condition_type)
      const conditionType = (CONDITION_TYPES as readonly string[]).includes(conditionTypeRaw)
        ? (conditionTypeRaw as ConditionType)
        : 'SINGLE_PRODUCT'

      const metricRaw = asString(condition.metric) as Metric
      const metric = getMetricOptions(conditionType).some((item) => item.metric === metricRaw)
        ? metricRaw
        : getDefaultMetric(conditionType)

      const fallbackRuleId = getDefaultRuleId(metric)
      const triggerRuleId = deriveTriggerRuleId(
        metric,
        asString(condition.trigger_mode),
        asString(condition.operator),
        fallbackRuleId,
      )

      addConditionRow({
        conditionId: asString(condition.condition_id) || createConditionId(),
        conditionType,
        product: asString(condition.product) || asString(condition.product_a),
        productB: asString(condition.product_b),
        evaluationWindow: (EVALUATION_WINDOWS as readonly string[]).includes(asString(condition.evaluation_window))
          ? (asString(condition.evaluation_window) as EvaluationWindow)
          : getDefaultEvaluationWindow(metric, triggerRuleId),
        metric,
        triggerRuleId,
        valueText: mapValueFromBackend(metric, condition.value),
      })
    }

    if (conditionRows.value.length === 0) addConditionRow()
    syncNextConditionId()
  } catch (err) {
    error.value = `加载策略失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

async function loadConditionRules() {
  try {
    conditionRules.value = await fetchConditionRules()
  } catch {
    conditionRules.value = null
    ElMessage.warning('条件规则配置加载失败，已使用前端缺省规则')
  }
}

async function saveConditions() {
  if (!strategyId.value) return
  saving.value = true
  error.value = ''
  try {
    if (conditionRows.value.length === 0) throw new Error('至少需要一个条件')
    if (conditionRows.value.length > MAX_CONDITIONS) {
      throw new Error(`最多只能保存 ${MAX_CONDITIONS} 个条件`)
    }

    const conditions = conditionRows.value.map((row) => buildConditionPayload(row))

    const detail = await putStrategyConditions(strategyId.value, {
      condition_logic: form.condition_logic,
      conditions,
    })

    ElMessage.success(`已保存触发条件（${detail.id}）`)
    router.push(`/strategies/${strategyId.value}`)
  } catch (err) {
    error.value = `保存失败：${String(err)}`
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadConditionRules()
  await loadDetail()
})
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">触发条件编辑：{{ strategyId }}</span>
          <el-button size="small" @click="router.push(`/strategies/${strategyId}`)">返回策略详情</el-button>
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

      <div class="editor-note">
        编辑规则：第一行固定 <code>condition_id / type(单选) / product / evaluation_window</code>；
        第二行设置 <code>指标（含基准） / 触发判定 / value</code>；
        <code>condition_id</code> 自动生成；价格类窗口支持分钟级，成交量/成交额比值支持小时/天级；
        <code>window_price_basis</code> 固定为 <code>CLOSE</code>。
      </div>

      <div class="conditions-toolbar">
        <strong>conditions</strong>
        <div class="conditions-toolbar-actions">
          <el-button size="small" :disabled="!canAddCondition" @click="addConditionRow">+ 条件</el-button>
        </div>
      </div>

      <div class="conditions-stack" v-loading="loading">
        <template v-for="(row, idx) in conditionRows" :key="`${row.conditionId}-${idx}`">
          <div class="condition-row">
            <div class="condition-fields">
            <div class="condition-field">
              <label class="field-label">条件编号</label>
              <div class="id-row-content">
                <div class="auto-id-text">{{ row.conditionId }}</div>
                <el-button class="remove-icon-btn" type="danger" plain circle @click="removeConditionRow(idx)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>

            <div class="condition-field">
              <label class="field-label">条件类型 / product</label>
              <div class="type-product-row">
                <el-radio-group v-model="row.conditionType" size="small" @change="onConditionTypeChange(row)">
                  <el-radio-button label="SINGLE_PRODUCT">单产品</el-radio-button>
                  <el-radio-button label="PAIR_PRODUCTS">双产品</el-radio-button>
                </el-radio-group>
                <div v-if="row.conditionType === 'SINGLE_PRODUCT'" class="pair-products preserve-pair-slot">
                  <el-select
                    v-model="row.product"
                    size="small"
                    class="full-width"
                    :disabled="symbolOptions.length === 0"
                    :empty-values="[]"
                    no-data-text="策略 symbols 为空"
                    placeholder="选择产品"
                  >
                    <el-option
                      v-for="symbol in symbolOptions"
                      :key="`single-${row.conditionId}-${symbol.code}`"
                      :label="symbol.label"
                      :value="symbol.code"
                    />
                  </el-select>
                  <el-select
                    class="product-slot-placeholder-select"
                    aria-hidden="true"
                    tabindex="-1"
                    size="small"
                    disabled
                  />
                </div>
                <div v-else class="pair-products">
                  <el-select
                    v-model="row.product"
                    size="small"
                    class="full-width"
                    :disabled="symbolOptions.length === 0"
                    :empty-values="[]"
                    no-data-text="策略 symbols 为空"
                    placeholder="选择 product"
                  >
                    <el-option
                      v-for="symbol in symbolOptions"
                      :key="`pair-a-${row.conditionId}-${symbol.code}`"
                      :label="symbol.label"
                      :value="symbol.code"
                    />
                  </el-select>
                  <el-select
                    v-model="row.productB"
                    size="small"
                    class="full-width"
                    :disabled="symbolOptions.length === 0"
                    :empty-values="[]"
                    no-data-text="策略 symbols 为空"
                    placeholder="选择 product_b"
                  >
                    <el-option
                      v-for="symbol in symbolOptions"
                      :key="`pair-b-${row.conditionId}-${symbol.code}`"
                      :label="symbol.label"
                      :value="symbol.code"
                    />
                  </el-select>
                </div>
              </div>
            </div>

            <div class="condition-field">
              <label class="field-label">判断指标</label>
              <div class="metric-window-row">
                <el-select v-model="row.metric" size="small" class="full-width" @change="onMetricChange(row)">
                  <el-option
                    v-for="opt in getMetricOptions(row.conditionType)"
                    :key="opt.metric"
                    :label="opt.label"
                    :value="opt.metric"
                  />
                </el-select>
                <div class="window-select-wrap">
                  <span class="window-prefix">window</span>
                  <el-select v-model="row.evaluationWindow" size="small" class="full-width">
                    <el-option
                      v-for="item in getEvaluationWindowOptions(row.metric, row.triggerRuleId)"
                      :key="item"
                      :label="item"
                      :value="item"
                    />
                  </el-select>
                </div>
              </div>
            </div>

            <div class="condition-field">
              <label class="field-label">触发判定 / value</label>
              <div class="trigger-value-row">
                <el-select v-model="row.triggerRuleId" size="small" class="full-width" @change="onTriggerRuleChange(row)">
                  <el-option
                    v-for="rule in getAllowedRules(row.metric)"
                    :key="rule.id"
                    :label="rule.label"
                    :value="rule.id"
                  />
                </el-select>
                <div class="value-inline-group">
                  <span class="value-prefix-label">设定值</span>
                  <div class="value-input-wrap">
                    <el-input v-model="row.valueText" size="small" :placeholder="getValuePlaceholder(row.metric)" />
                    <span class="value-unit">{{ getValueUnit(row.metric) }}</span>
                  </div>
                </div>
              </div>
            </div>

            </div>
          </div>
          <div v-if="idx < conditionRows.length - 1" class="condition-logic-between">
            <span class="between-label">条件逻辑</span>
            <el-select v-model="form.condition_logic" size="small" class="logic-select-between">
              <el-option label="AND" value="AND" />
              <el-option label="OR" value="OR" />
            </el-select>
          </div>
        </template>
      </div>

      <div class="footer-actions">
        <el-button type="primary" :loading="saving" @click="saveConditions">保存触发条件</el-button>
        <el-button @click="router.push(`/strategies/${strategyId}`)">取消</el-button>
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

.editor-note {
  margin-bottom: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.conditions-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.conditions-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.conditions-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.condition-logic-between {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.between-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.logic-select-between {
  width: 120px;
}

.condition-row {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 10px;
  background: var(--el-fill-color-light);
}

.condition-fields {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.condition-field {
  display: grid;
  grid-template-columns: 140px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}

.field-label {
  display: inline-flex;
  align-items: center;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0;
}

.auto-id-text {
  min-height: 30px;
  display: flex;
  align-items: center;
  color: var(--el-text-color-regular);
  font-size: 12px;
  font-family: var(--el-font-family-monospace, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace);
}

.id-row-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.pair-products {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.preserve-pair-slot {
  width: 100%;
}

.product-slot-placeholder-select {
  visibility: hidden;
  pointer-events: none;
}

.type-product-row {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}

.metric-window-row {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(120px, 0.7fr) minmax(180px, 1fr);
  gap: 10px;
  align-items: center;
}

.window-select-wrap {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  align-items: center;
}

.window-prefix {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.full-width {
  width: 100%;
}

.value-input-wrap {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 34px;
  gap: 6px;
  align-items: center;
}

.value-inline-group {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  align-items: center;
}

.value-prefix-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.trigger-value-row {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(180px, 0.8fr);
  gap: 10px;
  align-items: center;
}

.value-unit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color);
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-blank);
  font-size: 12px;
}

.remove-icon-btn {
  width: 40px;
  height: 40px;
  font-size: 18px;
  flex: 0 0 auto;
}

.remove-icon-btn :deep(.el-icon) {
  font-size: 18px;
}

.footer-actions {
  margin-top: 16px;
  display: flex;
  gap: 8px;
}

</style>
