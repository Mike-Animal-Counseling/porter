[English](DEVELOPER.md) | **中文**

# Porter — 开发者集成指南

Codex 连接已运行的 API 时，优先使用
[`clients/codex-http-config.toml`](../clients/codex-http-config.toml)，
通过 Streamable HTTP 与启动环境中的单个客户端访问令牌连接。
stdio 方案需要共享数据库访问与服务端 JWT 签名密钥，仅适合同一受信任机器。
两种配置已通过 Codex CLI 0.160.0 解析；这与对话中实际调用工具是不同的验收项。

## 1. Porter 是什么

Porter 是一个**用户所有的记忆与状态层**，面向 AI 编程 harness（Claude Code、Codex、Cursor、Zed、…）。
用户持有唯一的事实源头（single source of truth）；每个应用都是**客人**，只能获得临时的、范围受限的
访问权限。设计原则：**默认拒绝的最小权限授权**——每次读取都需要显式的 scope；
谁在何时读/写了什么都可**完整审计**；一切内容都可通过开放格式**一键迁移**；
返回给客户端的记忆一律视为**不可信输入**，绝不能当作指令执行。
v1 版 Porter 解决的是"一个人的多个工具共享记忆和状态"；赢得第三方应用生态明确不在范围内（PRD §5）。

Base URL: `http(s)://<porter-host>:8000`。以下所有 API 路由都相对于它。错误体恒为
`{"error": {"code": "<code>", "message": "<message>"}}`（PRD §11）。

---

## 2. OAuth 2.0 授权码流程

Porter 同时是授权服务器和资源服务器。标准授权码流程（PRD §10.2）。
Token 有效期：**授权码 10 分钟、一次性**；**access token 1 小时**；**refresh token 30 天**
（滑动续期）。

### Step 0 — 注册你的客户端

首个客户端注册需要一次性 setup token（见服务端启动日志或 `PORTER_SETUP_TOKEN`）；
之后则需要 `memory.admin` 权限。密钥只会显示**一次**——请妥善保管。

```bash
curl -s -X POST http://localhost:8000/api/v1/clients \
  -H "Content-Type: application/json" \
  -H "x-porter-setup-token: <setup-token>" \
  -d '{
    "name": "My AI App",
    "redirect_uris": ["https://myapp.example.com/oauth/callback"]
  }'
# -> 201 {"id": "<uuid>", "client_id": "porter_...", "client_secret": "<shown-once>",
#         "name": "My AI App", "redirect_uris": [...], "created_at": "..."}
```

### Step 1 — 授权请求

把用户送到授权确认页。请求**最小权限**的 scope（见 §3）：

```http
GET /oauth/authorize?response_type=code
  &client_id=porter_...
  &redirect_uri=https://myapp.example.com/oauth/callback
  &scope=memory.read.dietary%20memory.write
  &state=<random-csrf-state>
```

确认页会显示你的客户端名称、每个 scope 的中文说明，并允许用户逐个批准/拒绝
（PRD §10.2）。返回时校验 `state`（防 CSRF）。

### Step 2 — 用 code 换 token

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "code=<authorization-code>" \
  -d "redirect_uri=https://myapp.example.com/oauth/callback" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> 200 {"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600,
#         "refresh_token": "porter_rt_...", "scope": "memory.read.dietary memory.write"}
```

### Step 3 — 带着 bearer token 调 API

```bash
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <access-token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "我不吃什么", "top_k": 5, "tags": ["dietary"]}'
```

Scope 在**服务端**每次调用都强制执行；服务端从不信任客户端自称拥有的权限（T1，PRD §13.3）。

### Step 4 — 刷新

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=<refresh-token>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> 新的 access_token + 轮换后的 refresh_token（滑动续期）
```

Refresh token 重用被视为泄露：如果一个 refresh token 被用了两次，**该客户端的所有 token
都会被撤销**，并通知用户（PRD §10.4）。绝不要缓存或重试一个已经消费过的 refresh token。

### Step 5 — 撤销

```bash
# 撤销单个 token：
curl -s -X POST http://localhost:8000/oauth/revoke \
  -d "token=<token-to-revoke>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> 200 {"revoked": true}

# 撤销该客户端的全部 token（例如用户登出 / "断开 Porter"）：
curl -s -X POST http://localhost:8000/oauth/revoke \
  -d "token=<any-valid-token>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>" \
  -d "revoke_all=true"
```

