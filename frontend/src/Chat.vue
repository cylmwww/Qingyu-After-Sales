<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useChatStore, factLabel, type Evidence } from './store'
import Sidebar from './Sidebar.vue'
import TraceDetail from './TraceDetail.vue'
const store = useChatStore()
const selected = ref<Evidence | null>(null), drawer = ref(false), traceDrawer = ref(false), trace = ref<any>(null)
const timeline = ref<HTMLElement | null>(null), healthError = ref('')
const reviewConfig = ref({ precheck: false, postcheck: false, correction: 'none' })
const reviewEvents = computed(() => trace.value?.events?.filter((e: any) => ['review_result','review_failure','answer_repair','correction_start','correction_evidence','review_exit'].includes(e.kind)) || [])
function reviewLabel(entry: any) {
  if (entry.kind === 'review_result') return entry.payload.stage === 'pre' ? '前置证据检查' : '后置答案审核'
  return ({ review_failure: '审核失败', answer_repair: '答案修复前后', correction_start: '纠错原因与预算', correction_evidence: '补查与新增证据', review_exit: '未解决问题与退出' } as Record<string, string>)[entry.kind] || entry.kind
}
const examples = ['鞋盒扔了，但没穿出门，还能无理由退吗？', '耳机拆封了但没激活，退货条件是什么？', '定制商品有质量问题，怎么处理？']
const html = (text: string) => DOMPurify.sanitize(marked.parse(text, { async: false }) as string, { FORBID_TAGS: ['img'], FORBID_ATTR: ['style'] })
const badge = computed(() => store.mode === 'mock' ? '模拟链路 · 非真实模型' : '只读业务与政策助手')
function evidenceScore(item: Evidence) {
  if (item.context_kind === 'parent' || item.context_kind === 'related') return '上下文补充'
  const score = item.score_type === 'rerank' ? item.rerank_score : item.score_type === 'query_rrf' ? item.query_rrf_score
    : item.score_type === 'rrf' ? item.fusion_score : item.score_type === 'bm25' ? item.bm25_score : item.cosine_similarity
  return typeof score === 'number' && Number.isFinite(score) ? `${item.score_type || '检索分数'} ${score.toFixed(4)}` : '无检索分数'
}
function openEvidence(item: Evidence) { selected.value = item; drawer.value = true }
async function openTrace(id?: string) {
  if (!id) return
  traceDrawer.value = true
  trace.value = null
  try {
    const response = await fetch(`/api/traces/${id}`, { headers: store.headers() })
    if (!response.ok) throw new Error(`无法读取追踪：${response.status}`)
    trace.value = await response.json()
  } catch (error) { trace.value = { error: String(error) } }
}
async function refreshHealth() {
  try {
    const response = await fetch('/api/health')
    if (!response.ok) throw new Error('后端或数据库未就绪')
    const data = await response.json()
    store.mode = data.mode; store.indexId = data.index_id || ''
    reviewConfig.value = data.review_configuration || reviewConfig.value
    healthError.value = !data.index_id ? '尚未入库，请先运行知识导入命令。' : !data.index_compatible ? '当前模型配置与索引不兼容，请重新入库。' : ''
  } catch { healthError.value = '服务连接失败，请检查后端启动状态。' }
}
onMounted(refreshHealth)
watch(() => store.orderId, (value, previous) => {
  if (value && previous && value !== previous) {
    store.lineId = ''; store.returnReason = ''; store.applicationTime = ''; store.clarificationFacts = '{}'; store.category = ''
  }
})
watch(() => store.messages.map(m => m.content).join(''), async () => { await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight }) })
</script>

