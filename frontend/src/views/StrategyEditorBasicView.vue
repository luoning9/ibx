<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createStrategy, fetchStrategyDetail, patchStrategyBasic, putStrategyActions } from '../api/services'
import type { StrategyMarket, StrategySymbolItem, StrategyTradeType } from '../api/types'

const route = useRoute()
const router = useRouter()

const strategyId = computed(() => String(route.params.id || ''))
const isCreateMode = computed(() => !strategyId.value)
const linkUpstreamId = computed(() => {
  const raw = route.query.link_upstream
  const value = Array.isArray(raw) ? raw[0] : raw
  const text = String(value || '').trim().toUpperCase()
  return text || ''
})

const loading = ref(false)
const saving = ref(false)
const error = ref('')

const form = reactive({
  id: '',
  description: '',
  market: 'US_STOCK' as StrategyMarket,
  trade_type: 'buy' as StrategyTradeType,
  upstream_only_activation: false,
  expire_mode: 'relative' as 'relative' | 'absolute',
  expire_in_seconds: 86400,
  expire_at: '',
})

const marketOptions: Array<{ label: string; value: StrategyMarket }> = [
  { label: '美股', value: 'US_STOCK' },
  { label: 'COMEX期货', value: 'COMEX_FUTURES' },
]
const tradeTypesByMarket: Record<StrategyMarket, StrategyTradeType[]> = {
  US_STOCK: ['buy', 'sell', 'switch'],
  COMEX_FUTURES: ['open', 'close', 'spread'],
}
const defaultTradeTypeByMarket: Record<StrategyMarket, StrategyTradeType> = {
  US_STOCK: 'buy',
  COMEX_FUTURES: 'open',
}
const tradeTypes = computed(() => tradeTypesByMarket[form.market])
type CoreSymbolTradeType = Exclude<StrategySymbolItem['trade_type'], 'ref'>
type RequiredSymbolSpec = { type: CoreSymbolTradeType; label: string; placeholder: string }
type RelativeExpirePreset = '1h' | '2h' | 'today' | '1d' | '2d' | '3d' | '5d' | '1w'
type TimeZoneDateParts = { year: number; month: number; day: number; hour: number; minute: number; second: number }

const MARKET_TIME_ZONE = 'America/New_York'

const coreSymbolCodes = reactive<Record<CoreSymbolTradeType, string>>({
  buy: 'SLV',
  sell: '',
  open: '',
  close: '',
})

const refSymbolCodes = ref<string[]>([])
const refSymbolInput = ref('')
const relativeExpirePreset = ref<RelativeExpirePreset>('1d')
const browserTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local'

const relativeExpireOptions: Array<{ label: string; value: RelativeExpirePreset }> = [
  { label: '1小时', value: '1h' },
  { label: '2小时', value: '2h' },
  { label: '当天（America/New_York）', value: 'today' },
  { label: '1天', value: '1d' },
  { label: '2天', value: '2d' },
  { label: '3天', value: '3d' },
  { label: '5天', value: '5d' },
  { label: '1周', value: '1w' },
]

const relativeExpireSecondsMap: Record<Exclude<RelativeExpirePreset, 'today'>, number> = {
  '1h': 3600,
  '2h': 7200,
  '1d': 86400,
  '2d': 172800,
  '3d': 259200,
  '5d': 432000,
  '1w': 604800,
}

function getTimeZoneDateParts(date: Date, timeZone: string): TimeZoneDateParts {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
  const valueOf = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((part) => part.type === type)?.value ?? 0)

  return {
    year: valueOf('year'),
    month: valueOf('month'),
    day: valueOf('day'),
    hour: valueOf('hour'),
    minute: valueOf('minute'),
    second: valueOf('second'),
  }
}

function getTimeZoneOffsetMillis(date: Date, timeZone: string) {
  const parts = getTimeZoneDateParts(date, timeZone)
  const asUtcMillis = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second)
  return asUtcMillis - date.getTime()
}