用户也可以随时从面板撤销——被撤销的 token 会进入黑名单，60 秒内生效
（access token 本身 1 小时自然过期，作为兜底）。

---

## 3. Scope 目录

### 基础 scope（`porter/oauth.py:BASE_SCOPES`）

| Scope | 确认页标签 | 允许的操作 |
|---|---|---|
| `memory.read` | 读取全部记忆 | 搜索并读取**全部**记忆，不论标签 |
| `memory.write` | 写入记忆 | 提交新记忆 → 先进入**待确认队列**；用户批准后才能被搜索到 |
| `memory.admin` | 管理记忆 ⚠️ | 批准/删除记忆、读取审计日志、管理客户端与 scope tag。包含一切权限，最高级别 |
| `state.export` | 导出状态快照 | 创建/下载状态快照 |
| `state.import` | 导入状态快照 | 应用快照（恢复记忆与 agent 状态） |

### 带标签的读取：`memory.read.<tag>`

10 个预设标签中的任意一个（用户可添加自定义标签）都可以限定读取范围：`dietary`、
`coding_style`、`workflow`、`work`、`travel`、`health`、`finance`、`social`、`device`、`general`。
持有 `memory.read.dietary` 的 token 只能搜索带 `dietary` 标签的记忆；标签过滤由服务端
根据 token 的已授权 scope 强制执行，绝不信任客户端传来的标签
（`porter/oauth.py:effective_tag_filter`，fail-closed）。

**US6 验收示例**——请求 `memory.read` 被拒 vs `memory.read.dietary` 获准：

```bash
# 客户端在确认页请求 "memory.read" -> 用户拒绝，只批准了 "memory.read.dietary"。
# 签发的 token 只携带 ["memory.read.dietary"]。

# 下面这个会失败：token 并不包含完整的 memory.read……
curl -s -X GET "http://localhost:8000/api/v1/memories?tag=health" \
  -H "Authorization: Bearer <dietary-only-token>"
# -> [] (fail closed: 授权之外的行没有返回——标签交集为空)

# ……而已授权的标签可以：
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <dietary-only-token>" \
  -d '{"query": "忌口", "top_k": 5}'
# -> 200: 只有 scope_tags ⊆ [dietary] 的记忆

# 一个完全没有读取 scope 的 token：
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <write-only-token>" \
  -d '{"query": "忌口"}'
# -> 403 {"error": {"code": "insufficient_scope", "message": "missing required scope: memory.read"}}
```

Scope 匹配说明（`porter/auth.py:scope_implies`）：`memory.admin` 蕴含所有 scope。
任何 `memory.read.<tag>` 都满足 `memory.read` 门槛（各 endpoint 按 `memory.read` 设门槛；
标签过滤再收窄可见范围）。⚠️ **需要注意的不对称：** REST 的
`PATCH /api/v1/memories/{id}`（保留历史的编辑）需要 `memory.admin`，
而 MCP 的 `memory_update` 工具只需要 `memory.write`。两者都会创建经 `superseded_by`
关联的新版本；旧版本归档，绝不原地修改。

### Endpoint → 所需 scope

| 方法与路径 | Scope | 说明 |
|---|---|---|
| `POST /api/v1/memories/search` | `memory.read`（或 `memory.read.<tag>`）| 对**活跃**记忆的语义搜索；标签在服务端过滤 |
| `GET /api/v1/memories` | `memory.read`（或 `memory.read.<tag>`）| 列表/过滤；非 admin token 永远只能看到 `active` 记忆 |
| `POST /api/v1/memories` | `memory.write` | → `202 {"id", "status": "pending_review", "duplicate_candidates"}` |
| `POST /api/v1/memories/{id}/approve` | `memory.admin` | 面板/属主流程；可见性变更会被审计（T7）|
| `PATCH /api/v1/memories/{id}` | `memory.admin` | 保留历史的编辑；返回**新** id |
| `DELETE /api/v1/memories/{id}` | `memory.admin` | 硬删除（行 + 向量都删，US5）|
| `GET /api/v1/audit-log` | `memory.admin` | 谁在何时读/写了什么（只含 id 和数量——绝不含内容）|
| `POST /api/v1/clients` | `memory.admin`（bootstrap 之后）| 密钥只显示一次 |
| `GET /api/v1/clients`、scope-tag 管理 | `memory.admin` | |
| `GET /api/v1/export` | `memory.admin` | 完整导出（porter-export/v1，PRD §15）|
| `POST /api/v1/snapshots`、`GET /api/v1/snapshots`、`GET /api/v1/snapshots/{id}/download` | `state.export` | Ed25519 签名的快照 |
| `POST /api/v1/snapshots/apply` | `state.import` | |
| `GET /api/v1/sync/delta`、`POST /api/v1/sync/cursor` | `memory.read`（或 `memory.read.<tag>`）| **P1** —— 已上线（见 §5）|
| `GET /oauth/authorize`、`POST /oauth/authorize`、`POST /oauth/token`、`POST /oauth/revoke` | 客户端凭证 / 用户会话 | OAuth 本身 |
| `POST /api/v1/shares`、`GET /api/v1/shares`、`DELETE /api/v1/shares/{id}` | `memory.admin` | **P2** —— 属主签发的分享 token（见下）|

