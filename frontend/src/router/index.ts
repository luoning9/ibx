import { createRouter, createWebHashHistory } from 'vue-router'
import EventsView from '../views/EventsView.vue'
import PositionsView from '../views/PositionsView.vue'
import StrategyDetailView from '../views/StrategyDetailView.vue'
import StrategiesView from '../views/StrategiesView.vue'
import TradeInstructionsView from '../views/TradeInstructionsView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/strategies' },
    { path: '/strategies', name: 'strategies', component: StrategiesView },
    { path: '/strategies/:id', name: 'strategy-detail', component: StrategyDetailView },
    { path: '/events', name: 'events', component: EventsView },
    { path: '/positions', name: 'positions', component: PositionsView },
    {
      path: '/trade-instructions',
      name: 'trade-instructions',
      component: TradeInstructionsView,
    },
  ],
})

export default router