function getZonedDateTimeUtcMillis(
  timeZone: string,
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
  second = 0,
) {
  const utcGuess = Date.UTC(year, month - 1, day, hour, minute, second)
  const firstOffset = getTimeZoneOffsetMillis(new Date(utcGuess), timeZone)
  const firstPassUtc = utcGuess - firstOffset
  const secondOffset = getTimeZoneOffsetMillis(new Date(firstPassUtc), timeZone)

  if (firstOffset !== secondOffset) return utcGuess - secondOffset
  return firstPassUtc
}

function getMarketTodayRemainingSeconds() {
  const now = new Date()
  const marketNow = getTimeZoneDateParts(now, MARKET_TIME_ZONE)

  const marketDate = new Date(Date.UTC(marketNow.year, marketNow.month - 1, marketNow.day))
  marketDate.setUTCDate(marketDate.getUTCDate() + 1)
  const nextMarketDayYear = marketDate.getUTCFullYear()
  const nextMarketDayMonth = marketDate.getUTCMonth() + 1
  const nextMarketDayDate = marketDate.getUTCDate()

  const nextMarketMidnightUtc = getZonedDateTimeUtcMillis(
    MARKET_TIME_ZONE,
    nextMarketDayYear,
    nextMarketDayMonth,
    nextMarketDayDate,
    0,
    0,
    0,
  )

  return Math.max(60, Math.ceil((nextMarketMidnightUtc - now.getTime()) / 1000))
}

function resolveRelativeExpireSeconds(preset: RelativeExpirePreset) {
  if (preset === 'today') return getMarketTodayRemainingSeconds()
  return relativeExpireSecondsMap[preset]
}

function inferRelativeExpirePreset(expireInSeconds: number | null | undefined): RelativeExpirePreset {
  if (!expireInSeconds || expireInSeconds <= 0) return '1d'
  if (expireInSeconds === 3600) return '1h'
  if (expireInSeconds === 7200) return '2h'
  if (expireInSeconds === 86400) return '1d'
  if (expireInSeconds === 172800) return '2d'
  if (expireInSeconds === 259200) return '3d'
  if (expireInSeconds === 432000) return '5d'
  if (expireInSeconds === 604800) return '1w'
  if (expireInSeconds < 86400) return 'today'

  const fixedPresets: RelativeExpirePreset[] = ['1h', '2h', '1d', '2d', '3d', '5d', '1w']
  let closest: RelativeExpirePreset = '1h'
  let minDiff = Math.abs(resolveRelativeExpireSeconds(closest) - expireInSeconds)

  for (const preset of fixedPresets.slice(1)) {
    const diff = Math.abs(resolveRelativeExpireSeconds(preset) - expireInSeconds)
    if (diff < minDiff) {
      minDiff = diff
      closest = preset
    }
  }

  return closest
}

function formatForLocalDateTimeInput(iso: string | null | undefined) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso

  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function normalizeAbsoluteExpireAt(raw: string) {
  const value = raw.trim()
  if (!value) throw new Error('请填写绝对过期时间')

  const cleaned = value.replace(/\s+/g, ' ')
  const match = cleaned.match(/^(\d{4})(\d{2})(\d{2})(?:\s+(\d{2}):(\d{2}))?$/)
  if (!match) {
    throw new Error('绝对过期时间格式无效，请使用 YYYYMMDD 或 YYYYMMDD HH:MM（按浏览器时区解释）')
  }

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const hour = Number(match[4] ?? '0')
  const minute = Number(match[5] ?? '0')
  const second = 0

  const date = new Date(year, month - 1, day, hour, minute, second, 0)
  const isValid =
    date.getFullYear() === year &&
    date.getMonth() === month - 1 &&
    date.getDate() === day &&
    date.getHours() === hour &&
    date.getMinutes() === minute &&
    date.getSeconds() === second

  if (!isValid) {
    throw new Error('绝对过期时间无效，请检查日期与时间取值')
  }

  return date.toISOString()
}