### 分享 token：`memory.share`（P2）

属主可以签发受限的、按标签限定的读取 token——比如让朋友的 agent 读取你的
`dietary` 记忆——而无需完整注册 OAuth 客户端。通过 `POST /api/v1/shares`
（`memory.admin`）或面板（`/panel/shares`）签发：

```http
POST /api/v1/shares
Authorization: <redacted>
Content-Type: application/json

{"name": "friend's agent", "tags": ["dietary"], "expires_in_hours": 168}
```

```jsonc
// 201 —— token 只会显示一次；服务端从不存储也不记录日志
{"id": "…", "name": "friend's agent", "scopes": ["memory.read.dietary"],
 "created_at": "…", "expires_at": "…", "revoked": false, "token": "<JWT>"}
```

**安全契约**（`porter/oauth.py:mint_share_token`）：

- 每个标签都必须是真实的预设或自定义标签——`memory.admin`、`../`、`""`、重复标签和
  未知标签都会被 `400 invalid_scope` 拒绝。Scope 由校验过的标签*派生*，
  绝不取自信调用方的输入，因此 scope 注入不可能发生。
- 签发出的 scope **永远恰好是** `[memory.read.<tag> …]`——绝无其他。
  分享 token 只能搜索/列表/`sync/delta`（按标签在服务端过滤），别的什么都做不了：
  写入、签发分享、客户端管理、PATCH 全部返回 `403`。
- JWT（`sub: "share"`，`jti` = 分享 id）就是凭证；`shares` 表只存元数据 + 撤销状态。
  `DELETE /api/v1/shares/{id}` 即撤销——token 在下一次请求时即失效（远早于 60 秒预算）。

---
## 4. MCP 工具（harness 适配器）

Porter 自带一个 MCP 服务器（`porter/mcp_server.py`），提供 PRD §11 的工具集。认证方式相同：
每次工具调用都校验 bearer token（stdio 用 `PORTER_TOKEN` 环境变量，
Streamable HTTP 用 `Authorization` 头），并在服务端强制执行 scope（T1）。

| Tool | 签名 | Scope | 行为 |
|---|---|---|---|
| `memory_search` | `(query, top_k=5, tags=None)` | `memory.read` / `memory.read.<tag>` | 语义搜索；标签过滤在服务端强制执行，fail-closed |
| `memory_add` | `(content, type="fact", scope_tags=None, confidence=1.0, project=None)` | `memory.write` | **分级审批**（§5）：低风险自动生效（可立即被搜索）；敏感/冲突/低置信度进入人工队列。`type` 可为 `personal`/`project` 显式声明记忆域（SPECS §7.1.1）；`project="<name>"` 会加上归一化的 `project:<name>` scope 标签。返回 `id`、`status`（`pending_review`/`active`/`merged`）、`triage_reasons`、`duplicate_candidates` |
| `memory_capture` | `(conversation_text, source_tags=None)` | `memory.write` | 从对话文本提取持久事实；每个事实走与 `memory_add` 相同的 triage。每 call ≤10 个事实，每个烧一条 rate-limit token |
| `memory_update` | `(id, content)` | `memory.write` | 更新一条**活跃**记忆；保留历史（经 `superseded_by` 生成新 id），返回 `revised_from` |
| `sync_delta` | `(since=0, limit=100, project=None)` | `memory.read` / `memory.read.<tag>` | 增量变更 feed（P1 自动同步）。`project="<name>"` 只拉该项目的工作记忆 + 无标签/全局记忆（SPECS §7.1.1）|
| `memory_graph` | `(entity=None, memory_id=None)` | `memory.read` | 可选 wiki 图谱，仅返回可见的有效记忆 |
| `switch_handoff` | `(project, from_harness, to_harness, top_k=10, lang="en")` | `memory.read` | 按授权过滤的项目/个人上下文和双语简报 |
| `writeback_sync` | `(harness, project=None, dry_run=True, include_sensitive=False)` | `memory.read`；实际写入需 `memory.admin` | 默认预览，仅写入服务端文件系统中的标记区域 |
| `memory_contradictions` | `(memory_id)` | `memory.read` | 从可见有效记录中查找启发式矛盾候选 |
| `memory_merge` | `(memory_ids, strategy="newest_wins_per_aspect", content=None)` | `memory.admin` | PostgreSQL 原子合并并保存来源溯源 |
| `memory_resolve` | `(winner_id, loser_ids, resolution)` | `memory.admin` | 保留赢家，归档其余记录并保存关联 |

