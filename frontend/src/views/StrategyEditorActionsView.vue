<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchStrategies, fetchStrategyDetail, putStrategyActions } from '../api/services'
import type { StrategyDetail, StrategySummary, StrategyTradeType } from '../api/types'

type ActionType = 'STOCK_TRADE' | 'FUT_POSITION' | 'FUT_ROLL'
type OrderType = 'MKT' | 'LMT'
type Side = 'BUY' | 'SELL'
type PositionEffect = 'OPEN' | 'CLOSE'

const route = useRoute()
const router = useRouter()
const strategyId = computed(() => String(route.params.id || ''))

const loading = ref(false)
const saving = ref(false)
const error = ref('')
const detail = ref<StrategyDetail | null>(null)
const strategyOptions = ref<StrategySummary[]>([])

const form = reactive({
  nextStrategyId: '',
  nextStrategyNote: '',
  enableTradeAction: true,
  actionType: 'STOCK_TRADE' as ActionType,
  symbol: '',
  quantityText: '',
  orderType: 'LMT' as OrderType,
  limitPriceText: '',
  contract: '',
  positionEffect: 'OPEN' as PositionEffect,
  closeContract: '',
  openContract: '',
  closeOrderType: 'MKT' as OrderType,
  openOrderType: 'MKT' as OrderType,
  closeLimitPriceText: '',
  openLimitPriceText: '',
  maxLegSlippageUsdText: '',
  allowOvernight: false,
  cancelOnExpiry: false,
})

const STOCK_TRADE_TYPES: StrategyTradeType[] = ['buy', 'sell', 'switch']
const FUTURES_TRADE_TYPES: StrategyTradeType[] = ['open', 'close', 'spread']

const actionTypeChoices = {
  STOCK_TRADE: '股票买卖（STOCK_TRADE）',
  FUT_POSITION: '期货开平（FUT_POSITION）',
  FUT_ROLL: '期货展期（FUT_ROLL）',
} as const

const actionStatusText = computed(() => {
  if (!detail.value) return '后续动作状态：编辑中'
  if (!form.enableTradeAction && !form.nextStrategyId.trim()) return '后续动作状态：未设置'
  if (form.enableTradeAction) return `后续动作状态：${detail.value.trade_action_runtime.trade_status || 'NOT_SET'}`
  return '后续动作状态：仅激活下游策略'
})

const tradeType = computed(() => detail.value?.trade_type ?? 'buy')
const canEdit = computed(() => Boolean(detail.value?.editable))
const editableReason = computed(() => detail.value?.editable_reason || '')
const isStockStrategy = computed(() => STOCK_TRADE_TYPES.includes(tradeType.value))
const isFuturesStrategy = computed(() => FUTURES_TRADE_TYPES.includes(tradeType.value))

const allowedActionTypes = computed<ActionType[]>(() => {
  if (isStockStrategy.value) return ['STOCK_TRADE']
  if (isFuturesStrategy.value) return ['FUT_POSITION', 'FUT_ROLL']
  return ['STOCK_TRADE']
})

const symbolTradeTypeMap = computed(() => {
  const map = new Map<string, Set<string>>()
  if (!detail.value) return map
  for (const item of detail.value.symbols) {
    const code = item.code.trim().toUpperCase()
    const tradeType = item.trade_type.trim().toLowerCase()
    if (!code) continue
    if (!map.has(code)) map.set(code, new Set<string>())
    if (tradeType) map.get(code)?.add(tradeType)
  }
  return map
})

function tradeTypeMatchesSide(symbolTradeType: string, side: Side) {
  const normalized = symbolTradeType.trim().toLowerCase()
  if (side === 'BUY') return normalized === 'buy' || normalized === 'open'
  return normalized === 'sell' || normalized === 'close'
}

function inferSideFromStrategy(): Side {
  if (tradeType.value === 'buy' || tradeType.value === 'open') return 'BUY'
  if (tradeType.value === 'sell' || tradeType.value === 'close') return 'SELL'
  if (!detail.value) return 'BUY'

  for (const item of detail.value.symbols) {
    const tt = item.trade_type.trim().toLowerCase()
    if (tt === 'ref') continue
    if (tt === 'buy' || tt === 'open') return 'BUY'
    if (tt === 'sell' || tt === 'close') return 'SELL'
  }
  return 'BUY'
}

