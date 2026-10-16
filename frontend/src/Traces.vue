<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from './Sidebar.vue'
import TraceDetail from './TraceDetail.vue'
import { useChatStore } from './store'
const store = useChatStore(), route = useRoute(), rows = ref<any[]>([]), trace = ref<any>(null), error = ref(''), offset = ref(0)
async function load() { error.value = ''; try { const r = await fetch(`/api/traces?limit=30&offset=${offset.value}`, { headers: store.headers() }); if (!r.ok) throw new Error(`追踪读取失败：${r.status}`); rows.value = (await r.json()).items } catch (e) { error.value = String(e) } }
async function open(id: string) { error.value = ''; trace.value = null; try { const r = await fetch(`/api/traces/${id}`, { headers: store.headers() }); if (!r.ok) throw new Error(`详情读取失败：${r.status}`); trace.value = await r.json() } catch (e) { error.value = String(e) } }
onMounted(async () => { await load(); if (typeof route.query.id === 'string') await open(route.query.id) })
</script>
<template><div class="workspace"><Sidebar/><main class="management-main"><header><div><h1>请求追踪</h1><p>冻结配置、排名、工具、审核与失败均可回查。</p></div></header><section class="management-content"><label>本地访问令牌 <input v-model="store.token" type="password" autocomplete="off"></label> <button @click="load">刷新</button><p v-if="error" class="message-error">{{ error }}</p><p v-if="!rows.length">暂无可读取的追踪。</p><div class="trace-layout"><div><button v-for="row in rows" :key="row.id" class="trace-row" @click="open(row.id)"><strong>{{ row.question }}</strong><small>{{ row.status }} · {{ row.id.slice(0, 8) }} · {{ row.started_at }}</small></button><button :disabled="offset === 0" @click="offset = Math.max(0, offset - 30); load()">上一页</button><button :disabled="rows.length < 30" @click="offset += 30; load()">下一页</button></div><TraceDetail :trace="trace"/></div></section></main></div></template>
