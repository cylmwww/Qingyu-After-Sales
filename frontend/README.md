# qingyu-new Web · 轻语客服工作台

React、TypeScript 与 Vite 实现的对话前端，采用奶油白与靛蓝色调。支持深色模式、来源引用、流式回答、本地会话搜索和业务确认卡片。

## 本地开发

需要 Node.js `^20.19.0` 或 `>=22.12.0`。先按[启动说明](../docs/setup.md)启动 8002 端口的 API，再在本目录执行：

```bash
npm ci
```

```bash
npm run dev
```

访问 `http://127.0.0.1:5173`。默认 `/api` 代理到 `http://127.0.0.1:8002`；通过 `VITE_API_BASE` 可以指定目标。独立 API 来源需要在网关配置相应的跨域策略。

## 检查与构建

```bash
npm test
```

```bash
npm run build
```

构建生成 `frontend/dist/`，FastAPI 启动时会检测并托管该目录。Docker 自动执行前端构建。

## 交互与数据

- 首页入口仅将示例写入输入框，确认文字后再发送。
- Enter 发送、Shift + Enter 换行；Ctrl / ⌘ + K 搜索本地会话。
- 右上角说明按钮打开站内说明。侧栏支持新对话、删除记录与切换主题。
- 流式文本在后端完成答案检查后分段发送，等待期间显示处理状态。
- `localStorage` 使用 `qingyu-new:conversations:v1` 保存最多 50 个会话、每个最多 100 条消息；主题使用 `qingyu-new:theme`。清除浏览器数据或切换浏览器后不会恢复本地列表。
- 后端 `session_id` 随会话保存，继续对话时复用。待确认操作仅在服务端进程中保留 15 分钟。
- `VITE_DEMO_USER_ID` 默认为 `u_demo`。当前后端尚未认证 `user_id`，不能直接用于不可信用户环境。

返回[项目说明](../README.md)。