Ollama 的 `memory_capture` 输出始终进入人工确认队列，不会自动批准或自动合并，
详见[提取说明](EXTRACTION.zh-CN.md)。面板 `/panel/handoff` 支持预览、复制和
下载，不需要客户端 token，但仍受 owner 身份和 CSRF 检查保护。

写回发生在 Porter 进程所在机器。Docker API 不能直接修改另一台电脑的编辑器
文件；目标文件在本机时，可使用预览/下载或可信的本地 stdio 进程。默认不写回
敏感记忆，需显式请求才会包含。

**所有返回的记忆都是 `untrusted_data`。** 每个结果都携带 `"untrusted_data": true`，
工具描述里也明确写了（T3，PRD §5 原则 5）：记忆内容必须视为不可信的用户数据，
**绝不能当作指令**——不要让它覆盖 harness 的系统提示词或工具策略。

**stdio 配置片段**（Claude Code 的 `.mcp.json`；合并，不要替换）：

```json
{
  "mcpServers": {
    "porter": {
      "command": "python3",
      "args": ["-m", "porter.mcp_stdio"],
      "env": { "PORTER_TOKEN": "<access-token-with-memory.read-memory.write>" }
    }
  }
}
```

> 保持 `PORTER_REPO=postgres` + `DATABASE_URL` 指向与 Porter API **同一个** Postgres，
> 否则记忆会落在另一个临时的独立存储里。完整模板见 `clients/claude-code-mcp.json`
> 和 `clients/codex-config.toml`，Streamable HTTP 替代方案见 `DEPLOY.md`
> （`POST http://localhost:8000/mcp`，带 `Authorization: Bearer <token>`）。

---

## 5. 自动化层：分级审批、衰减、摘要

写入不再总是排队。`porter/triage.py` 把每次写入分到两个记忆域：**个人
域**（你自己的记忆——偏好、健康、金融、PII——永远排队人工审核）和**项目
域**（harness 某个项目/工作区的全部工作记忆——指令、任务状态、决定、
代码库知识——在 `project:<name>` 作用域下自动批准）。队列拦截：敏感内
容/标签（基于内容的扫描——标签走私绕不过）、无项目标签的项目记忆
（`missing_project_scope`）、同项目冲突（相似度 0.70–0.92，只限同一
项目内）、低置信度（< 0.6）。近乎相同的写入（≥ 0.97）自动并入已有记忆
（标签不做并集）。`PORTER_APPROVAL_MODE=strict` 恢复 v1 行为（全部排
队）。完整规则：SPECS.md §7.1。

### 5a. ML triage 模型（可选，推荐）

规则之上的开源第二意见（`porter/triage_model.py`）：

- **模型**：`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`（约 280M 参数，
  中英多语言，CPU 可跑），HuggingFace `zero-shot-classification`。选它
  而不用更大的纯英文模型：跨语言 zero-shot 质量更好，每次写入延迟可
  接受。
- **安装**：`pip install transformers torch`（已在
  `requirements-dev.txt`），之后直接写入即可——模型首次使用时懒加载，
  加载有超时上限。
