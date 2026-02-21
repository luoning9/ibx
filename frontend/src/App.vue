<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const menuActive = computed(() => {
  if (route.path.startsWith('/strategies')) return '/strategies'
  if (route.path.startsWith('/events')) return '/events'
  if (route.path.startsWith('/positions')) return '/positions'
  if (route.path.startsWith('/trade-instructions')) return '/trade-instructions'
  return '/strategies'
})

function handleMenuSelect(index: string) {
  if (index !== route.path) router.push(index)
}
</script>

<template>
  <el-container class="app-shell">
    <el-header class="top-nav">
      <div class="brand">IBX Console</div>
      <el-menu
        :default-active="menuActive"
        mode="horizontal"
        class="menu"
        :ellipsis="false"
        @select="handleMenuSelect"
      >
        <el-menu-item index="/strategies">策略列表</el-menu-item>
        <el-menu-item index="/events">运行事件</el-menu-item>
        <el-menu-item index="/positions">持仓情况</el-menu-item>
        <el-menu-item index="/trade-instructions">交易指令</el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>