const requiredSymbolSpecsMap: Record<StrategyTradeType, RequiredSymbolSpec[]> = {
  buy: [{ type: 'buy', label: '买入产品（buy）', placeholder: '输入买入产品代码，如 AAPL' }],
  sell: [{ type: 'sell', label: '卖出产品（sell）', placeholder: '输入卖出产品代码，如 AAPL' }],
  switch: [
    { type: 'sell', label: '卖出产品（sell）', placeholder: '输入卖出产品代码，如 AAPL' },
    { type: 'buy', label: '买入产品（buy）', placeholder: '输入买入产品代码，如 MSFT' },
  ],
  open: [{ type: 'open', label: '开仓产品（open）', placeholder: '输入开仓产品代码，如 SIH7' }],
  close: [{ type: 'close', label: '平仓产品（close）', placeholder: '输入平仓产品代码，如 SIH7' }],
  spread: [
    { type: 'open', label: '开仓产品（open）', placeholder: '输入开仓产品代码，如 SIH7' },
    { type: 'close', label: '平仓产品（close）', placeholder: '输入平仓产品代码，如 SIM7' },
  ],
}

const requiredSymbolSpecs = computed(() => requiredSymbolSpecsMap[form.trade_type])

function applySymbolsToInputs(symbols: StrategySymbolItem[]) {
  coreSymbolCodes.buy = ''
  coreSymbolCodes.sell = ''
  coreSymbolCodes.open = ''
  coreSymbolCodes.close = ''

  const refs: string[] = []
  const seenRefs = new Set<string>()
  for (const item of symbols) {
    if (item.trade_type === 'ref') {
      const normalizedRef = item.code.trim().toUpperCase()
      if (normalizedRef && !seenRefs.has(normalizedRef)) {
        refs.push(normalizedRef)
        seenRefs.add(normalizedRef)
      }
      continue
    }
    if (!coreSymbolCodes[item.trade_type]) {
      coreSymbolCodes[item.trade_type] = item.code
    }
  }

  refSymbolCodes.value = refs
  refSymbolInput.value = ''
}

watch(
  () => form.market,
  (nextMarket) => {
    const availableTypes = tradeTypesByMarket[nextMarket]
    if (!availableTypes.includes(form.trade_type)) {
      form.trade_type = defaultTradeTypeByMarket[nextMarket]
    }
  },
)

function appendRefSymbols(raw: string) {
  const tokens = raw
    .split(/[\s,，;；]+/)
    .map((token) => token.trim().toUpperCase())
    .filter(Boolean)

  if (tokens.length === 0) return

  const existing = new Set(refSymbolCodes.value)
  for (const token of tokens) {
    if (existing.has(token)) continue
    refSymbolCodes.value.push(token)
    existing.add(token)
  }
}

function commitRefInput() {
  appendRefSymbols(refSymbolInput.value)
  refSymbolInput.value = ''
}

function onRefInputChange(value: string) {
  if (!/[,\n，;；]/.test(value)) return
  appendRefSymbols(value)
  refSymbolInput.value = ''
}

function removeRefSymbol(index: number) {
  refSymbolCodes.value.splice(index, 1)
}

function buildSymbolsPayload() {
  const symbols: StrategySymbolItem[] = []
  for (const spec of requiredSymbolSpecs.value) {
    const code = coreSymbolCodes[spec.type].trim().toUpperCase()
    if (!code) throw new Error(`请填写${spec.label}`)
    symbols.push({ code, trade_type: spec.type, contract_id: null })
  }

  for (const code of refSymbolCodes.value) {
    const normalized = code.trim().toUpperCase()
    if (normalized) symbols.push({ code: normalized, trade_type: 'ref', contract_id: null })
  }

  return symbols
}

