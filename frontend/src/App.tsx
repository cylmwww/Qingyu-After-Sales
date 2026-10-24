import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, BookOpen, CircleHelp, Menu, PackageSearch, PanelLeftOpen, ReceiptText, RotateCcw, X } from "lucide-react";
import { checkHealth } from "./lib/api";
import { useChat } from "./hooks/useChat";
import { Composer } from "./components/Composer";
import { QingyuMark } from "./components/QingyuMark";
import { MessageView } from "./components/MessageView";
import { SearchDialog } from "./components/SearchDialog";
import { Sidebar } from "./components/Sidebar";
import type { ActionProposal } from "./types";

const SUGGESTIONS = [
  { icon: BookOpen, title: "退换货政策", detail: "核对条件，找到适用条款", prompt: "定制商品过了七天，但有质量问题还能退吗？" },
  { icon: PackageSearch, title: "订单与物流", detail: "查询订单，跟进配送进度", prompt: "帮我查一下 ORD10001 的物流进度" },
  { icon: ReceiptText, title: "支付与发票", detail: "了解付款方式和开票时间", prompt: "电子发票一般多久可以开具？" },
  { icon: RotateCcw, title: "售后处理", detail: "了解申请流程与所需材料", prompt: "退货申请需要提供哪些信息？" },
];

function App() {
  const chat = useChat();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [theme, setTheme] = useState<"light" | "dark">(() => localStorage.getItem("qingyu-new:theme") === "dark" ? "dark" : "light");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const helpRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("qingyu-new:theme", theme);
  }, [theme]);
  useEffect(() => {
    const controller = new AbortController();
    void checkHealth(controller.signal).then(setHealthy);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && !helpRef.current?.open) { event.preventDefault(); setSearchOpen(true); }
      if (event.key === "Escape") { setSearchOpen(false); setSidebarOpen(false); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function newChat() {
    chat.select(null);
    setDraft("");
    setSidebarOpen(false);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  function selectChat(id: string) { chat.select(id); setSidebarOpen(false); setSearchOpen(false); }
  function suggest(prompt: string) { setDraft(prompt); inputRef.current?.focus(); }
  function confirm(action: ActionProposal, messageId: string) {
    if (!chat.active) return;
    const conversationId = chat.active.id;
    void chat.send("确认执行", action.action_id).then((succeeded) => {
      if (succeeded) chat.dismissAction(conversationId, messageId);
    });
  }

  return <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
    <Sidebar conversations={chat.conversations} activeId={chat.active?.id ?? null} open={sidebarOpen} onClose={() => { setSidebarOpen(false); setSidebarCollapsed(true); }} onNew={newChat} onSelect={selectChat} onRemove={chat.remove} onSearch={() => setSearchOpen(true)} onTheme={() => setTheme(theme === "light" ? "dark" : "light")} theme={theme} />
    <main className="main-panel">
      <header className="topbar">
        <button className="icon-button open-sidebar" aria-label="打开侧栏" title="打开侧栏" onClick={() => { setSidebarCollapsed(false); setSidebarOpen(true); }}>{sidebarCollapsed ? <PanelLeftOpen size={20} /> : <Menu size={20} />}</button>
        <div className="page-title"><span className="model-title">客服工作台</span><span className="page-subtitle">{chat.active ? "对话进行中" : "从一个问题开始"}</span></div>
        <div className="topbar-right"><span className={`connection-pill ${healthy === false ? "offline" : healthy === null ? "connecting" : ""}`} role="status"><span />{healthy === null ? "连接中" : healthy ? "服务已连接" : "服务未连接"}</span><button className="icon-button help-button" title="使用说明" aria-label="使用说明" onClick={() => helpRef.current?.showModal()}><CircleHelp size={19} /></button></div>
      </header>
      {healthy === false && <div className="service-banner" role="status"><strong>暂时无法连接服务</strong><span>请确认后端已启动，然后刷新页面。</span></div>}
      {chat.active ? <div className="chat-content"><MessageView conversation={chat.active} busy={chat.busy} onConfirm={confirm} onCancel={(messageId) => chat.dismissAction(chat.active!.id, messageId)} /></div>
        : <div className="welcome"><div className="welcome-inner">
            <div className="welcome-brand"><QingyuMark /><span>轻语，为每一次咨询找到依据。</span></div>
            <h1>把问题说清楚，<br />让服务更进一步。</h1>
            <p>查政策、追订单、办售后。<br className="mobile-break" />从这里开始一段有据可循的对话。</p>
            <div className="suggestions" aria-label="常用咨询">{SUGGESTIONS.map((item) => <button key={item.title} onClick={() => suggest(item.prompt)}><span className="suggestion-icon"><item.icon size={20} /></span><span><strong>{item.title}</strong><small>{item.detail}</small></span><ArrowUpRight size={16} /></button>)}</div>
            <div className="welcome-note"><span />知识回答可查看来源，业务操作先由你确认。</div>
          </div></div>}
      <Composer busy={chat.busy} onSend={(message) => void chat.send(message)} onStop={chat.stop} draft={draft} onDraft={setDraft} inputRef={inputRef} />
    </main>
    {searchOpen && <SearchDialog conversations={chat.conversations} onClose={() => setSearchOpen(false)} onSelect={selectChat} />}
    <dialog ref={helpRef} className="help-dialog" aria-labelledby="help-title">
      <div className="help-heading"><h2 id="help-title">在轻语开始咨询</h2><button className="icon-button" aria-label="关闭说明" onClick={() => helpRef.current?.close()}><X size={20} /></button></div>
      <p>直接描述你的问题，也可以选择首页的常用咨询，把示例填入输入框后再发送。</p>
      <ul><li>政策回答下方可以查看来源，请核对重要条款。</li><li>订单示例使用 ORD10001、ORD10002、ORD10003，需要先初始化演示数据。</li><li>业务写操作会展示确认卡片，确认后才执行。</li><li>对话列表保存在当前浏览器，清除浏览器数据后无法恢复。</li></ul>
      <p className="help-footnote">qingyu-new · 当前工作台使用演示身份，尚未接入登录系统。</p>
    </dialog>
  </div>;
}

export default App;