const lockedSide = computed<Side>(() => inferSideFromStrategy())

const symbolOptions = computed<Array<{ code: string; label: string }>>(() => {
  return Array.from(symbolTradeTypeMap.value.entries())
    .filter(([, tradeTypes]) => Array.from(tradeTypes).some((tt) => tradeTypeMatchesSide(tt, lockedSide.value)))
    .map(([code, tradeTypes]) => ({
      code,
      label: tradeTypes.size > 0 ? `${code}（${Array.from(tradeTypes).join(', ')}）` : code,
    }))
    .sort((a, b) => a.code.localeCompare(b.code))
})

const defaultActionSymbol = computed(() => {
  return symbolOptions.value[0]?.code || ''
})

function sideLabel(side: Side) {
  return side === 'BUY' ? '买入（BUY）' : '卖出（SELL）'
}

const lockedSideText = computed(() => sideLabel(lockedSide.value))

const nextStrategyChoices = computed(() => {
  const selfId = strategyId.value.toUpperCase()
  return strategyOptions.value
    .filter((row) => row.id.toUpperCase() !== selfId)
    .filter((row) => row.status === 'PENDING_ACTIVATION')
    .map((row) => ({
      id: row.id,
      label: row.id,
      description: row.description || '',
    }))
})

const showNormalTradeFields = computed(() => form.actionType !== 'FUT_ROLL')
const showFutPositionFields = computed(() => form.actionType === 'FUT_POSITION')
const showFutRollFields = computed(() => form.actionType === 'FUT_ROLL')
const showLimitPrice = computed(() => form.orderType === 'LMT')
const showCloseLimitPrice = computed(() => form.closeOrderType === 'LMT')
const showOpenLimitPrice = computed(() => form.openOrderType === 'LMT')

const tradeSummary = computed(() => {
  const symbol = (form.symbol || '--').toUpperCase()
  const quantity = form.quantityText.trim() || '--'
  const overnight = form.allowOvernight ? '是' : '否'
  const cancelOnExpiry = form.cancelOnExpiry ? 'true' : 'false'

  if (form.actionType === 'STOCK_TRADE') {
    const sideText = lockedSide.value === 'BUY' ? '买入' : '卖出'
    const limitText = form.orderType === 'LMT' ? `，限价 ${form.limitPriceText.trim() || '--'}` : ''
    return `${sideText} ${quantity} 股 ${symbol}，${form.orderType}${limitText}，DAY，隔夜=${overnight}，到期撤单=${cancelOnExpiry}`
  }

  if (form.actionType === 'FUT_POSITION') {
    const effectText = form.positionEffect === 'OPEN' ? '开仓' : '平仓'
    const sideText = lockedSide.value === 'BUY' ? '买入' : '卖出'
    const contract = form.contract.trim().toUpperCase() || symbol
    const limitText = form.orderType === 'LMT' ? `，限价 ${form.limitPriceText.trim() || '--'}` : ''
    return `${effectText} ${sideText} ${quantity} 手 ${contract}，${form.orderType}${limitText}，DAY，隔夜=${overnight}，到期撤单=${cancelOnExpiry}`
  }

  const closeContract = form.closeContract.trim().toUpperCase() || '--'
  const openContract = form.openContract.trim().toUpperCase() || '--'
  const slippage = form.maxLegSlippageUsdText.trim() || '未设置'
  return `展期 ${closeContract} -> ${openContract}，数量 ${quantity} 手，平仓${form.closeOrderType}/开仓${form.openOrderType}，腿间滑点上限 ${slippage} USD，隔夜=${overnight}，到期撤单=${cancelOnExpiry}`
})

function normalizeText(value: unknown) {
  if (value == null) return ''
  return String(value).trim()
}

function asObject(value: unknown) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function normalizeActionType(raw: unknown): ActionType {
  const actionType = normalizeText(raw).toUpperCase()
  if (actionType === 'FUT_POSITION') return 'FUT_POSITION'
  if (actionType === 'FUT_ROLL') return 'FUT_ROLL'
  return 'STOCK_TRADE'
}