async function loadDetail() {
  if (isCreateMode.value) return
  loading.value = true
  error.value = ''
  try {
    const detail = await fetchStrategyDetail(strategyId.value)
    form.id = detail.id
    form.description = detail.description
    form.market = detail.market || (detail.sec_type === 'FUT' ? 'COMEX_FUTURES' : 'US_STOCK')
    form.trade_type = detail.trade_type
    applySymbolsToInputs(detail.symbols)
    form.upstream_only_activation = detail.upstream_only_activation
    form.expire_mode = detail.expire_at ? 'absolute' : 'relative'
    form.expire_in_seconds = detail.expire_in_seconds ?? 86400
    relativeExpirePreset.value = inferRelativeExpirePreset(form.expire_in_seconds)
    form.expire_at = formatForLocalDateTimeInput(detail.expire_at)
  } catch (err) {
    error.value = `加载策略失败：${String(err)}`
  } finally {
    loading.value = false
  }
}

async function saveBasic() {
  saving.value = true
  error.value = ''
  try {
    if (form.expire_mode === 'relative') {
      form.expire_in_seconds = resolveRelativeExpireSeconds(relativeExpirePreset.value)
    }
    commitRefInput()
    const symbols = buildSymbolsPayload()

    if (isCreateMode.value) {
      const created = await createStrategy({
        id: form.id.trim() || undefined,
        description: form.description,
        market: form.market,
        trade_type: form.trade_type,
        symbols,
        upstream_only_activation: form.upstream_only_activation,
        expire_mode: form.expire_mode,
        expire_in_seconds: form.expire_mode === 'relative' ? form.expire_in_seconds : null,
        expire_at: form.expire_mode === 'absolute' ? normalizeAbsoluteExpireAt(form.expire_at) : null,
        condition_logic: 'AND',
        conditions: [],
      })
      const upstreamId = linkUpstreamId.value
      if (upstreamId) {
        try {
          const upstreamDetail = await fetchStrategyDetail(upstreamId)
          await putStrategyActions(upstreamId, {
            trade_action_json: upstreamDetail.trade_action_json,
            next_strategy_id: created.id,
            next_strategy_note: created.description || null,
          })
          ElMessage.success(`策略 ${created.id} 已创建，并已关联到上游 ${upstreamId}`)
          router.push(`/strategies/${upstreamId}/edit/actions`)
        } catch (linkErr) {
          ElMessage.warning(`策略 ${created.id} 已创建，但自动关联上游失败：${String(linkErr)}`)
          router.push(`/strategies/${created.id}`)
        }
        return
      }

      ElMessage.success(`策略 ${created.id} 已创建`)
      router.push(`/strategies/${created.id}`)
      return
    }

    const updated = await patchStrategyBasic(strategyId.value, {
      description: form.description,
      market: form.market,
      trade_type: form.trade_type,
      symbols,
      upstream_only_activation: form.upstream_only_activation,
      expire_mode: form.expire_mode,
      expire_in_seconds: form.expire_mode === 'relative' ? form.expire_in_seconds : null,
      expire_at: form.expire_mode === 'absolute' ? normalizeAbsoluteExpireAt(form.expire_at) : null,
    })
    ElMessage.success(`策略 ${updated.id} 已更新`)
    router.push(`/strategies/${updated.id}`)
  } catch (err) {
    error.value = `保存失败：${String(err)}`
  } finally {
    saving.value = false
  }
}

