# 部署与运行边界

## Docker 演示

需要 Docker Desktop 或 Docker Engine 与 Compose。

```bash
docker compose up --build -d
```

访问 `http://127.0.0.1:8000`。镜像构建会先编译前端，再安装后端，以非 root 用户运行。默认使用 SQLite、BM25 和进程内缓存，三个重型模型/解析开关关闭；没有 DeepSeek 密钥时使用离线回答。

演示数据在启动后显式初始化：

```bash
docker compose exec api python -m scripts.seed_demo
```

SQLite 保存在 `qingyu-new-data` volume。停止服务：

```bash
docker compose down
```

## 单机与多实例

两种配置都要求提供自己的 `POSTGRES_PASSWORD`。可以写入被忽略的根目录 `.env`，例如 `POSTGRES_PASSWORD=替换为自己的密码`，不要提交真实值。

单机使用 PostgreSQL、Redis 和一个 API：

```bash
docker compose -f docker-compose.single.yml up --build -d
```

多实例增加 Qdrant、两个 API 和 Nginx：

```bash
docker compose -f docker-compose.multi.yml up --build -d
```

数据库统一命名为 `qingyu_new`，入口均为 8000 端口。单机和多实例默认构建本地模型依赖，首次启动可能下载权重，需预留时间、内存和磁盘。停止时使用对应的 `-f` 参数。

这些配置提供部署骨架，未代表已完成公网或生产验收。上线前应设置 TLS、认证、访问控制、备份与监控，并按照实际部署验证。

## 本地构建

在 `frontend/` 安装依赖并构建：

```bash
npm ci
```

```bash
npm run build
```

回到根目录启动 API。检测到 `frontend/dist/` 后，FastAPI 在 `/` 提供前端页面；未构建时仍可使用 API 和 `/docs`。

## 数据与能力边界

- 请求中的 `user_id` 尚未绑定可信认证；不能把演示权限判断当作登录安全机制。
- 待确认操作仅在服务进程内保存 15 分钟。Redis 和 PostgreSQL 不会自动共享该状态，多实例环境需要在进一步完善确认状态持久化或路由后再承接真实写操作。
- 浏览器仅保存本地会话列表与可见消息；清除浏览器数据或切换设备后不会自动恢复。
- 业务工具使用项目数据库中的演示数据，未连接真实电商订单系统。
- 依赖不可用可能触发内存、词面重排或离线回答降级。通过 `/api/v1/capabilities` 查看实际后端，不能仅根据配置开关判断能力已启用。
- 没有真实模型和人工复核的回归结果，仅证明代码机制，不代表问答准确率或上线效果。

返回[项目说明](../README.md)。