- **开关**：`PORTER_TRIAGE_MODEL=auto`（默认：有 finetuned 用
  finetuned → zeroshot 可导入用 zeroshot → heuristic）/ `zeroshot` /
  `heuristic` / `finetuned`；`PORTER_TRIAGE_MODEL_PATH`（默认
  `models/triage-v1`）；`PORTER_TRIAGE_THRESHOLD`（默认 0.65）；
  `PORTER_TRIAGE_TIMEOUT`（默认 2s）。
- **安全**：规则既是快速预过滤也是兜底——模型只看规则已通过的写入；
  推理超过 2s 就对该写入回退到 heuristic（审计记录）；E2EE 记忆永不
  进模型（密文盲）。分数按内容哈希做进程内 LRU 缓存（只缓存模型分
  数；规则每次都重新跑）。
- **延迟**：zero-shot 每次写入 CPU 预期约 100–500ms（首次加载需下载
  约 1GB 权重，一次）。

### 5b. Triage 模型训练闭环（预测 → 纠正 → 训练 → 部署）

zero-shot 是好起点；几百条**你亲手纠正**的标签就能在你的数据分布上
超过它。闭环：

1. **预测** — `python scripts/triage_label_batch.py --input texts.txt
   --output labels.jsonl`（或 `--from-db --limit 200` 从库里抽近期记
   忆）。跑当前 triage 栈，每行输出一个 JSONL：
   `{"text": "...", "domain": "personal|project",
   "project": "<name|null>",
   "sensitivity": {"health": 0/1, "finance": 0/1, "pii": 0/1,
   "credentials": 0/1}, "predicted_by": "zeroshot|heuristic"}`。
2. **纠正** — 人工就地改 JSONL：修正 `domain` 和 0/1 敏感度。这是训
   练信号——你 inbox 里批准/拒绝的记录就是 ground truth。
3. **训练** — `python scripts/finetune_triage.py --input
   labels_corrected.jsonl --output models/triage-v1`。在
   `microsoft/mdeberta-v3-base` 上微调 6 标签头（2 域 + 4 敏感度；
   CPU 友好默认：3 epoch、batch 8、eval 切分、保存最优）。结束打印
   eval 集上每标签 precision/recall/F1。需要
   `transformers torch scikit-learn`。`--dry-run` 只检查管线不训练。
4. **部署** — 设 `PORTER_TRIAGE_MODEL_PATH=models/triage-v1`（或
   `PORTER_TRIAGE_MODEL=finetuned`）；`auto` 自动最高优先级选用。用
   finetuned 模型重跑第 1 步抽查。

数据量：几百条标注就能超过 zero-shot；超过 ~2000 条后这个 6 标签任
务的收益趋平。

- **衰减：** 活跃记忆闲置时掉置信度（每 30 天 ×0.95，读取时懒计算）；低于
  0.2 自动归档（审计、可恢复）。重要记忆可 pin：
  `POST /api/v1/memories/{id}/pin`（admin）。
- **摘要：** `GET /api/v1/digest` 展示待确认计数（按原因）、近期自动批准、
  冲突。待确认*计数*对任何 reader 可见；待确认*内容*需 `memory.admin`
  （与 sync feed 相同的 inbox 规则）。
- **E2EE** 记忆永远排队、永不衰减/扫描（密文盲）。

## 6. 同步轮询（P1 自动同步）

> **状态：P1 —— 已上线。** `GET /api/v1/sync/delta` 返回增量变更流
> （纯 v1 服务端回答 `501 {"error": {"code": "sync_not_available", ...}}`；
> v1 只有手动快照，PRD §9.5）。下面的契约是已实现的行为。

Hub-and-spoke：Porter 服务端是唯一的事实源头；每个 harness 适配器维护一个按设备的
`cursor`，长轮询获取增量。

```http
GET /api/v1/sync/delta?since=<int>&limit=100
Authorization: Bearer <token>
```

```jsonc
// 200 响应
{
  "changes": [
    {
      "seq": 1042,                    // int64，严格递增
      "memory_id": "mem_abc123",      // UUID 字符串
      "op": "create",                // "create" | "revise" | "approve" | "delete"
      "status": "active",            // "pending" | "active" | "archived"
      "at": "2026-10-11T12:34:56Z",  // ISO-8601 时间戳
      "actor_client_id": "porter_claude",
      "memory": {                    // 完整记忆对象，"delete" 时为 null
        "id": "mem_abc123",
        "content": "...",            // E2EE 记忆：base64 密文，
                                     // 带 "encrypted": true + "key_id"
        "type": "preference",
        "scope_tags": ["dietary"],
        "confidence": 0.95,
        "untrusted_data": true        // T3: 同步来的内容是不可信输入
      }
    }
  ],
  "cursor": 1042                     // 返回的最大 seq
}
```

