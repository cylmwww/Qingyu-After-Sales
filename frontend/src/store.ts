import { defineStore } from 'pinia'
import { SSEParser } from './sse'
export function factLabel(field: string): string {
  const labels: Record<string, string> = { order_id: '订单号', category_id: '商品品类', return_reason: '退货原因',
    selections: '退货商品与数量', after_sales_applied_at: '申请时刻', signed_at: '签收时刻', purchased_at: '购买时刻',
    gift_returned_line_ids: '赠品返还情况', tags_intact: '吊牌状态', product_intact: '商品完好情况',
    used_outdoors: '外出使用情况', packaging_intact: '包装状态', activated: '激活状态', accessories_complete: '配件完整情况' }
  return labels[field] || '必要业务条件'
}
const toolProgress: Record<string, string> = { search_knowledge: '正在检索政策依据', read_policy_context: '正在查阅适用条款',
  get_order: '正在查询订单', get_after_sales: '正在查询售后与退款记录', get_logistics: '正在查询出库物流', calculate_refund: '正在核算退款预估' }
export type Evidence = { citation_id: string; chunk_id: string; document_id: string; document_version: string; text: string; rank?: number; cosine_similarity?: number; rerank_score?: number; bm25_score?: number; fusion_score?: number; query_rrf_score?: number; score_type?: string; context_kind?: string; source_path: string; metadata: Record<string, unknown> }
export type Message = { role: 'user' | 'assistant'; content: string; traceId?: string; evidence?: Evidence[]; state?: string; error?: string; mode?: string; runtimeVersion?: string; coverage?: string }
export const useChatStore = defineStore('chat', {
  state: () => ({ messages: [] as Message[], busy: false, status: '', mode: '', indexId: '', token: '', category: '', question: '',
    agentMode: true, sessionId: sessionStorage.getItem('qingyu-session') || '', taskId: sessionStorage.getItem('qingyu-task') || '',
    taskState: '', orderId: '', missing: [] as string[], clarificationFacts: '{}', progress: '',
    returnReason: '', applicationTime: '', lineId: '', unitIndices: '1', orderLines: [] as any[],
    pendingRequest: sessionStorage.getItem('qingyu-pending') || '', sessions: [] as any[] }),
  actions: {
    headers(): Record<string, string> { return this.token ? { Authorization: `Bearer ${this.token}` } : {} },
    async loadSessions() {
      try { const r = await fetch('/api/agent/sessions', { headers: this.headers() }); if (!r.ok) throw new Error(`会话读取失败：${r.status}`); this.sessions = (await r.json()).items } catch (e) { this.status = String(e) }
    },
    async newSession() {
      try { const r = await fetch('/api/agent/sessions', { method: 'POST', headers: this.headers() }); if (!r.ok) throw new Error(`新建会话失败：${r.status}`); await this.selectSession((await r.json()).session_id); await this.loadSessions() } catch (e) { this.status = String(e) }
    },
    async selectSession(id: string) {
      if (!id || this.busy) return
      this.sessionId = id; this.taskId = ''; this.taskState = ''; this.orderId = ''; this.orderLines = []; this.messages = []; this.progress = ''; this.clearTaskInputs()
      sessionStorage.setItem('qingyu-session', id); sessionStorage.removeItem('qingyu-task')
      try {
        const response = await fetch(`/api/agent/sessions/${id}`, { headers: this.headers() })
        if (!response.ok) throw new Error(`会话读取失败：${response.status}`)
        this.taskId = (await response.json()).tasks[0]?.id || ''
        if (this.taskId) { sessionStorage.setItem('qingyu-task', this.taskId); await this.reconnect() }
        else this.status = '新会话已就绪，可提交问题'
      } catch (e) { this.status = String(e) }
    },
    clearTaskInputs() { this.missing = []; this.clarificationFacts = '{}'; this.returnReason = ''; this.applicationTime = ''; this.lineId = ''; this.pendingRequest = ''; sessionStorage.removeItem('qingyu-pending') },
    async consume(response: Response, message: Message) {
      if (!response.body) throw new Error('浏览器无法读取响应流')
      const reader = response.body.getReader(), decoder = new TextDecoder(), parser = new SSEParser()
      let terminal = false
      while (true) {
        const { value, done } = await reader.read()
        for (const event of parser.feed(decoder.decode(value, { stream: !done }))) {
          const data = event.data as Record<string, any>
          if (event.event === 'submission') { terminal = false; message.error = undefined; message.content = ''; message.evidence = [] }
          if (event.event === 'status') { this.status = data.message; message.mode = data.mode || message.mode }
          if (event.event === 'route') { this.progress = '正在分析诉求'; this.taskState = 'running' }
          if (event.event === 'runtime_dispatch') message.runtimeVersion = data.runtime_version
          if (event.event === 'agent_decision') this.progress = '正在根据已有资料决定下一步'
          if (event.event === 'observation_applied') this.progress = data.reused ? '已复用查询结果，正在核对信息缺口' : '已保存查询结果，正在核对依据'
          if (event.event === 'candidate_created') this.progress = '正在审核候选回答'
          if (event.event === 'done') { message.runtimeVersion = data.runtime_version || message.runtimeVersion; message.coverage = data.coverage }
          if (event.event === 'plan') this.progress = `计划 ${data.version} · ${data.steps.length} 个步骤`
          if (event.event === 'tool_start') this.progress = toolProgress[data.tool] || '正在查询必要资料'
          if (event.event === 'tool_result' && data.tool === 'get_order' && data.result.status === 'success') this.orderLines = data.result.data.lines
          if (event.event === 'replan') this.progress = '正在根据查询结果调整步骤'
          if (event.event === 'candidate_reused') this.progress = '正在复审已有候选与定位依据，尚未发布'
          if (event.event === 'review_result') this.progress = data.passed ? '检查完成，正在准备发布' : data.execution === 'incomplete' ? '审核未完成，候选未发布' : data.issue_count ? `检查发现 ${data.issue_count} 项问题，候选未发布` : '未满足发布条件，候选未发布'
          if (event.event === 'correction_start') this.progress = `正在修复 · 第 ${data.round} 轮`
          if (event.event === 'review_failure') this.progress = '审核未完成，候选答案已暂存'
          if (event.event === 'clarification') { terminal = true; this.taskState = 'waiting'; this.missing = data.missing; message.state = 'waiting'; message.content = `${data.message}：${(data.missing as string[]).map(factLabel).join('、')}`; this.status = '等待补充条件'; this.progress = '' }
          if (event.event === 'delta') message.content += data.text
          if (event.event === 'evidence') message.evidence = data.items
          if (event.event === 'done') { terminal = true; message.content = data.answer; message.evidence = data.evidence || message.evidence; message.state = 'completed'; message.mode = data.mode; message.error = undefined; this.taskState = 'completed'; this.missing = []; this.clarificationFacts = '{}'; this.status = data.outcome === 'partial_answer' ? '已回答可确认部分，仍需核查' : data.outcome === 'handoff' ? '需要人工核查（未提交工单）' : data.reviewed ? `完成 · ${data.mode === 'mock' ? '模拟' : ''}审核通过${data.correction_rounds ? ' · 已修复' : ''}` : data.prechecked ? '完成 · 已前置检查，未后置审核' : '完成 · 未启用审核'; this.progress = '' }
          if (event.event === 'error') {
            terminal = true; this.progress = ''
            if (data.code === 'user_cancelled') {
              message.state = 'cancelled'; message.error = undefined; message.content = '任务已取消。可提交新问题。'; message.evidence = []
              this.taskState = 'cancelled'; this.clearTaskInputs(); this.status = '任务已取消，可提交新问题'
            } else { message.state = 'failed'; message.error = data.code; this.taskState = 'failed'; this.status = '请求未完成，需显式恢复或取消' }
          }
        }
        if (done) break
      }
      if (!terminal) throw new Error('连接中断，结果尚未确认；请重新连接检查后台状态。')
    },
    async send() {
      if (this.busy || !this.question.trim()) return
      const question = this.question.trim(); this.question = ''
      this.messages.push({ role: 'user', content: question }, { role: 'assistant', content: '', evidence: [], state: 'running' })
      const message = this.messages[this.messages.length - 1]
      this.busy = true; this.status = '连接服务中'
      try {
        let body: Record<string, unknown> = { question, ...(this.category ? { category_id: this.category } : {}) }
        if (this.agentMode) {
          if (!this.sessionId) {
            const response = await fetch('/api/agent/sessions', { method: 'POST', headers: this.headers() })
            if (!response.ok) throw new Error(`无法建立模拟客户会话：${response.status}`)
            this.sessionId = (await response.json()).session_id; sessionStorage.setItem('qingyu-session', this.sessionId)
          }
          const supplied = JSON.parse(this.clarificationFacts || '{}')
          if (!supplied || Array.isArray(supplied) || typeof supplied !== 'object') throw new Error('补充条件需要JSON对象')
          const action = this.taskState === 'waiting' ? 'clarify' : ['failed', 'interrupted'].includes(this.taskState) ? 'resume' : 'new'
          body = { question, session_id: this.sessionId, request_id: crypto.randomUUID(), action,
            ...(this.taskId && action !== 'new' ? { task_id: this.taskId } : {}),
            facts: { ...supplied, ...(this.orderId ? { order_id: this.orderId } : {}), ...(this.category ? { category_id: this.category } : {}),
              ...(this.returnReason ? { return_reason: this.returnReason } : {}),
              ...(this.applicationTime ? { after_sales_applied_at: this.applicationTime } : {}),
              ...(this.lineId ? { selections: [{ line_id: this.lineId, unit_indices: this.unitIndices.split(',').map(s => Number(s.trim())) }] } : {}) } }
          this.pendingRequest = JSON.stringify(body); sessionStorage.setItem('qingyu-pending', this.pendingRequest)
        }
        const response = await fetch(this.agentMode ? '/api/agent/chat' : '/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', ...this.headers() }, body: JSON.stringify(body) })
        if (!response.ok) throw new Error(`服务返回 ${response.status}：${await response.text()}`)
        message.traceId = response.headers.get('X-Trace-ID') || undefined
        if (this.agentMode && message.traceId) { this.taskId = message.traceId; sessionStorage.setItem('qingyu-task', this.taskId); this.pendingRequest = ''; sessionStorage.removeItem('qingyu-pending') }
        await this.consume(response, message)
      } catch (error) { message.state = 'failed'; message.error = String(error); this.status = '请求未完成，可重新连接检查后台状态' }
      finally { this.busy = false }
    },
    async reconnect() {
      if ((!this.taskId && !this.sessionId) || this.busy) return
      this.busy = true
      try {
        if (!this.taskId) {
          const session = await fetch(`/api/agent/sessions/${this.sessionId}`, { headers: this.headers() })
          if (!session.ok) throw new Error(`无法读取会话：${session.status}`)
          this.taskId = (await session.json()).tasks[0]?.id || ''
          if (!this.taskId) throw new Error('会话还没有已保存任务，可重试提交')
          sessionStorage.setItem('qingyu-task', this.taskId)
        }
        const taskResponse = await fetch(`/api/agent/tasks/${this.taskId}`, { headers: this.headers() })
        if (!taskResponse.ok) throw new Error(`无法读取任务：${taskResponse.status}`)
        const task = await taskResponse.json(); this.taskState = task.status
        if (task.facts.order_id) this.orderId = task.facts.order_id.value
        const response = await fetch(`/api/traces/${this.taskId}/events`, { headers: this.headers() })
        if (!response.ok) throw new Error(`重连失败：${response.status}`)
        this.messages.push({ role: 'assistant', content: '', traceId: this.taskId, state: task.status })
        await this.consume(response, this.messages[this.messages.length - 1])
      } catch (error) { this.status = String(error) } finally { this.busy = false }
    },
    async cancel() {
      if (!this.taskId) return
      const response = await fetch(`/api/agent/tasks/${this.taskId}/cancel`, { method: 'POST', headers: this.headers() })
      if (response.ok) { this.taskState = 'cancelled'; this.status = '任务已取消，可提交新问题'; this.clearTaskInputs() }
      else this.status = `取消失败：${response.status}`
    },
    async retrySubmission() {
      if (!this.pendingRequest || this.busy) return
      this.busy = true
      try {
        const response = await fetch('/api/agent/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', ...this.headers() }, body: this.pendingRequest })
        if (!response.ok) throw new Error(`重试提交失败：${response.status}`)
        this.taskId = response.headers.get('X-Task-ID') || ''; sessionStorage.setItem('qingyu-task', this.taskId)
        this.pendingRequest = ''; sessionStorage.removeItem('qingyu-pending')
        this.messages.push({ role: 'assistant', content: '', traceId: this.taskId })
        await this.consume(response, this.messages[this.messages.length - 1])
      } catch (error) { this.status = String(error) } finally { this.busy = false }
    }
  }
})
