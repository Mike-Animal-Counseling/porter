[English](README.md) | **中文**

# Porter — 个人 AI 记忆与状态搬运层

> "记忆和状态应该归用户所有、随用户搬走，应用只是经授权后来读取的客人。"
> 完整产品定义见 PRD：`~/workspace/your_files/porter-prd-v20.md`

**当前版本：v0.1.0** · [CHANGELOG](CHANGELOG.md) · [安全政策](SECURITY.md) · [部署指南](DEPLOY.md)

W1：FastAPI 记忆 CRUD + pgvector 语义检索 + 待确认队列 + 去重 + append-only 审计。
W2：OAuth 2.0 授权码流程 + scope 强制执行 + 限流 + 撤销黑名单。
F3：MCP server（stdio + Streamable HTTP，`memory_search`/`memory_add`/`memory_update`）。
F4：用户面板（记忆/待确认队列/授权/审计/快照/设置）。
E2E：harness 记忆导入器（Codex / Claude Code / Windsurf / Cline / Roo / Gemini / Qwen / Cursor / Junie / Droid / Tabnine / Devin / OpenHands；Zed/OpenCode/Amp/Kilo/Kimi/Copilot CLI/Aider/Continue/Zencoder/Neovim/Emacs/Antigravity/Kiro/Augment 诚实无导入）+ US1/US3 验收测试。
快照：Ed25519 签名（PRD §9.2）+ 可选 AES-256-GCM 加密。

> **部署到自己机器并接入 27 个 coding harness：见 [DEPLOY.md](DEPLOY.md)**

## 架构

```
porter/
├── porter/                 # Python 包
│   ├── app.py              # FastAPI 工厂 + 全部 REST/OAuth 路由
│   ├── auth.py             # 鉴权中间件：限流 -> Bearer 解析 -> jti 黑名单 -> scope 依赖
│   ├── origin.py           # 共享 origin 检查：localhost 门禁 + CSRF 同源守卫（面板/OAuth 共用）
│   ├── oauth.py            # OAuth 逻辑：scope 目录/JWT/secret/同意屏/发 token/轮换/撤销
│   ├── oauth_store.py      # OAuthStore：clients/auth_codes/tokens（Postgres/InMemory）
│   ├── ratelimit.py        # T5：in-memory token bucket（默认 60 req/min/client）
│   ├── embeddings.py       # Embedder 接口：hash(桩)/sentence-transformer/http/openai
│   ├── repositories.py     # MemoryRepository：Postgres(pgvector) / InMemory(numpy)
│   ├── mcp_server.py       # MCP server：tools 定义 + scope 鉴权（stdio/HTTP 共用）
│   ├── mcp_stdio.py        # stdio 传输入口（本地 CLI）
│   ├── panel.py            # 用户面板（HTMX 服务端渲染，localhost only，中英双语）
│   ├── i18n.py             # 面板 i18n：中英字符串表 + 语言偏好持久化
│   ├── importers.py        # harness 记忆导入器（14 个，含 Zed 诚实 no-op；只读）
│   ├── import_cli.py       # `porter import` CLI
│   ├── snapshots.py        # 快照：Ed25519 签名 + AES-256-GCM 可选加密
│   ├── exporter.py         # porter-export/v1 一键导出
│   ├── schemas.py          # Pydantic 模型（PRD §11）
│   ├── errors.py           # 统一错误格式 {error: {code, message}}
│   ├── db.py               # asyncpg 连接池 + migration runner（含 no-transaction 支持）
│   ├── config.py           # 环境变量配置
│   ├── main.py             # API 入口
│   └── embed_server.py     # 独立 embedding 服务（docker-compose `embedder`）
├── migrations/
│   ├── 001_init.sql        # memories/clients/grants/tokens/audit_log(append-only trigger)/
│   │                       #   snapshots/sync_cursors
│   ├── 002_oauth.sql       # auth_codes 表、tokens.token_hash、audit_action 新增 grant/refresh/security
│   └── 003_custom_tags.sql # 用户自定义 scope_tags
├── api/Dockerfile          # ARG REQ_FILE 切换 api/embedder 依赖
├── docker-compose.yml      # api + db(pgvector/pg16) + embedder，一键启动
├── clients/                # MCP 客户端配置模板（27 个 harness）
├── tests/                  # pytest：156 tests（CRUD/检索/去重/审计/OAuth/MCP/面板/快照签名/i18n）
└── scripts/
    ├── demo.py             # W1 演示（owner 上下文，无需 Docker）
    ├── demo_oauth.py       # W2 演示：注册->同意->token->读写->撤销->轮换（无需 Docker）
    └── demo_e2e.py         # E2E 演示
```

