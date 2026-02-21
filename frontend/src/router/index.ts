import { createRouter, createWebHashHistory } from 'vue-router'
import EventsView from '../views/EventsView.vue'
import PositionsView from '../views/PositionsView.vue'
import StrategyDetailView from '../views/StrategyDetailView.vue'
import StrategyEditorActionsView from '../views/StrategyEditorActionsView.vue'
import StrategyEditorBasicView from '../views/StrategyEditorBasicView.vue'
import StrategyEditorConditionsView from '../views/StrategyEditorConditionsView.vue'
import StrategiesView from '../views/StrategiesView.vue'
import TradeInstructionsView from '../views/TradeInstructionsView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/strategies' },
    { path: '/strategies', name: 'strategies', component: StrategiesView },
    { path: '/strategies/new', name: 'strategy-create', component: StrategyEditorBasicView },
    { path: '/strategies/:id', name: 'strategy-detail', component: StrategyDetailView },
    {
      path: '/strategies/:id/edit/basic',
      name: 'strategy-edit-basic',
      component: StrategyEditorBasicView,
    },
    {
      path: '/strategies/:id/edit/conditions',
      name: 'strategy-edit-conditions',
      component: StrategyEditorConditionsView,
    },
    {
      path: '/strategies/:id/edit/actions',
      name: 'strategy-edit-actions',
      component: StrategyEditorActionsView,
    },
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