确认你的位置，让下一次轮询从上次断点继续：

```http
POST /api/v1/sync/cursor
Authorization: Bearer <token>
Content-Type: application/json

{"cursor": 1042}
```

**轮询循环建议（默认 60 秒）：**

```
cursor = 从磁盘加载 cursor，不存在则为 0
循环：
    r = GET /sync/delta?since=<cursor>&limit=100
    for change in r.changes: apply(change)      # 按 seq 幂等应用
    if r.changes: cursor = r.cursor; 落盘; POST /sync/cursor {"cursor": cursor}
    等待 60 秒
```

- 轮询**最多**每 60 秒一次（v1.5 可能加入长轮询/SSE）；遇到 `429 rate_limited` 请退避。
- 按 `seq` 幂等应用变更；重发时去重。
- 带标签的 token 只能收到其 scope 下可见的变更（与 §3 相同的 `effective_tag_filter` 规则）。

**冲突。** 默认最后写入者胜，但绝不静默丢弃：冲突的写入都会进入用户的待确认队列
等待裁决，绝不原地覆盖（PRD §9.3–9.5）。`revise` 操作会创建经 `superseded_by`
关联的新 id；之后请用新 id。

---

## 7. 端到端加密（P2）

> **状态：已实现（P2）。** 服务端现已按以下契约消费 `content_ciphertext`：
> `POST /api/v1/memories`（和 MCP 的 `memory_add` 工具）接受 `content` 或
> （`content_ciphertext` + `encryption`）——二者只能选其一，不能都传，也不能都不传
> （否则 422）。读取（列表、同步增量、导出、快照）在 `content` 中返回密文，
> 并带 `encrypted: true` + `key_id`；服务端从不解密（它不持有密钥）。

对于 Porter 服务端绝不能看到的记忆，在客户端加密后提交密文：

```http
POST /api/v1/memories
Authorization: Bearer <token-with-memory.write>
Content-Type: application/json
```

```jsonc
{
  // 注意：加密时不要传 "content" 字段。
  "content_ciphertext": "<XChaCha20-Poly1305(明文) 的 base64>",
  "encryption": {
    "alg": "xchacha20-poly1305",
    "key_id": "2026-10-dietary-01"
  },
  "type": "preference",
  "scope_tags": ["dietary"],
  "confidence": 0.95,
  "source": {"client_id": "porter_claude"}
}
```

**服务端保证：** 密文原样存储；**从不做 embedding、从不建索引、从不记明文日志**。
读取时原样返回。

**密钥管理是客户端的事。** 示例客户端（`clients/python/porter_client.py`、
`clients/typescript/porter-client.ts`）实现了这个方案：

- 密钥存放在 `~/.porter/keys/<key_id>.key`，权限 **0600**。
- 生成：经 libsodium 的 `crypto_box`/`crypto_aead`（`pynacl` / `libsodium-wrappers`）；
  XChaCha20-Poly1305，随机 24 字节 nonce 前置于密文（`nonce || ciphertext`，base64 编码）。
- 口令方案：口令派生密钥（Argon2id → 32 字节密钥），磁盘布局相同。
- 用户多设备间的密钥同步不在 v1 范围内（目前请用系统钥匙串/手动拷贝）。

**诚实的局限（P2，设计使然）：**

- **加密记忆不支持语义搜索和去重**——服务端没有 embedding 也没有明文可比对，
  所以 `memory_search` 匹配不到它们，`duplicate_candidates` 对它们永远为空。
- **标签、类型、置信度、时间戳仍是明文元数据**——只有 `content` 被加密。
  如果标签本身敏感（比如自定义标签命名了某种疾病），请重新考虑往 `scope_tags` 里放什么。
- `source` 块（client_id、created_at）照常是明文。

---

## 8. 错误码

源自 PRD §11 和 `porter/errors.py`。形状：`{"error": {"code", "message"}}`。