function normalizeOrderType(raw: unknown): OrderType {
  return normalizeText(raw).toUpperCase() === 'MKT' ? 'MKT' : 'LMT'
}

function normalizePositionEffect(raw: unknown): PositionEffect {
  return normalizeText(raw).toUpperCase() === 'CLOSE' ? 'CLOSE' : 'OPEN'
}

function normalizeBoolean(raw: unknown, fallback = false) {
  if (typeof raw === 'boolean') return raw
  if (raw == null) return fallback
  const text = normalizeText(raw).toLowerCase()
  if (text === 'true') return true
  if (text === 'false') return false
  return fallback
}

function normalizeActionTypeForStrategy() {
  if (!allowedActionTypes.value.includes(form.actionType)) {
    form.actionType = allowedActionTypes.value[0] || 'STOCK_TRADE'
  }
}

function applyActionDefaults() {
  normalizeActionTypeForStrategy()
  if (form.symbol) {
    form.symbol = form.symbol.trim().toUpperCase()
  }

  const allowedSymbolSet = new Set(symbolOptions.value.map((item) => item.code))
  if (form.enableTradeAction) {
    if (!form.symbol || !allowedSymbolSet.has(form.symbol)) {
      form.symbol = defaultActionSymbol.value
    }
  } else if (form.symbol && !allowedSymbolSet.has(form.symbol)) {
    form.symbol = ''
  }

  if (!showLimitPrice.value) form.limitPriceText = ''
  if (!showCloseLimitPrice.value) form.closeLimitPriceText = ''
  if (!showOpenLimitPrice.value) form.openLimitPriceText = ''
  if (!showFutPositionFields.value) {
    form.contract = ''
    form.positionEffect = 'OPEN'
  }
  if (!showFutRollFields.value) {
    form.closeContract = ''
    form.openContract = ''
    form.closeOrderType = 'MKT'
    form.openOrderType = 'MKT'
    form.closeLimitPriceText = ''
    form.openLimitPriceText = ''
    form.maxLegSlippageUsdText = ''
  }
}

function loadFromDetail(d: StrategyDetail) {
  detail.value = d
  form.nextStrategyId = d.next_strategy?.id ?? ''
  form.nextStrategyNote = d.next_strategy?.description ?? ''

  const action = d.trade_action_json ? asObject(d.trade_action_json) : null
  form.enableTradeAction = Boolean(action)

  if (!action) {
    form.actionType = allowedActionTypes.value[0] || 'STOCK_TRADE'
    form.symbol = defaultActionSymbol.value
    form.quantityText = ''
    form.orderType = 'LMT'
    form.limitPriceText = ''
    form.contract = ''
    form.positionEffect = tradeType.value === 'close' ? 'CLOSE' : 'OPEN'
    form.closeContract = ''
    form.openContract = ''
    form.closeOrderType = 'MKT'
    form.openOrderType = 'MKT'
    form.closeLimitPriceText = ''
    form.openLimitPriceText = ''
    form.maxLegSlippageUsdText = ''
    form.allowOvernight = false
    form.cancelOnExpiry = false
    applyActionDefaults()
    return
  }

  form.actionType = normalizeActionType(action.action_type)
  form.symbol = normalizeText(action.symbol).toUpperCase()
  form.quantityText = normalizeText(action.quantity)
  form.orderType = normalizeOrderType(action.order_type)
  form.limitPriceText = normalizeText(action.limit_price)
  form.contract = normalizeText(action.contract).toUpperCase()
  form.positionEffect = normalizePositionEffect(action.position_effect)
  form.closeContract = normalizeText(action.close_contract).toUpperCase()
  form.openContract = normalizeText(action.open_contract).toUpperCase()
  form.closeOrderType = normalizeOrderType(action.close_order_type)
  form.openOrderType = normalizeOrderType(action.open_order_type)
  form.closeLimitPriceText = normalizeText(action.close_limit_price)
  form.openLimitPriceText = normalizeText(action.open_limit_price)
  form.maxLegSlippageUsdText = normalizeText(action.max_leg_slippage_usd)
  form.allowOvernight = normalizeBoolean(action.allow_overnight, false)
  form.cancelOnExpiry = normalizeBoolean(action.cancel_on_expiry, false)
  applyActionDefaults()
}