onMounted(loadDetail)
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">{{ isCreateMode ? '新建策略 - 基本信息' : `编辑策略 ${strategyId} - 基本信息` }}</span>
          <el-space>
            <el-button size="small" @click="router.push('/strategies')">返回列表</el-button>
            <el-button v-if="!isCreateMode" size="small" @click="router.push(`/strategies/${strategyId}`)">
              返回详情
            </el-button>
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

      <el-form label-width="140px" v-loading="loading">
        <el-form-item label="策略ID（可选）" v-if="isCreateMode">
          <el-input v-model="form.id" placeholder="留空自动生成" />
        </el-form-item>
        <el-form-item label="自然语言描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="market">
          <el-select v-model="form.market" class="trade-type-select">
            <el-option v-for="opt in marketOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="交易类型">
          <div class="trade-type-row">
            <el-select v-model="form.trade_type" class="trade-type-select">
              <el-option v-for="tp in tradeTypes" :key="tp" :value="tp" :label="tp" />
            </el-select>
            <div class="required-inline">
              <div v-for="spec in requiredSymbolSpecs" :key="spec.type" class="required-inline-item">
                <el-tag class="required-type-tag" type="success">{{ spec.type }}</el-tag>
                <el-input class="required-code-input" v-model="coreSymbolCodes[spec.type]" :placeholder="spec.placeholder" />
              </div>
              <div
                v-if="requiredSymbolSpecs.length === 1"
                class="required-inline-item required-inline-placeholder"
                aria-hidden="true"
              />
            </div>
          </div>
        </el-form-item>

        <el-form-item label="参考产品">
          <div class="ref-input-stack">
            <el-input
              v-model="refSymbolInput"
              placeholder="输入后按回车或逗号添加，支持粘贴多个代码"
              @keyup.enter="commitRefInput"
              @change="commitRefInput"
              @input="onRefInputChange"
            >
              <template #append>
                <el-button @click="commitRefInput">添加</el-button>
              </template>
            </el-input>
            <div class="ref-tags">
              <el-tag v-for="(code, idx) in refSymbolCodes" :key="`${code}-${idx}`" type="info" closable @close="removeRefSymbol(idx)">
                {{ code }}
              </el-tag>
              <span v-if="refSymbolCodes.length === 0" class="ref-empty">暂无参考产品</span>
            </div>
          </div>
        </el-form-item>

        <el-form-item label="仅上游激活">
          <el-switch v-model="form.upstream_only_activation" />
        </el-form-item>

        <el-form-item label="过期模式">
          <div class="expire-row">
            <el-radio-group v-model="form.expire_mode">
              <el-radio-button label="relative">relative</el-radio-button>
              <el-radio-button label="absolute">absolute</el-radio-button>
            </el-radio-group>
            <span v-if="form.expire_mode === 'relative'" class="expire-prefix">激活后</span>
            <el-select v-if="form.expire_mode === 'relative'" v-model="relativeExpirePreset" class="relative-expire-select">
              <el-option v-for="opt in relativeExpireOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
            <el-input
              v-else
              v-model="form.expire_at"
              class="absolute-expire-input"
              placeholder="例如 20260222 或 20260222 10:00（按浏览器时区）"
            />
            <span v-if="form.expire_mode === 'absolute'" class="timezone-note">当前时区：{{ browserTimeZone }}</span>
          </div>
        </el-form-item>

        <el-form-item>
          <el-space>
            <el-button type="primary" :loading="saving" @click="saveBasic">保存</el-button>
          </el-space>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.ref-input-stack {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ref-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.ref-empty {
  font-size: 12px;
  color: #9fb0c3;
}

.trade-type-row {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.trade-type-select {
  width: 260px;
  flex: 0 0 auto;
}

.required-inline {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.required-inline-item {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 8px;
  align-items: center;
  flex: 1 1 0;
  min-width: 0;
}

.required-type-tag {
  width: 100%;
  justify-content: center;
}

.required-code-input {
  width: 100%;
}

.required-inline-placeholder {
  visibility: hidden;
}

.expire-row {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.expire-prefix {
  color: #9fb0c3;
  font-size: 13px;
}

.relative-expire-select {
  width: 260px;
}

.absolute-expire-input {
  max-width: 360px;
}

.timezone-note {
  font-size: 12px;
  color: #9fb0c3;
  white-space: nowrap;
}

.mb-12 {
  margin-bottom: 12px;
}
</style>