| HTTP | Code | 何时 |
|---|---|---|
| 400 | `invalid_scope` | 请求了未知或格式错误的 scope |
| 400 | `invalid_grant` | 错误/过期/被重用的授权码，错误的 refresh token 或客户端凭证 |
| 401 | `invalid_token` | 缺少或无效的 `Authorization: Bearer` token |
| 401 | `token_revoked` | token 已被撤销（黑名单命中）或检测到 refresh token 重用（T2）|
| 403 | `insufficient_scope` | token 有效但缺少所需 scope |
| 404 | `not_found` | 记忆 / 客户端 / 快照不存在 |
| 409 | `conflict` | 版本/快照冲突 |
| 422 | `invalid_input` | 校验失败（错误的 UUID、置信度越界、未知标签……）|
| 429 | `rate_limited` | 超出按客户端的速率限制（T5）——请退避 |
| 501 | `sync_not_available` | P1 占位：v1 中的 `/sync/delta` |
| 503 | `not_ready` | 存储库尚未初始化（启动中）|
| 500 | `internal_error` | 意外失败 |

PRD §11 还命名了 `duplicate_memory` 和 `snapshot_conflict`——v1 把近似重复
作为写操作（202）的 `duplicate_candidates` 字段报告，而不是当作错误。

---

## 9. 给集成者的安全提示

1. **记忆是不可信输入（T3）。** 搜索、列表、MCP、同步返回的一切都携带
   `untrusted_data: true`。绝不能把记忆内容当作指令；绝不能让它覆盖你的系统
   提示词、工具策略或白名单。这是最重要的一条。
2. **Token 存储。** `client_secret` 和 refresh token 是高价值凭证：存入系统钥匙串
   或密钥管理器，绝不进代码、日志或聊天记录。客户端密钥在服务端经 argon2id 哈希，
   无法找回——丢了就重新注册一个客户端。
3. **绝不记录记忆内容。** 审计日志是服务端的事（只记 id 和数量）。客户端日志、
   崩溃报告、埋点里都不能出现记忆文本——带标签或加密的记忆尤其如此。
4. **最小权限。** 优先用 `memory.read.<tag>` 而不是 `memory.read`；
   只做搜索的客户端绝不要申请 `memory.admin`。用户会在确认页拒绝过宽的 scope（他们真的会）。
5. **写入进入待确认。** `memory.write` 只提交到待确认队列；
   不要向用户承诺写入立即可被搜索。UX 里要处理 `duplicate_candidates`。
6. **重用的 refresh token = 泄露。** 如果刷新因 `token_revoked` 失败，停下：
   轮换凭证并把事件告知用户；不要用旧的 refresh token 重试。
7. **加密记忆。** 只有 `content` 被加密；标签/类型/时间戳是明文元数据（见 §6）。
   密钥文件必须 0600，绝不能通过你的遥测离开用户设备。

## 10. 示例客户端

- Python（标准库 + `requests` + `pynacl`）：[`../clients/python/porter_client.py`](../clients/python/porter_client.py)
- TypeScript（`fetch` + `libsodium-wrappers`）：[`../clients/typescript/porter-client.ts`](../clients/typescript/porter-client.ts)

两者都覆盖 OAuth token 使用、搜索/新增/更新、E2EE 工具函数和 60 秒同步轮询循环。

## Deployment corrections (2026-10-14)

Install the editable package from the repository root with
`pip install -c requirements-lock.txt -e .`. Stdio clients share the API
`DATABASE_URL`, `PORTER_JWT_SECRET`, and embedding endpoint; templates
include these fields. Docker owner browsers authenticate at `/owner/login`
using the setup token before approving OAuth consent.

Sync cursors acknowledge the last raw row scanned on a page, including
filtered rows. Drain further pages until the cursor stops advancing.
Process `archive` with a null memory by removing the old ID from local
context. Delete tombstones preserve scope metadata. Migration 013 serializes
sequence assignment through transaction commit.

Run `python -m pytest -q` and `python -m ruff check .`. For real
Postgres validation set `PORTER_TEST_DATABASE_URL` to a dedicated database
whose name ends in `_test`, then run `tests/test_postgres_integration.py`.
The integration test applies migrations and writes data; use a disposable
test database. 默认提取器为 `stub`；可选本地 Ollama 需要显式配置，候选事实
需人工确认，见[提取说明](EXTRACTION.zh-CN.md)。