<template>
  <div class="workspace">
    <Sidebar/>
    <main>
      <header><div><h1>政策问答</h1><p>先找到依据，再解释规则。</p></div><t-tag :theme="store.mode === 'mock' ? 'warning' : 'success'" variant="light">{{ badge }}</t-tag></header>
      <div class="notice">订单、售后和物流均为模拟记录；身份由服务器绑定。查询与退款预估不代表审批或到账。<span v-if="store.agentMode">前置检查{{ reviewConfig.precheck ? '开启' : '关闭' }}，后置审核{{ reviewConfig.postcheck ? '开启（通过后发布）' : '关闭' }}，纠错{{ { none: '关闭', answer_only: '仅修改答案', targeted_lookup: '允许定向补查' }[reviewConfig.correction as 'none' | 'answer_only' | 'targeted_lookup'] }}。</span><span v-else>单轮模式未启用独立审核。</span></div>
      <div v-if="store.progress" class="notice" aria-live="polite">{{ store.progress }}</div>
      <div v-if="healthError" class="error-banner">{{ healthError }} <button @click="refreshHealth">重新检查</button></div>
      <section ref="timeline" class="conversation" aria-live="polite">
        <div v-if="!store.messages.length" class="welcome"><div class="welcome-icon">✦</div><h2>有什么售后规则想了解？</h2><p>描述商品状态与相关时间，回答中的引用可以展开查看原文。</p><div class="examples"><button v-for="example in examples" :key="example" @click="store.question = example">{{ example }} <span>↗</span></button></div></div>
        <article v-for="(message, index) in store.messages" :key="index" :class="['message', message.role]">
          <div class="avatar">{{ message.role === 'user' ? '你' : '屿' }}</div><div class="message-body"><div class="message-label">{{ message.role === 'user' ? '你的问题' : '青屿政策助手' }} <small v-if="message.mode === 'mock'">模拟输出</small></div>
            <div v-if="message.role === 'user'" class="plain-text">{{ message.content }}</div><div v-else class="markdown" v-html="html(message.content || (store.busy ? '正在检索政策依据…' : '未生成回答'))"></div>
            <div v-if="message.error" class="message-error">请求未完成：{{ message.error }}。已有文字可能是不完整输出，不应作为确定结论。</div>
            <div v-if="message.evidence?.length" class="citations"><span>检索证据</span><button v-for="item in message.evidence" :key="item.citation_id" @click="openEvidence(item)">[{{ item.citation_id }}] {{ item.metadata.title }} <small>v{{ item.document_version }}</small></button></div>
            <button v-if="message.traceId" class="trace-link" @click="openTrace(message.traceId)">查看请求追踪 · {{ message.traceId.slice(0, 8) }}</button>
          </div>
        </article>
      </section>
      <div class="notice">
        <label><input v-model="store.agentMode" type="checkbox" :disabled="store.busy || ['running','waiting','failed','interrupted'].includes(store.taskState)"> 只读业务模式（关闭后使用原单轮 RAG）</label>
        <button :disabled="store.busy" @click="store.newSession">新建会话</button>
        <button :disabled="store.busy" @click="store.loadSessions">读取会话</button>
        <select aria-label="选择会话" :disabled="store.busy" @change="store.selectSession(($event.target as HTMLSelectElement).value)"><option value="">切换已保存会话</option><option v-for="session in store.sessions" :key="session.id" :value="session.id">{{ session.id.slice(0,8) }} · {{ session.created_at }}</option></select>
        <label v-if="store.agentMode">订单 <input v-model="store.orderId" placeholder="ORD-006" :disabled="store.busy"></label>
        <button v-if="store.taskId || store.sessionId" type="button" :disabled="store.busy" @click="store.reconnect">重新连接</button>
        <button v-if="store.pendingRequest" type="button" :disabled="store.busy" @click="store.retrySubmission">重试原提交</button>
        <button v-if="store.taskId && !['completed','cancelled'].includes(store.taskState)" type="button" @click="store.cancel">取消任务</button>
        <div v-if="store.missing.length">待补充：{{ store.missing.map(factLabel).join('、') }}。请通过下方表单确认条件。</div>
        <div v-if="store.missing.includes('return_reason')"><label>退货原因 <select v-model="store.returnReason"><option value="">请选择</option><option value="no_reason">无理由</option><option value="quality">质量问题（不代表已核实）</option></select></label></div>
        <div v-if="store.missing.includes('after_sales_applied_at')"><label>申请时刻 <input v-model="store.applicationTime" placeholder="2026-10-09T10:00:00+08:00"></label></div>
        <div v-if="store.missing.includes('selections')"><label>退货商品 <select v-model="store.lineId"><option value="">请选择订单行</option><option v-for="line in store.orderLines.filter(l => !l.gift_for_line_id)" :key="line.id" :value="line.id">{{ line.id }}（{{ line.quantity }}件）</option></select></label><label>商品单元序号 <input v-model="store.unitIndices" placeholder="1 或 1,2"></label></div>
        <details v-if="store.agentMode"><summary>确认或修正业务条件</summary><p>仅提交已确认的条件；申请时间需含时区。商品单元使用订单行ID与从1开始的序号。</p><textarea v-model="store.clarificationFacts" aria-label="确认业务条件JSON" rows="3" placeholder='{"return_reason":"no_reason","after_sales_applied_at":"2026-10-09T10:00:00+08:00"}' :disabled="store.busy"></textarea></details>
        <span v-if="['failed','interrupted'].includes(store.taskState)">发送补充说明将显式恢复当前任务，或先取消后提新问题。</span>
      </div>
      <footer><form @submit.prevent="store.send"><div class="composer-options"><label>商品范围 <select v-model="store.category" :disabled="store.busy"><option value="">全部品类（不推测）</option><option value="apparel_footwear">服饰鞋靴</option><option value="digital_accessories">数码配件</option><option value="customized">定制商品</option></select></label><label class="token-label">本地访问令牌 <input v-model="store.token" type="password" placeholder="仅保存在当前页面" autocomplete="off"></label></div><div class="composer"><textarea v-model="store.question" aria-label="输入售后政策问题" placeholder="例如：鞋盒不在了，其他状态完整，签收第六天申请能退吗？" rows="2" :disabled="store.busy" @keydown.ctrl.enter.prevent="store.send"></textarea><t-button type="submit" :loading="store.busy" :disabled="!store.question.trim() || !!healthError">发送问题</t-button></div><div class="composer-hint"><span>{{ store.status || '请勿输入真实客户信息或密钥。' }}</span><span>Ctrl + Enter 发送</span></div></form></footer>
    </main>
    <t-drawer v-model:visible="drawer" header="引用证据" size="min(520px, 95vw)" :footer="false"><div v-if="selected" class="evidence-detail"><t-tag theme="primary">{{ selected.citation_id }} · v{{ selected.document_version }}</t-tag><h2>{{ selected.metadata.title }}</h2><dl><dt>文档</dt><dd>{{ selected.document_id }}</dd><dt>片段</dt><dd>{{ selected.chunk_id }}</dd><dt>来源</dt><dd>{{ selected.source_path }}</dd><dt>检索排名 / 分数</dt><dd>{{ selected.rank ?? '—' }} / {{ evidenceScore(selected) }}（不代表答案置信度）</dd></dl><h3>检索原文</h3><div class="markdown" v-html="html(selected.text)"></div><h3>条件、版本与来源</h3><pre>{{ JSON.stringify(selected.metadata, null, 2) }}</pre></div></t-drawer>
    <t-drawer v-model:visible="traceDrawer" header="请求追踪（开发者视图）" size="min(720px, 95vw)" :footer="false"><p>包含冻结配置、候选排名、实际上下文、模型调用和错误；用量未知时为 null。在线通过不等于离线正确。</p><section v-if="reviewEvents.length"><h3>检查与修复详情</h3><p>候选及修复稿仅供追踪核查，未通过的稿件不能作为最终回答。</p><details v-for="entry in reviewEvents" :key="entry.sequence"><summary>{{ reviewLabel(entry) }} · 第 {{ entry.payload.round ?? 0 }} 轮 · {{ entry.payload.elapsed_ms ? Math.round(entry.payload.elapsed_ms) + 'ms' : '' }}</summary><pre class="trace-json">{{ JSON.stringify(entry.payload, null, 2) }}</pre></details></section><TraceDetail :trace="trace"/><router-link v-if="trace?.id" :to="`/traces?id=${trace.id}`">在追踪页查看</router-link></t-drawer>
  </div>
</template>