## 快速开始

### 方式 A：Docker（生产形态，Postgres + pgvector + BGE-M3）

```bash
cd ~/workspace/porter
cp .env.example .env
# 生产环境务必设置：PORTER_JWT_SECRET（随机 32 字节以上）
docker compose up --build
# API: http://localhost:8000   文档: http://localhost:8000/docs
```

首次启动时 API 的 lifespan 会自动跑 migration，embedder 会下载 BGE-M3（约 2.3GB，
缓存到 `porter-models` volume，只需一次）。`scripts/migrate.py` 保留供手动执行。

### 方式 B：本地开发（无 Docker，in-memory + stub embedding）

```bash
cd ~/workspace/porter
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/demo.py        # W1 端到端演示
.venv/bin/python scripts/demo_oauth.py  # W2 OAuth 全流程演示
.venv/bin/python -m pytest tests/ -q    # 跑测试（42 tests）
PORTER_REPO=memory .venv/bin/python -m porter.main   # 起 API（:8000）
```

## OAuth 流程（PRD §10）

```
1. POST /api/v1/clients            注册 client -> client_id + client_secret（secret 只显示一次）
2. GET  /oauth/authorize            同意屏：每条 scope 的中文解释 + 逐项勾选
3. POST /oauth/authorize            批准 -> 302 redirect_uri?code=...（10 分钟有效，一次性）
4. POST /oauth/token                code + secret -> access_token(1h, JWT) + refresh_token(30d)
5. API 调用带 Authorization: Bearer <access_token>
6. POST /oauth/token                grant_type=refresh_token -> 轮换（旧 refresh 作废；复用=被盗 -> 全吊销）
7. POST /oauth/revoke               撤销单个 token 或 revoke_all=true 全吊销（60s 内生效）
```

```bash
# 1. 注册（第一次需要一次性 setup token，之后需要 memory.admin）
#    setup token 在服务启动日志里（PORTER_SETUP_TOKEN 未设置时自动生成），
#    或用你自己设定的 PORTER_SETUP_TOKEN。
curl -s -X POST localhost:8000/api/v1/clients -H 'Content-Type: application/json' \
  -H "X-Porter-Setup-Token: <setup-token-from-startup-log>" -d '{
  "name": "my-cli", "redirect_uris": ["http://localhost:9999/callback"]
}'
# -> {"client_id": "porter_...", "client_secret": "...", ...}  # secret 只出现这一次！

# 2. 浏览器打开同意屏（人工批准）
open "http://localhost:8000/oauth/authorize?client_id=porter_...&redirect_uri=http://localhost:9999/callback&scope=memory.read.dietary%20memory.write&state=xyz"

# 3. 用 code 换 token
curl -s -X POST localhost:8000/oauth/token \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=<from-redirect>" \
  --data-urlencode "redirect_uri=http://localhost:9999/callback" \
  --data-urlencode "client_id=porter_..." \
  --data-urlencode "client_secret=<secret>"
# -> {"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600,
#     "refresh_token": "porter_rt_...", "scope": "memory.read.dietary memory.write"}

# 4. 带 token 调 API（scope 由服务端强制执行）
curl -s -X POST localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer eyJ..." -H 'Content-Type: application/json' \
  -d '{"query": "what foods should I avoid?", "top_k": 5}'

# 5. 刷新 / 撤销
curl -s -X POST localhost:8000/oauth/token \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "refresh_token=porter_rt_..." \
  --data-urlencode "client_id=porter_..." --data-urlencode "client_secret=<secret>"
curl -s -X POST localhost:8000/oauth/revoke \
  --data-urlencode "token=eyJ..." \
  --data-urlencode "client_id=porter_..." --data-urlencode "client_secret=<secret>"
# 撤销后该 token 再调 API -> 401 {"error": {"code": "token_revoked", ...}}
```

