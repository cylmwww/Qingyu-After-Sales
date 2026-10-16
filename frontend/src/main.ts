import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import App from './App.vue'
import Chat from './Chat.vue'
import Traces from './Traces.vue'
import Knowledge from './Knowledge.vue'
import './style.css'
const router = createRouter({ history: createWebHistory(), routes: [{ path: '/', component: Chat }, { path: '/traces', component: Traces }, { path: '/knowledge', component: Knowledge }] })
createApp(App).use(createPinia()).use(router).use(TDesign).mount('#app')