async function loadDetail() {
  if (!strategyId.value) return
  loading.value = true
  error.value = ''
  try {
    const d = await fetchStrategyDetail(strategyId.value)
    loadFromDetail(d)

    try {
      const all = await fetchStrategies()
      strategyOptions.value = all
      const allowed = new Set(nextStrategyChoices.value.map((item) => item.id.toUpperCase()))
      if (form.nextStrategyId && !allowed.has(form.nextStrategyId.toUpperCase())) {
        form.nextStrategyId = ''
        ElMessage.warning('当前下游策略不是 PENDING_ACTIVATION 状态，已清空，请重新选择。')
      }
    } catch {
      strategyOptions.value = []
    }
  } catch (err) {
    error.value = `加载策略失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

function parseRequiredNumber(raw: string, label: string, allowZero = false) {
  const text = raw.trim()
  if (!text) throw new Error(`请填写${label}`)
  const value = Number(text)
  if (!Number.isFinite(value)) throw new Error(`${label}必须是数字`)
  if (allowZero ? value < 0 : value <= 0) throw new Error(`${label}必须${allowZero ? '大于等于' : '大于'} 0`)
  return value
}

function parseOptionalNumber(raw: string, label: string, allowZero = true) {
  const text = raw.trim()
  if (!text) return undefined
  const value = Number(text)
  if (!Number.isFinite(value)) throw new Error(`${label}必须是数字`)
  if (allowZero ? value < 0 : value <= 0) throw new Error(`${label}必须${allowZero ? '大于等于' : '大于'} 0`)
  return value
}

function buildTradeActionPayload() {
  if (!form.enableTradeAction) return null

  normalizeActionTypeForStrategy()
  if (!allowedActionTypes.value.includes(form.actionType)) {
    throw new Error(`trade_type=${tradeType.value} 不允许 action_type=${form.actionType}`)
  }

  const symbol = form.symbol.trim().toUpperCase()
  if (!symbol) throw new Error('请选择产品代码')
  if (!symbolOptions.value.some((item) => item.code === symbol)) {
    throw new Error('产品代码必须与交易方向一致')
  }

  const quantity = parseRequiredNumber(form.quantityText, '下单数量')
  const payload: Record<string, unknown> = {
    action_type: form.actionType,
    symbol,
    quantity,
    tif: 'DAY',
    allow_overnight: form.allowOvernight,
    cancel_on_expiry: form.cancelOnExpiry,
  }

  if (form.actionType === 'STOCK_TRADE') {
    payload.side = lockedSide.value
    payload.order_type = form.orderType
    if (form.orderType === 'LMT') {
      payload.limit_price = parseRequiredNumber(form.limitPriceText, '限价')
    }
    return payload
  }

  if (form.actionType === 'FUT_POSITION') {
    if (form.contract.trim()) payload.contract = form.contract.trim().toUpperCase()
    payload.position_effect = form.positionEffect
    payload.side = lockedSide.value
    payload.order_type = form.orderType
    if (form.orderType === 'LMT') {
      payload.limit_price = parseRequiredNumber(form.limitPriceText, '限价')
    }
    return payload
  }

  const closeContract = form.closeContract.trim().toUpperCase()
  const openContract = form.openContract.trim().toUpperCase()
  if (!closeContract) throw new Error('请填写待平合约（近月）')
  if (!openContract) throw new Error('请填写目标合约（远月）')
  payload.close_contract = closeContract
  payload.open_contract = openContract
  payload.close_order_type = form.closeOrderType
  payload.open_order_type = form.openOrderType
  if (form.closeOrderType === 'LMT') {
    payload.close_limit_price = parseRequiredNumber(form.closeLimitPriceText, '平仓限价')
  }
  if (form.openOrderType === 'LMT') {
    payload.open_limit_price = parseRequiredNumber(form.openLimitPriceText, '开仓限价')
  }
  const slippage = parseOptionalNumber(form.maxLegSlippageUsdText, '腿间最大滑点')
  if (slippage !== undefined) payload.max_leg_slippage_usd = slippage
  return payload
}

async function saveActions() {
  if (!strategyId.value || !detail.value) return
  saving.value = true
  error.value = ''
  try {
    const nextStrategyId = form.nextStrategyId.trim().toUpperCase()
    if (nextStrategyId && nextStrategyId === strategyId.value.toUpperCase()) {
      throw new Error('下游策略不能指向当前策略自身')
    }
    if (nextStrategyId) {
      const allowed = new Set(nextStrategyChoices.value.map((item) => item.id.toUpperCase()))
      if (!allowed.has(nextStrategyId)) {
        throw new Error('下游策略必须从 PENDING_ACTIVATION 列表中选择')
      }
    }

    const tradeAction = buildTradeActionPayload()
    if (!tradeAction && !nextStrategyId) {
      throw new Error('交易动作和下游策略至少配置一个')
    }

    const updated = await putStrategyActions(strategyId.value, {
      trade_action_json: tradeAction,
      next_strategy_id: nextStrategyId || null,
      next_strategy_note: form.nextStrategyNote.trim() || null,
    })

    ElMessage.success(`已保存后续动作（${updated.id}）`)
    router.push({ path: `/strategies/${strategyId.value}`, query: { anchor: 'actions' } })
  } catch (err) {
    error.value = `保存失败：${String(err)}`
  } finally {
    saving.value = false
  }
}

watch(
  () => [form.actionType, form.orderType, form.closeOrderType, form.openOrderType, form.enableTradeAction],
  () => {
    applyActionDefaults()
  },
)

watch(
  () => form.nextStrategyId,
  (nextId) => {
    const selected = nextStrategyChoices.value.find((item) => item.id === nextId)
    form.nextStrategyNote = selected?.description || ''
  },
)

watch(strategyId, loadDetail)
onMounted(loadDetail)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row actions-header-row">
          <span class="card-title">后续动作编辑</span>
          <span class="card-tools">{{ actionStatusText }}</span>
          <el-button size="small" @click="router.push({ path: `/strategies/${strategyId}`, query: { anchor: 'actions' } })">返回策略详情</el-button>
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

      <el-alert
        v-if="detail && !canEdit"
        :title="editableReason || '当前状态不可编辑后续动作'"
        type="warning"
        show-icon
        :closable="false"
        class="mb-12"
      />

      <div v-loading="loading" class="actions-editor-body">
        <div v-if="detail" class="editor-meta">
          <span>策略ID：{{ detail.id }}</span>
          <span>交易类型：{{ detail.trade_type }}</span>
        </div>

        <div class="actions-grid">
          <div class="editor-sub-card">
            <div class="sub-card-header">
              <span class="sub-card-title">后续策略</span>
              <el-tag size="small" effect="dark" type="info">{{ form.nextStrategyId ? 'CONFIGURED' : 'NOT_SET' }}</el-tag>
            </div>

            <div class="form-field">
              <label class="field-label">策略ID</label>
              <div class="next-strategy-row">
                <el-select
                  v-model="form.nextStrategyId"
                  filterable
                  clearable
                  placeholder="仅可选择 PENDING_ACTIVATION 的策略（留空表示不激活后续策略）"
                >
                  <el-option
                    v-for="item in nextStrategyChoices"
                    :key="item.id"
                    :label="item.label"
                    :value="item.id"
                  />
                </el-select>
                <el-button
                  class="next-strategy-create-btn"
                  @click="router.push({ path: '/strategies/new', query: { link_upstream: strategyId } })"
                >
                  新增策略
                </el-button>
              </div>
            </div>

            <div class="form-field">
              <label class="field-label">说明</label>
              <el-input
                v-model="form.nextStrategyNote"
                type="textarea"
                :rows="3"
                placeholder="例如：SLV 回撤达到 20% 时再卖出 100 股"
              />
            </div>
          </div>

          <div class="editor-sub-card">
            <div class="sub-card-header">
              <div class="sub-card-title-row">
                <span class="sub-card-title">交易指令</span>
                <el-switch v-model="form.enableTradeAction" />
              </div>
              <el-tag size="small" effect="dark" :type="form.enableTradeAction ? 'success' : 'info'">
                {{ form.enableTradeAction ? (detail?.trade_action_runtime?.trade_status || 'EDITING') : 'NOT_SET' }}
              </el-tag>
            </div>

            <template v-if="form.enableTradeAction">
              <div class="trade-row trade-row-2">
                <div class="form-inline-field action-type-inline">
                  <label class="field-label">动作类型</label>
                  <el-radio-group v-model="form.actionType" class="action-type-group">
                    <el-radio
                      v-for="type in allowedActionTypes"
                      :key="type"
                      :label="type"
                    >
                      {{ actionTypeChoices[type] }}
                    </el-radio>
                  </el-radio-group>
                </div>
                <div class="form-inline-field trade-inline-field direction-field">
                  <label class="field-label">交易方向</label>
                  <span class="direction-value">{{ lockedSideText }}</span>
                </div>
              </div>

              <div class="trade-row trade-row-2">
                <div class="form-inline-field trade-inline-field symbol-field">
                  <label class="field-label">产品代码</label>
                  <el-select class="adaptive-control" v-model="form.symbol" filterable placeholder="仅可选择与交易方向一致的 symbols">
                    <el-option
                      v-for="symbol in symbolOptions"
                      :key="symbol.code"
                      :label="symbol.label"
                      :value="symbol.code"
                    />
                  </el-select>
                </div>
                <div class="form-inline-field trade-inline-field">
                  <label class="field-label">下单数量</label>
                  <el-input v-model="form.quantityText" placeholder="例如 100" />
                </div>
              </div>

              <template v-if="showNormalTradeFields">
                <div v-if="showFutPositionFields" class="trade-row trade-row-2">
                  <div class="form-field">
                    <label class="field-label">期货合约（可选）</label>
                    <el-input v-model="form.contract" placeholder="例如 SIH7" />
                  </div>
                  <div class="form-field">
                    <label class="field-label">开平类型</label>
                    <el-radio-group v-model="form.positionEffect">
                      <el-radio-button label="OPEN">OPEN</el-radio-button>
                      <el-radio-button label="CLOSE">CLOSE</el-radio-button>
                    </el-radio-group>
                  </div>
                </div>

                <div class="trade-row trade-row-2">
                  <div class="form-inline-field trade-inline-field">
                    <label class="field-label">订单类型</label>
                    <el-radio-group v-model="form.orderType">
                      <el-radio-button label="LMT">LMT</el-radio-button>
                      <el-radio-button label="MKT">MKT</el-radio-button>
                    </el-radio-group>
                  </div>
                  <div class="form-inline-field trade-inline-field">
                    <label class="field-label">限价（LMT）</label>
                    <el-input
                      v-model="form.limitPriceText"
                      :disabled="!showLimitPrice"
                      :placeholder="showLimitPrice ? '仅限价单必填' : '市价单不需要限价'"
                    />
                  </div>
                </div>
              </template>

              <template v-if="showFutRollFields">
                <div class="trade-row trade-row-2">
                  <div class="form-field">
                    <label class="field-label">待平合约（近月）</label>
                    <el-input v-model="form.closeContract" placeholder="例如 SIH6" />
                  </div>
                  <div class="form-field">
                    <label class="field-label">目标合约（远月）</label>
                    <el-input v-model="form.openContract" placeholder="例如 SIK6" />
                  </div>
                </div>

                <div class="trade-row trade-row-3">
                  <div class="form-field">
                    <label class="field-label">平仓订单类型</label>
                    <el-radio-group v-model="form.closeOrderType">
                      <el-radio-button label="LMT">LMT</el-radio-button>
                      <el-radio-button label="MKT">MKT</el-radio-button>
                    </el-radio-group>
                  </div>
                  <div class="form-field">
                    <label class="field-label">开仓订单类型</label>
                    <el-radio-group v-model="form.openOrderType">
                      <el-radio-button label="LMT">LMT</el-radio-button>
                      <el-radio-button label="MKT">MKT</el-radio-button>
                    </el-radio-group>
                  </div>
                  <div class="form-field">
                    <label class="field-label">腿间最大滑点（USD，可选）</label>
                    <el-input v-model="form.maxLegSlippageUsdText" placeholder="例如 150" />
                  </div>
                </div>

                <div class="trade-row trade-row-2">
                  <div class="form-field">
                    <label class="field-label">平仓限价</label>
                    <el-input
                      v-model="form.closeLimitPriceText"
                      :disabled="!showCloseLimitPrice"
                      :placeholder="showCloseLimitPrice ? '仅平仓 LMT 必填' : '平仓 MKT 不需要限价'"
                    />
                  </div>
                  <div class="form-field">
                    <label class="field-label">开仓限价</label>
                    <el-input
                      v-model="form.openLimitPriceText"
                      :disabled="!showOpenLimitPrice"
                      :placeholder="showOpenLimitPrice ? '仅开仓 LMT 必填' : '开仓 MKT 不需要限价'"
                    />
                  </div>
                </div>
              </template>

              <div class="trade-row trade-row-2">
                <div class="form-inline-field trade-inline-field">
                  <label class="field-label">隔夜交易</label>
                  <el-switch v-model="form.allowOvernight" />
                </div>
                <div class="form-inline-field trade-inline-field">
                  <label class="field-label">到期撤单</label>
                  <el-switch v-model="form.cancelOnExpiry" />
                </div>
              </div>

              <div class="summary-box">
                <div class="summary-title">交易摘要</div>
                <div class="summary-text">{{ tradeSummary }}</div>
              </div>
            </template>

            <div v-else class="trade-disabled-hint">未配置交易动作，将仅用于激活下游策略。</div>
          </div>
        </div>

        <div class="footer-actions">
          <el-button type="primary" :loading="saving" :disabled="!canEdit" @click="saveActions">
            保存后续动作
          </el-button>
          <el-button @click="router.push({ path: `/strategies/${strategyId}`, query: { anchor: 'actions' } })">取消</el-button>
        </div>
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

.actions-header-row {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 12px;
}

.mb-12 {
  margin-bottom: 12px;
}

.card-tools {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  justify-self: center;
  text-align: center;
  white-space: nowrap;
}

.actions-editor-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.editor-meta {
  display: flex;
  gap: 16px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.actions-grid {
  display: grid;
  grid-template-columns: minmax(260px, 0.95fr) minmax(360px, 1.35fr);
  gap: 12px;
}

.editor-sub-card {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  padding: 12px;
  background: var(--el-fill-color-light);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sub-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.sub-card-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.sub-card-title-row {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.next-strategy-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
}

.next-strategy-create-btn {
  font-size: var(--el-font-size-base);
}

.form-inline-field {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
}

.trade-inline-field {
  grid-template-columns: 88px minmax(0, 1fr);
}

.symbol-field :deep(.adaptive-control.el-select) {
  min-width: 0;
  width: fit-content;
  max-width: 100%;
}

.field-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.action-type-group {
  display: flex;
  flex-wrap: wrap;
  row-gap: 8px;
}

.trade-row {
  display: grid;
  gap: 10px;
}

.trade-row-2 {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.trade-row-3 {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.direction-field {
  justify-self: start;
}

.direction-value {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
  color: var(--el-text-color-regular);
  font-size: 13px;
  line-height: 1;
  white-space: nowrap;
}

.summary-box {
  border: 1px dashed var(--el-border-color);
  border-radius: 8px;
  padding: 10px;
  background: var(--el-fill-color-blank);
}

.summary-title {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-bottom: 4px;
}

.summary-text {
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.5;
}

.trade-disabled-hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 8px 0;
}

.footer-actions {
  margin-top: 4px;
  display: flex;
  gap: 8px;
}

@media (max-width: 980px) {
  .actions-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .next-strategy-row {
    grid-template-columns: 1fr;
  }

  .form-inline-field {
    grid-template-columns: 1fr;
    align-items: flex-start;
  }

  .trade-row-2,
  .trade-row-3 {
    grid-template-columns: 1fr;
  }
}
</style>