### Scope 目录（PRD §10.3）

| scope | 说明 |
|---|---|
| `memory.read` | 读取全部记忆 |
| `memory.read.<tag>` | 只读某标签的记忆（tag 限 10 个预设：`dietary` 饮食、`coding_style` 代码风格、`workflow` 工作流偏好、`work` 工作信息、`travel` 旅行、`health` 健康、`finance` 财务、`social` 社交关系、`device` 设备环境、`general` 通用） |
| `memory.write` | 写入（先进待确认队列，需批准） |
| `memory.admin` | 批准/删除/审计/client 管理（默认不授予，同意屏红色警告） |
| `state.export` / `state.import` | 快照导出/导入（P1） |

未知 scope -> `400 {"error": {"code": "invalid_scope", ...}}`。

## MCP Server（PRD §8 F3）

Porter 把记忆能力暴露为 MCP tools，供 Claude Code / Codex / OpenCode / Cursor 等 harness 调用：

| tool | 说明 | 需要 scope |
|---|---|---|
| `memory_search(query, top_k=5, tags=[])` | 语义检索，返回记忆（每条带 `untrusted_data: true`） | `memory.read` 或 `memory.read.<tag>`（tag 过滤服务端强制执行） |
| `memory_add(content, type="fact", scope_tags=[])` | 写入**待确认队列**（202 语义：返回 id + `pending_review` + 去重候选） | `memory.write` |
| `memory_update(id, content)` | 更新**已激活**记忆的内容（重嵌）；保留历史链：旧版本归档并经 `superseded_by` 指向**新 id**（与 REST PATCH 一致） | `memory.write` |

两种传输：

```bash
# 1) stdio（本地 CLI 优先）：token 走 PORTER_TOKEN 环境变量
PORTER_TOKEN=eyJ... PORTER_REPO=postgres DATABASE_URL=... \
  python -m porter.mcp_stdio

# 2) Streamable HTTP（远程 harness）：挂在 API 的 /mcp 上
#    POST http://localhost:8000/mcp，token 走 Authorization 头
```

鉴权与 REST 是同一套：Bearer token 验签 + 吊销检查 + scope 服务端强制执行；失败以 MCP tool error（`porter:<code>:` 前缀）返回，绝不静默。`/mcp` 路径在网关层即要求有效 token（匿名连 initialize 都不行）。检索到的记忆内容一律视为**不可信数据**（T3），tool 描述里已写明。

客户端配置模板在 [`clients/`](clients/)（27 个：codex/cursor/zed/claude-code/opencode/gemini/qwen/amp/windsurf/roo/cline/kilocode/kimi/copilot-cli/aider/devin/continue/junie/droid/openhands/tabnine/zencoder/neovim/emacs/antigravity/kiro/augment；`{{PORTER_TOKEN}}` 占位符，目标路径见各文件头注释与 DEPLOY.md §5）。注意各家的键名差异：OpenCode 用顶层 `mcp`、Amp 用 `amp.mcpServers`、Aider 用 YAML `mcp-servers:`。

## For developers（第三方应用接入）

想把 Porter 接进你自己的 AI 应用？完整接入指南：**[`docs/DEVELOPER.md`](docs/DEVELOPER.md)**（US6）——OAuth 授权码流程（含 curl 示例）、scope 目录与 tag 过滤语义、MCP tools、P1 同步轮询、P2 端到端加密、错误码表、集成方安全须知。

开箱即用的示例客户端（OAuth + search/add/update + E2EE + 60s 同步轮询）：

| 语言 | 路径 | 依赖 |
|---|---|---|
| Python | [`clients/python/porter_client.py`](clients/python/porter_client.py) | stdlib + `requests` + `pynacl` |
| TypeScript | [`clients/typescript/porter-client.ts`](clients/typescript/porter-client.ts) | `fetch`（Node 18+ 内置）+ `libsodium-wrappers` |

两个客户端的 demo 流程：`register → 打印 consent URL → search → add → poll once`（`PORTER_SETUP_TOKEN` / `PORTER_AUTH_CODE` 走环境变量）。

