<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'

import { fetchMarkets, probeMarketData } from '../api/services'
import type { MarketDataProbePayload, MarketDataProbeResponse, MarketProfile } from '../api/types'

type ProbeForm = {
  code: string
  market: string
  contract_month: string
  start_local: string
  end_local: string
  bar_size: string
  what_to_show: string
  use_rth: boolean
  include_partial_bar: boolean
  max_bars: number | null
  page_size: number | null
}

function toLocalInputValue(date: Date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${year}-${month}-${day}T${hours}:${minutes}`
}

function toIso(localValue: string) {
  return new Date(localValue).toISOString()
}

const now = new Date()
const start = new Date(now.getTime() - 30 * 60 * 1000)
const BAR_SIZE_OPTIONS = [
  { label: '1 min', value: '1 min' },
  { label: '2 mins', value: '2 mins' },
  { label: '3 mins', value: '3 mins' },
  { label: '5 mins', value: '5 mins' },
  { label: '10 mins', value: '10 mins' },
  { label: '15 mins', value: '15 mins' },
  { label: '20 mins', value: '20 mins' },
  { label: '30 mins', value: '30 mins' },
  { label: '1 hour', value: '1 hour' },
  { label: '2 hours', value: '2 hours' },
  { label: '3 hours', value: '3 hours' },
  { label: '4 hours', value: '4 hours' },
  { label: '8 hours', value: '8 hours' },
  { label: '1 day', value: '1 day' },
  { label: '2 days', value: '2 days' },
]

const form = ref<ProbeForm>({
  code: 'SLV',
  market: 'US_STOCK',
  contract_month: '',
  start_local: toLocalInputValue(start),
  end_local: toLocalInputValue(now),
  bar_size: '1 min',
  what_to_show: 'TRADES',
  use_rth: true,
  include_partial_bar: true,
  max_bars: 30,
  page_size: 500,
})

const loading = ref(false)
const error = ref('')
const result = ref<MarketDataProbeResponse | null>(null)
const marketOptions = ref<MarketProfile[]>([])
const marketOptionsLoading = ref(false)
const marketOptionsError = ref('')

const bars = computed(() => result.value?.bars ?? [])
const metaText = computed(() =>
  JSON.stringify(result.value?.meta ?? {}, null, 2),
)
const requestText = computed(() =>
  JSON.stringify(result.value?.request ?? {}, null, 2),
)

function toErrorMessage(err: unknown) {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string' && detail) return `请求失败：${detail}`
    if (detail && typeof detail === 'object') return `请求失败：${JSON.stringify(detail)}`
  }
  return `请求失败：${String(err)}`
}

function marketLabel(profile: MarketProfile) {
  return `${profile.market} (${profile.sec_type}/${profile.exchange})`
}

async function loadMarkets() {
  marketOptionsLoading.value = true
  marketOptionsError.value = ''
  try {
    const rows = await fetchMarkets()
    marketOptions.value = rows
    const current = form.value.market.trim().toUpperCase()
    const first = rows[0]
    if (first && !rows.some((item) => item.market === current)) {
      form.value.market = first.market
    }
  } catch (err) {
    marketOptionsError.value = toErrorMessage(err).replace('请求失败：', '市场列表加载失败：')
  } finally {
    marketOptionsLoading.value = false
  }
}

async function runProbe() {
  loading.value = true
  error.value = ''
  try {
    const payload: MarketDataProbePayload = {
      code: form.value.code.trim().toUpperCase(),
      market: form.value.market.trim().toUpperCase(),
      contract_month: form.value.contract_month.trim() || null,
      start_time: toIso(form.value.start_local),
      end_time: toIso(form.value.end_local),
      bar_size: form.value.bar_size.trim(),
      what_to_show: form.value.what_to_show.trim(),
      use_rth: form.value.use_rth,
      include_partial_bar: form.value.include_partial_bar,
      max_bars: form.value.max_bars,
      page_size: form.value.page_size,
    }
    result.value = await probeMarketData(payload)
  } catch (err) {
    error.value = toErrorMessage(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadMarkets()
})
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">Market Data 测试</span>
          <el-button size="small" type="primary" :loading="loading" @click="runProbe">执行</el-button>
        </div>
      </template>

      <el-form label-width="140px" class="probe-form">
        <el-row :gutter="12">
          <el-col :xs="24" :md="8">
            <el-form-item label="代码(code)">
              <el-input v-model="form.code" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="市场(market)">
              <el-select
                v-model="form.market"
                style="width: 100%"
                filterable
                :loading="marketOptionsLoading"
                placeholder="请选择市场"
              >
                <el-option
                  v-for="item in marketOptions"
                  :key="item.market"
                  :label="marketLabel(item)"
                  :value="item.market"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="合约月(contract_month)">
              <el-input v-model="form.contract_month" placeholder="可选，例如 202606" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :xs="24" :md="6">
            <el-form-item label="开始时间">
              <el-input v-model="form.start_local" type="datetime-local" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="结束时间">
              <el-input v-model="form.end_local" type="datetime-local" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="bar_size">
              <el-select v-model="form.bar_size" style="width: 100%">
                <el-option
                  v-for="option in BAR_SIZE_OPTIONS"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="what_to_show">
              <el-input v-model="form.what_to_show" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :xs="24" :md="6">
            <el-form-item label="max_bars">
              <el-input-number v-model="form.max_bars" :min="1" :step="1" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="page_size">
              <el-input-number v-model="form.page_size" :min="1" :step="1" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="use_rth">
              <el-switch v-model="form.use_rth" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="6">
            <el-form-item label="include_partial_bar">
              <el-switch v-model="form.include_partial_bar" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <el-alert
        v-if="marketOptionsError"
        :title="marketOptionsError"
        type="warning"
        show-icon
        :closable="false"
        class="mb-12"
      />

      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <span class="card-title">返回结果</span>
      </template>
      <el-descriptions v-if="result" :column="2" border size="small" class="mb-12">
        <el-descriptions-item label="provider">{{ result.provider_class }}</el-descriptions-item>
        <el-descriptions-item label="bars 数量">{{ bars.length }}</el-descriptions-item>
      </el-descriptions>

      <el-table v-if="result" :data="bars" size="small" max-height="320">
        <el-table-column prop="ts" label="ts" min-width="180" />
        <el-table-column prop="open" label="open" min-width="90" />
        <el-table-column prop="high" label="high" min-width="90" />
        <el-table-column prop="low" label="low" min-width="90" />
        <el-table-column prop="close" label="close" min-width="90" />
        <el-table-column prop="volume" label="volume" min-width="100" />
      </el-table>

      <el-row v-if="result" :gutter="12" class="mt-12">
        <el-col :xs="24" :md="12">
          <div class="json-title">request</div>
          <pre class="json-box">{{ requestText }}</pre>
        </el-col>
        <el-col :xs="24" :md="12">
          <div class="json-title">meta</div>
          <pre class="json-box">{{ metaText }}</pre>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<style scoped>
.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.probe-form {
  margin-bottom: 12px;
}

.mb-12 {
  margin-bottom: 12px;
}

.mt-12 {
  margin-top: 12px;
}

.json-title {
  margin-bottom: 6px;
  color: #a8bed3;
  font-size: 12px;
}

.json-box {
  margin: 0;
  padding: 10px;
  border-radius: 6px;
  background: #0f1d2e;
  color: #d8e6f6;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