## 安全说明（PRD §14 威胁模型）

| 威胁 | 本版本缓解 |
|---|---|
| T1 超 scope 读取 | 服务端 scope 强制执行；`memory.read.<tag>` 在 SQL 层过滤，client 传的 tags 只做提示；空交集直接 fail-closed 返回空 |
| T2 token 泄露 | access token 1h；refresh 轮换 + 复用检测（10s 宽限窗外复用 -> 全 client 吊销 + `security` 审计）；撤销黑名单 30s 缓存，吊销 60s 内生效 |
| T3 记忆投毒 | 检索结果标注 `untrusted_data: true`；写入来源追踪；待确认队列人工把关 |
| T4 本机被拖库 | refresh token / auth code 只存 sha256；client secret 只存 argon2 哈希；**物理机与网络安全是用户责任** |
| T5 恶意刷写 | 60 req/min/client 限流（429 `rate_limited`）；`memory.write` 只进待确认队列 |
| T7 审计篡改 | DB trigger 禁止改删审计行；grant/revoke/refresh/security 事件全部审计 |

**v1 信任假设（重要）**：
- 同意屏（`/oauth/authorize`）和面板没有登录：**能访问到服务器网络的人就能操作**。默认只绑 `127.0.0.1`；如需暴露请放在可信内网/认证反向代理之后。同意 POST 有同源 CSRF 守卫，跨站伪造的批准会被 401 拒绝；宽松 CORS 已移除。
- 首次 client 注册需要一次性 setup token（`X-Porter-Setup-Token`，未设置时每进程自动生成并大声打印到启动日志）；之后注册需要 `memory.admin`。首次启动后请尽快走完第一个授权。
- 未设置 `PORTER_JWT_SECRET` 时每次重启生成临时签名密钥，旧 access token 全部失效（refresh token 不受影响，可重新换）。生产环境务必设置。

## 关键设计决策

| 决策 | 说明 |
|---|---|
| Access token = JWT (HS256) | 无状态校验；jti 黑名单做撤销（DB + 30s 内存缓存） |
| Refresh token = opaque | 32 字节随机，DB 只存 sha256；轮换制，复用检测防盗 |
| Auth code = opaque | 10 分钟、一次性，DB 原子 `consume` 防重放 |
| 写入先进待确认队列 | 防垃圾场（PRD §8 F1）；`POST /memories/:id/approve` 生效 |
| 去重阈值 0.92 | 写入时向量相似度 > 0.92 返回 `duplicate_candidates`，不自动合并 |
| 审计 append-only | DB trigger 禁止 UPDATE/DELETE（defense in depth）+ 应用层不提供改删接口 |
| 检索结果不可信 | 所有 search 命中带 `untrusted_data: true`（防 prompt 注入，PRD 原则 5） |
| Embedder 接口 | `hash`（桩，本地 dev/测试）/ `sentence-transformer`（BGE-M3，生产）/ `http`（embedder 服务）/ `openai`（可选，需用户明确开启） |
| Repository 接口 | `PostgresMemoryRepository`（生产）/ `InMemoryMemoryRepository`（测试/无 DB 环境），同一套领域规则 |
| 限流 | in-memory token bucket；多 worker 部署下每 worker 独立计数（自托管 v1 可接受，P2 可换 Redis） |

## 环境变量

见 `.env.example`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://porter:porter@localhost:5432/porter` | Postgres 连接 |
| `PORTER_REPO` | `memory` | `memory`（in-memory）/ `postgres` |
| `EMBEDDER_KIND` | `hash` | `hash` / `sentence-transformer` / `http` / `openai` |
| `AUTH_ENABLED` | `1` | `0` 关闭鉴权（仅本地 dev） |
| `PORTER_JWT_SECRET` | （随机临时） | 生产务必设置！ |
| `PORTER_RATE_LIMIT_PER_MINUTE` | `60` | T5 限流 |

## 用户面板（PRD §8 F4）

服务端渲染的面板（HTMX + 极简 CSS，无重型前端框架），挂载在 `/panel`。
默认中文，设置页可切换中文/English（`porter/i18n.py`，偏好持久化在
`~/.porter/settings.json`）。

**访问**：`http://127.0.0.1:8000/panel`（仅本机）。面板是 owner 界面：
auth 中间件把 `/panel` 视为公共路径前缀（免 bearer），但每条路由强制
localhost 门禁（`panel_owner` 依赖 + 结构化测试保证不遗漏）；`main.py`
默认只绑定 `127.0.0.1`。面板内的所有变更走与 REST 端点相同的
repository/store 调用 + 审计落盘，没有后门。

**页面**（PRD §17 信息架构）：

| 页面 | 路径 | 功能 |
|---|---|---|
| 记忆 | `/panel/memories` | 全文/语义搜索切换（HTMX 即时搜），按标签/类型/来源筛选；点进详情可编辑（保留历史链）或删除（需二次确认） |
| 待确认队列 | `/panel/inbox` | 批准 / 编辑后批准 / 拒绝；写入时命中的疑似重复（相似度 >0.92）会并排展示 |
| 授权 | `/panel/clients` | 接入方列表（权限中文说明、最近活跃）、注册新接入方（secret 只显示一次）、一键撤销（二次确认，60 秒内全网失效） |
| 审计 | `/panel/audit` | 日志流（谁/何时/动作/涉及记忆数/scope），按来源与动作筛选；append-only 完整性说明 |
| 快照 | `/panel/snapshots` | 创建快照（选标签、设密码→AES-256-GCM 加密）、历史列表与下载、上传应用（dry-run 预览 → 确认导入待确认队列） |
| 设置 | `/panel/settings` | 自定义 scope_tags 增删（预设标签受保护）、一键导出（porter-export/v1 JSON）、备份说明 |

**截图说明**（文字版，UI 以实际运行为准）：
- 记忆页：顶部搜索框 + 过滤器，表格列为内容/类型/状态/来源/创建时间。
- 待确认队列：每条记忆一张卡片，右侧批准/编辑/拒绝按钮，疑似重复以黄色警告框展示。
- 授权页：表格列为接入方/权限范围（中文）/最近活跃/撤销按钮。
- 审计页：顶部绿色完整性说明条，表格列为时间/谁/动作/涉及记忆/scope。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
# 99 tests: CRUD/待确认流/检索召回/去重/审计 + OAuth 全流程
# （注册/同意/CSRF/发 token/轮换/复用检测/scope 隔离/撤销/限流/错误格式）
# + 面板（路由/门禁/待确认操作/撤销链路/快照/标签/导出）
# + MCP（双传输/鉴权/scope/不可信标记）+ 快照 Ed25519 签名（8 tests）
```

## 尚未在构建环境验证（诚实清单）

构建机没有 Docker / 真实 Postgres / 大内存，以下在本环境**未执行**，
首次真实部署或 CI 里必须验证（详见 [SECURITY.md](SECURITY.md)）：

- Migration `001`–`003` 在真实 `pgvector/pg16` 上的执行 + 审计 trigger 的实际生效
- BGE-M3  embedding 的质量与延迟（构建机内存不足；接口与 HTTP 链路已测通）
- `docker compose up` 端到端（compose YAML 已校验，Dockerfile 已评审）
- 真实 harness 握手（27 个配置按各家 MCP 规范编写，未对真实 CLI 测试；Copilot CLI 的配置路径尤其需要真机确认）

## 路线图

- [x] W1：记忆 CRUD + 检索 + 审计（含 review）
- [x] W2：OAuth 授权码流程 + scope 强制执行 + 限流 + 撤销（含 review）
- [x] F4：用户面板（记忆/待确认/授权/审计/快照/设置）+ 自定义标签 + 导出
- [x] F3：MCP server（stdio + Streamable HTTP）+ 客户端配置模板
- [x] E2E：harness 记忆导入器（Codex / Claude Code / Windsurf / Cline / Roo / Gemini / Qwen / Cursor / Junie / Droid / Tabnine / Devin / OpenHands；其余诚实无导入）+ 27 harness MCP 配置
- [x] 快照 v1：Ed25519 签名 + 可选 AES-256-GCM 加密
- [x] 发布：版本化 + CHANGELOG + SECURITY.md + 红队走查
- [ ] P1：自动同步（`sync_cursors` 表 + `/sync/delta` 桩已预留）
