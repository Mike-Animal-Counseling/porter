[English](CHANGELOG.md) | **中文**

# 更新日志

Porter 的所有重要变更都记录在此。格式遵循
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)；
版本遵循 [SemVer](https://semver.org/)。

## [Unreleased]

### 交接页面与可选提取器

- 面板新增项目建议、中英文简报预览、复制/下载与交接审计，保留 owner、CSRF
  和禁止缓存保护。
- 两个存储后端均原子保存生命周期反馈；旧版偏好记录也禁止进入团队工作区。
- 新增可选本地 Ollama 结构化提取，后台调用并校验输出，所有模型候选记忆
  均需人工确认。PostgreSQL 提取批次以事务保存，默认仍使用规则后端。

### 切换功能整合（2026-10-19）

- 整合远端至 `4000dd5`，保留工作区/密钥迁移；兼容旧验收数据库，不删除数据。
- 切换摘要、矛盾检测和写回按 scope 过滤；写回默认预览，文件写入、合并和
  密钥管理要求 owner 权限。
- 工作区写入检查成员角色，禁止个人记忆进入团队工作区。
- PostgreSQL 保存归档溯源，合并/冲突解决使用完整事务；修复密钥领取 SQL、
  API 加密依赖和 Windows 文件原子替换。

### 运行入口加固（2026-10-19）

- 收件箱复用已有向量生成重复提示，不再调用模型或修改检索时间、触发衰减。
  PostgreSQL 相似度查询返回完整记录，与内存仓库的项目标签和内容保持一致。
- 新增只需单个客户端访问令牌的 Codex HTTP 模板，两种传输配置均通过
  Codex CLI 0.160.0 解析。stdio 模板使用规范的 embedding 变量名，
  避免被继承的环境配置覆盖其 HTTP 后端。

- 在 owner/认证路由之前拒绝陌生域名；自定义域名经过本机代理也需登录。
  支持 TLS 代理的显式公开来源地址，内置启动命令禁用转发头信任。
- 畸形 Origin/Referer 返回明确拒绝，默认端口按同源规则归一化。
- HTTP 与 stdio 统一解析认证开关；无效开关或存储模式阻止启动，
  避免误关认证或静默使用临时存储。OpenAPI 版本改为读取包版本。

### 仓库规范

- 统一 Python 打包与版本元数据，拆分轻量开发依赖和可选 ML 依赖。
- 统一 Ruff 检查、导入顺序与格式，增加对应 pre-commit 钩子、UTF-8/LF 约定和 Dependabot 更新配置。
- 收紧 CI 权限并固定 Action 提交，增加源码/wheel 构建与安装包验证。
- 修正部署、贡献和安全文档，使其符合真实 PostgreSQL、Docker 与 BGE-M3 验收结果。

### 修复与实测（2026-10-18）

- 修复真实部署发现的纯注释 SQL 执行、迁移中断后重试、游标默认值类型转换问题。
- 修复模型镜像嵌套依赖文件缺失，默认使用 CPU PyTorch，固定已验证的模型依赖。
- 删除自动合并审计中的记忆原文预览。
- 新增可重复 Docker 验收和 CI 任务；真实数据库、OAuth、两种 MCP、加密快照、
  容器重建均通过。本地完整测试 404 项通过，无跳过项。
- 真实 BGE-M3 端到端验收通过；中英文小样本 R@5=100%、R@1=58.3%，
  去重候选仍需人工确认。结果见 `docs/embedding-eval.2026-10-18.json`。

### 修复（2026-10-14）

- 修复分页和并发写入导致的同步遗漏；正确推进不可见页并过滤删除事件。
- 补齐数据库记忆/审计类型、JSON 解析和迁移锁；修改时保留置顶状态。
- 修复 Windows 加密快照损坏、新类型恢复和同秒文件名冲突。
- 修复 Docker 密钥、端口和持久化配置；容器浏览器使用密钥解锁。
- 统一客户端持久库配置，补充可安装包和随机凭据初始化脚本。
- 模型推理移出事件循环；临时分类错误可重试；禁止密钥路径穿越和覆盖。
- CI 增加 Windows/Linux 与真实 Postgres 验证，锁定核心依赖。
- 更正部署说明和完成度表述；详见 `docs/REVIEW.zh-CN.md`。

### 新增

**新的 embedding 候选模型** — `porter/embeddings.py` 新增
`HarrierEmbedder`（`microsoft/harrier-oss-v1-0.6b`，MIT 协议，1024 维，
与 BGE-M3/Qwen3 同维度，可直接 A/B，无需 pgvector 迁移）和
`EmbeddingGemmaEmbedder`（`google/embeddinggemma-300m`，768 维，Gemma
协议——需先过 license 审查 + 迁移，暂仅用于评测）。
`PORTER_EMBEDDER=harrier|embeddinggemma`；两者都已接入
`scripts/eval_embeddings.py`
（`--embedders hash,qwen3,bge-m3,harrier,embeddinggemma`）。真实评测仍需
真机（torch + 模型下载）。
**项目级同步（"行李跟人走"）**——`GET /api/v1/sync/delta` 和 MCP
`sync_delta` 工具接受 `project=<name>`：此时 delta 只携带该项目的
工作记忆（带 `project:<name>` 标签的记录，归一化匹配）加上无标签/
全局记录（个人域跟随用户到处走）。项目 X 的 harness 看不到项目 Y
的工作记忆。只是便利过滤，**不是**安全边界——OAuth 标签 scope 先做
可见性门控，同步内容保持 `untrusted_data`。（SPECS.md §5.1）

**`project` 快捷参数**——`POST /api/v1/memories` 和 MCP `memory_add`
接受 `project="<name>"`，映射为归一化的 `project:<name>` scope 标签
（`porter/triage.py::project_tag`；空名拒绝、超长名在标签校验处大声
失败）。harness 不用再手拼标签格式。

**面板域视图**——Memories 页新增 个人 / 项目 / 全部 分段切换（按声
明的域过滤；ML 解析出的域是 triage 时信号，不是存储字段），中英文
类型标签补齐 `personal`/`project`。

**Aider 导入器**——`<project>/.aider.chat.history.md` 聊天记录按会话
导入为项目域 `episode` 种子（对话上下文就是项目工作记忆，
SPECS §7.1.1）。best-effort；没有也正常。

**两个记忆域（核心产品定位）**——triage 把每次写入分到**个人域**
（你自己的记忆——偏好、健康、金融、PII；敏感度把关、人工审核）或
**项目域**（某个项目的**全部**工作记忆——指令、任务状态、决定、代
码库知识、踩过的坑；你换 harness 时跟着走的行李）。规则：个人域→
排队；项目域→在强制 `project:<name>` 作用域下自动批准（缺标签则
`missing_project_scope`）；冲突按项目限定（跨项目相似不算冲突）。
v1 的 `preference` 类型映射到个人域。（SPECS.md §7.1.1；README §原理）

**ML triage 模型**（`porter/triage_model.py`）——规则之上的开源第二
意见：`heuristic`（规则包装）、`zeroshot`
（`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`，中英多语言，每次写入
CPU 约 100–500ms）、`finetuned`（本地 6 标签
`microsoft/mdeberta-v3-base` 头）。`PORTER_TRIAGE_MODEL=auto` 默认
（finetuned → zeroshot → heuristic）；模型命中以
`ml_sensitive:<类别>` 排队。规则既是预过滤也是兜底（2s 超时→
heuristic，审计记录）；E2EE 记忆永不进模型；分数按内容哈希 LRU 缓
存。（docs/DEVELOPER.md §5a）

**Triage 训练闭环**（预测 → 纠正 → 训练 → 部署）——
`scripts/triage_label_batch.py`（输出可纠正的 JSONL：
`{domain, project, sensitivity, predicted_by}`）、
`scripts/finetune_triage.py`（6 标签微调，eval 集上每标签
P/R/F1，`--dry-run`）、`PORTER_TRIAGE_MODEL_PATH` 部署（默认
`models/triage-v1`）。几百条亲手纠正的标签就能超过 zero-shot。
（docs/DEVELOPER.md §5b）

**指令文件作为项目记忆入库**——Cursor `.cursorrules` +
`.cursor/rules/*.mdc` 和新增 `WarpImporter`（`~/.warp/WARP.md` +
`<project>/WARP.md`）加入既有的 AGENTS.md / CLAUDE.md / GEMINI.md /
QWEN.md 种子，成为 `project` 域的 inbox 种子并带 `project:<目录>`
标签（文档中明确：这只是项目域的来源之一，不是它的定义）。

**自动化层（分级审批、自动合并、衰减、自动捕获、摘要）**
- `porter/triage.py`：分级审批。低风险写入自动批准（审计，原因
  `low_risk`）；以下情况进入人工队列：基于内容的敏感检测
  （NFKC 归一化的中英 pattern——看内容不只看标签，标签走私无法绕过）、
  敏感 scope 标签、相似度 [0.70, 0.92)（可能冲突）、置信度 < 0.6。
  相似度 ≥ 0.92 保留去重候选路径。E2EE 记忆永远排队、永不内容扫描。
  `PORTER_APPROVAL_MODE=strict` 恢复 v1「全部排队」行为。
- 自动合并：与活跃记忆相似度 ≥ 0.97 自动并入（审计，状态 `merged`，保留
  历史链）；标签不做并集（T1）。
- `porter/decay.py`：懒置信度衰减（闲置每 30 天 ×0.95，读取时计算）；
  低于 0.2 → 自动归档（审计、可恢复）。跳过 E2EE 和 pinned 记忆。
  新增 `POST /memories/{id}/pin`（admin）。
- `memory_capture` MCP 工具：用 `porter/llm.py`（`extract_facts` 协议，默认
  regex stub，`PORTER_LLM_EXTRACTOR` 可接真实后端）从对话文本提取持久事实。
  每个事实走 triage；每 call ≤10 个事实，每个烧一条 rate limit（T5）。
- `GET /api/v1/digest`：待确认计数 + 按原因 breakdown、近期自动批准、冲突
  （待确认内容仅 admin 可见，按 tag scope 过滤）。
- 面板 inbox 显示 triage 原因 chips；owner 面板写入同样走 triage。
- 迁移 `007_automation.sql`：`last_retrieved_at`（衰减闲置时钟，从
  `updated_at` 回填）、`pinned`、`triage_reasons` 列。
  （`confidence` 自 migration 001 已存在。）

**面板「克制」设计打磨（95% 克制，5% 灵魂）**
- 强调色 indigo `#6E6BF2` → 暖金 `#D4A24E`（hover `#E5B45E`，
  弱化 `rgba(212,162,78,.14)`）；深色底面改为暖炭灰；
  浅色主题金色按对比度调整（`#A87B2A`）。
- 页面标题（`h1`）→ 中文衬线展示字体栈
  （`"Noto Serif SC","Songti SC","STSong","SimSun",serif`）；正文保持干净的
  无衬线字体（含 CJK 覆盖）；ID/游标/哈希的等宽字体不变。
- 同步页：细金色心跳线（纯 CSS/SVG 动画虚线，无 JS
  库）；活跃度绑定真实适配器的 `last_seen`——有适配器在线时动画，
  无在线时为平静直线 + "静默 / quiet"。
- 记忆页：表格 / 星图视图切换；星图是迷你
  力导向星空（原生 canvas，节点来自真实记忆列表，按共享标签连线；
  点击节点 → 走现有的 HTMX 侧滑详情；
  标签只用 `fillText` 绘制，绝不用 `innerHTML`）。
- 更温暖的文案（中英）：待办清零 "你的记忆星空很干净"、
  同步空状态、星图提示。
- 仪器级打磨：卡片细发丝内高光、金色视图切换激活态、
  按钮按压反馈、抽屉缓动曲线、时间戳/ID 等宽数字、
  长记忆摘要 2 行截断。

**面板 UI 重设计（按 `ui-redesign-spec.md`）**
- 深色优先的设计系统，浅色模式通过 CSS 变量 token 切换：
  `porter/static/css/tokens.css`（深/浅精确 token 集、单一
  indigo-violet 强调色 `#6E6BF2`、语义化状态色）+
  `porter/static/css/app.css`（布局外壳、按钮/输入框/徽章/卡片/
  表格、每页样式）。无 Tailwind、无 CDN——离线优先保持。
- 新布局外壳：248px 侧边栏（8 个带图标导航项、待办数量
  徽章、同步状态点；<1100px 收起为图标栏，<720px 变抽屉）
  + 粘性 56px 顶栏（面包屑、搜索、中/EN 分段选择器、
  主题切换持久化在 localStorage，默认深色）。
- 待办队列按分拣优先重建：卡片带安静的元数据眉题、
  置信度条、批准 / 编辑并批准 / 拒绝、全部批准（需确认）；
  HTMX 操作直接替换卡片并弹出 toast（无整页刷新）。
- 记忆：密集表格 + 侧滑详情抽屉（HTMX 片段），含
  元数据网格、置信度条、历史链、内联编辑表单。
- 同步：统计头（在线 / 落后 / 中位延迟）、适配器卡片（状态
  徽章 + 延迟条）、"How sync works" 说明、15s HTMX
  自动刷新。
- 接入方/分享以卡片展示，带复制按钮；撤销走 `hx-confirm`
  （删除了独立确认页）。
- 审计：1h/24h/7d/30d 时间范围筛选、CSV 导出（`/panel/audit.csv`）、
  带动作徽章的密集表格。
- 快照以卡片展示，带签名/加密徽章。
- 设置：分组卡片、危险区（输入确认删除全部记忆 +
  整实例重置，均审计）、CSV 导出链接。
- Toast 系统（`HX-Trigger` → 右下角，4s 自动消失）；旧的
  `?msg=` 闪信现在经 `panel.js` 渲染为 toast。
- `porter/static/js/panel.js`：主题、toast、复制到剪贴板、抽屉、
  侧边栏、语言切换（fetch POST + 重载）。
- 内联 SVG `icon()` 辅助函数（24 个图标，无 emoji）；`?msg=` 横幅移除。
- 新增 i18n 键（中英，完整性测试通过）：导航/外壳、同步统计、
  危险区、审计范围、空状态。

**第二批 harness：再增 12 个（共 27 个）**
- `clients/` 新增 12 个 MCP 客户端配置：`devin-mcp_config.json`
  （`~/.config/devin/mcp_config.json`）、`continue-config.json`
  （`~/.continue/config.json`，仅 Agent 模式）、`junie-mcp.json`
  （`~/.junie/mcp/mcp.json`）、`droid-mcp.json`（`~/.factory/mcp.json`，
  `type: stdio`）、`openhands-mcp.json`（`~/.openhands/mcp.json`）、
  `tabnine-settings.json`（需要重启）、`zencoder-settings.json`
  （VS Code `zencoder.mcpServers` 键）、`neovim-mcphub-servers.json`
  （经 mcphub.nvim，覆盖 avante.nvim + codecompanion.nvim）、
  `emacs-gptel-mcp.el`（经 mcp.el）、`antigravity-mcp_config.json`
  （HTTP `serverUrl`；Authorization 头支持未验证——已标注）、
  `kiro-mcp.json`、`augment-mcp.json`（`transport: stdio`）。
- 5 个新增 seed 导入器（只读、进待确认队列、打 `seed` 标签）：
  `junie`（`~/.junie/guidelines.md` + `<project>/.junie/guidelines.md`）、
  `droid`（`~/.factory/AGENTS.md` + 项目级）、`tabnine`
  （`<project>/TABNINE.md`）、`devin`（`<project>/.devin/**/*.md`，
  置信度 0.3）、`openhands`（`<project>/.agents/skills/*/SKILL.md`
  以 `skill_ref` 导入）。新增 CLI 参数 `--junie-dir`、`--factory-dir`；
  `--harness all` 现覆盖 14 个导入器。
- `NO_NATIVE_STORE` 注册表扩展：continue、zencoder、neovim、
  emacs、antigravity、kiro、augment（仅 MCP 植入，如实标注）。
  Windsurf 配置头注释说明了 Devin Desktop 改名（2026-06）；两个配置都附带。
- `_GlobalMdSeedImporter` 新增可选参数 `project_file_name`，用于
  项目级文件在子目录中的 harness（Junie）。

**F9 自动同步核心 / P1（PRD §9.5，US7）**
- 新增 `GET /api/v1/sync/delta?since=<int>&limit=<int,默认 100,最大 500>`
  （需要 `memory.read`）：增量变更流
  `{changes: [{seq, memory_id, op, status, at, actor_client_id, memory}], cursor: <head seq>}`。
  服务端 tag-scope 过滤；删除墓碑、以及非管理员看到的待确认创建，
  其 `memory` 为 null（待办队列规则）；
  非管理员只能看到当前激活的记忆 + 删除墓碑。
  拉取按 `action='read'` 审计（`meta={"via":"sync"}`），并刷新
  适配器的 `last_seen` 但不推进其游标。
- 新增 `POST /api/v1/sync/cursor {"cursor": int}`（任意有效 bearer，无需
  特殊 scope）：显式游标确认；适配器以其 token 的公开
  `client_id` 识别（关闭鉴权时为 `"owner"`）。
  返回 `{adapter_id, cursor, last_seen}`。
- 每次变更都追加到 `change_log` 流
  （`create_pending`→`create`、`approve`→`approve`、`revise`→`revise`
  针对新记录、`delete`→`delete`），携带 `actor_client_id`
  （所有变更类 repo 方法新增可选参数，向后兼容）。
  Migration `004_sync.sql`：`change_log` 表、`memories.version INT`
  （乐观并发计数器，每次 revise +1）、以及
  `sync_cursors(device_id TEXT, cursor TEXT)` →
  `(adapter_id TEXT PK, cursor BIGINT NOT NULL DEFAULT 0, last_seen)`。
- US7 冲突：新增 `update_with_version(memory_id, *, expected_version,
  content, actor_client_id)`——沿 `superseded_by` 链解析到当前
  head，然后：匹配/None → 普通 revise；不匹配 →
  仍然 last-writer-wins，同时被覆盖的内容保留为一条新的
  待确认记录
  （`source={"conflict_of": <new_id>, "client_id": ...}`，置信度
  保留）。`PATCH /api/v1/memories/{id}` 接受可选
  `expected_version`，冲突时返回 200
  `{memory, conflict, loser_pending_id}`（永不 409）；
  MCP `memory_update(id, content)` 增加同名可选
  `expected_version` 参数，上报 `conflict`/`loser_pending_id`。
- 新增 MCP tool `sync_delta(since=0, limit=100)`，与 REST 端点相同的
  鉴权、scope、可见性规则。
- 新增面板页面 `/panel/sync`（+ 导航项）：OAuth clients ∪ sync
  cursors 的适配器表——在线点（last_seen < 5 min）、游标、
  落后 head 的延迟、最近活跃。中英新增 12 个 i18n 键。
- `MemoryOut` 新增 `version`。

**P2 E2EE 加密记忆（docs/DEVELOPER.md §6，PRD §9.5）**
- `POST /api/v1/memories` 接受 `content`（普通）或
  （`content_ciphertext` + `encryption{alg: "xchacha20-poly1305", key_id}`）
  ——二选一，不可同时、不可缺省（422）；密文必须是合法 base64
  （1..40000 字符），`key_id` 1..64 字符。
- Migration `005_e2ee_share.sql`：`memories.encrypted BOOLEAN NOT NULL
  DEFAULT FALSE`、`memories.key_id TEXT`。密文原样存在
  `content` 中，`embedding = NULL`：永不做 embedding、永不去重、
  永不可搜（两后端 search 都显式排除加密行）、永不记日志——
  审计只记元数据（服务端根本没有明文可泄）。
- `MemoryOut`（+ sync delta、导出、快照）新增 `encrypted: bool`
  和 `key_id: str | None`；读取时 `content` 返回密文并带标记。
  加密随 revise/update_with_version 历史链流转；
  快照导出/导入往返保留该标记。服务端永不解密。
- MCP `memory_add` 接受相同的 `content_ciphertext` + `encryption`
  参数，校验规则一致；结果上报 `encrypted: true`。
- 面板：加密行显示 "🔒 已加密 / Encrypted" 徽章（记忆列表、
  待办队列，详情页显示 `key_id`）；绝不尝试解密。
- 示例客户端的 `decrypt_memory` 辅助函数现在接受真实的服务端
  响应形状（`content` + `encrypted` + `key_id`）。

**P2 `memory.share`——owner 签发的受限分享 token（PRD §8）**
- 新增 `POST /api/v1/shares`（需要 `memory.admin`）：
  `{name 1..64, tags [预设+自定义], expires_in_hours 1..2160}`。
  每个 tag 都会校验（`memory.admin`、`../`、空、重复、未知 →
  400 `invalid_scope`）；签发的 scopes 永远恰好是
  `[memory.read.<tag>...]`——绝无其他。返回
  `{id, name, scopes, expires_at, token}`；token 只显示一次，
  永不存储、永不记日志。
- Migration `005_e2ee_share.sql` 还创建 `shares` 表
  `(id UUID PK, name, scopes TEXT[], created_at, expires_at, revoked)`。
- 鉴权：share JWT（`sub="share"`，jti = share id）在 tokens
  表未命中后按 `shares` 表解析——找到且未撤销且未过期才接受——
  经 `verify_bearer_token`（MCP）与 `_resolve_auth`（HTTP）共用的
  单一辅助函数，保证 REST 与 MCP 不会分叉。
  `AuthContext(client_pk=None, client_id="share:<id>")`。
- 新增 `GET /api/v1/shares`（token 永不列出）和
  `DELETE /api/v1/shares/{id}`（撤销；失效撤销缓存使 token
  在下一次请求即死，< 60s）。
- 限流（T5）：`client_pk=None` 身份（share + dev owner）现在按
  `share:<verified client_id>` 分桶，不再共用一个 `client:None`
  桶——一个 share 烧不掉其他 share 的配额（MCP `mcp:share:` 桶同理修复）。
- 新增面板页面 `/panel/shares`（+ 导航项）：列表（名称、标签、
  过期、状态）、创建表单（名称 + 标签勾选 + 过期小时数，token
  只显示一次）、撤销按钮。中英新增 20 个 i18n 键。
- share token 可用于 `/sync/delta`（tag 过滤）和 MCP search；
  写入、client 管理、share 签发、PATCH 均为 403。

**面板 i18n（中文/英文）**
- 新增 `porter/i18n.py`：所有面向用户的面板文案抽入
  `STRINGS = {"zh": {...}, "en": {...}}` + `t(key, lang)` 辅助函数
  （中文兜底，缺键永不让面板 500；任一语言缺键则测试失败）。
- 设置页的语言设置（中文 / English 下拉）；持久化到
  `~/.porter/settings.json`（0600，原子替换；
  测试可用 `PORTER_SETTINGS_PATH` 覆盖），重启不丢失，默认中文。
  `<html lang>` 正确设置（`zh-CN` / `en`）。
- 顺带修复了一处过时的面板注释：自 v0.1.0 红队走查起快照
  就是 Ed25519 签名的（面板还写着未签名）。

12-harness 扩展（尚未打 tag——随下个版本发布；
v0.1.0 在下方保持不动）。取舍理由：v0.1.0 已在 git 打 tag；
harness 扩展是加法（新增导入器 + 配置模板，无破坏性变更），
故在此累积到下一次版本切割。

### 修复

- **自动合并现已按项目隔离**：项目域写入不再自动合并进**不同项目**
  的近似重复记忆（否则项目作用域会被静默丢失）。跨项目近似重复改
  为以 `duplicate_candidate` 排队人工确认；同项目自动合并行为不变。
- MCP `memory_add` 和 `memory_capture` 现接受 `type="personal"` /
  `type="project"`（之前报 422，尽管 schema 允许）；面板类型标签中
  英文补齐两个域。

### 新增

**harness 导入器（PRD §8 F7）**
- Claude Code：`~/.claude/projects/<encoded-cwd>/memory/*.md` +
  每项目 `MEMORY.md` + `~/.claude/CLAUDE.md` seed 偏好
  （置信度 0.3，打 `seed` 标签）；可解析时尊重
  `autoMemoryDirectory` 覆盖；项目目录解码为尽力而为
  （原始编码名始终保留在 `source.project_dir`）。
- Windsurf：`~/.codeium/windsurf/memories/*.md`（尽力而为；不存在
  属正常，已文档说明）。
- Cline：`<project>/memory-bank/*.md`（需要 `--project-root`）。
- Roo Code：`<project>/memory-bank/*.md` + `.roo/rules/*.md`
  （规则以 `skill_ref` 导入，打 `rule` 标签）。
- Gemini CLI：`~/.gemini/GEMINI.md`（+ 项目级 `GEMINI.md`），
  低置信度 seed 偏好（0.4）。
- Qwen Code：`~/.qwen/QWEN.md`（+ 项目级）——同样处理。
- 所有扫描器跳过符号链接（harness 目录内的符号链接不能再把
  外部文件拉入导入）。
- 诚实注册表 `NO_NATIVE_STORE`：OpenCode / Amp / Kilo Code /
  Kimi CLI / Copilot CLI / Aider 没有可移植的记忆存储——
  仅 MCP 植入，不编造解析器。

**MCP 客户端配置（`clients/`，共 15 个）**
- 新增：`claude-code-mcp.json`、`opencode.json`（顶层 `mcp` 键）、
  `gemini-settings.json`、`qwen-settings.json`、
  `amp-settings.json`（`amp.mcpServers` 键）、
  `windsurf-mcp_config.json`、`roo-mcp.json`、`cline-mcp.json`、
  `kilocode-mcp.json`、`kimi-mcp.json`、
  `copilot-cli-mcp.json`（`type: stdio`；配置路径未验证——
  请在真实安装上确认）、`aider-conf.yml`（`mcp-servers:` 键）。
- 每个文件都带头注释：目标路径、如何应用、以及未验证
  （UNVERIFIED）之处的说明。

### 文档
- `DEPLOY.md`：15-harness 读取/植入矩阵（§3）、每 harness
  接入表（§5）。
- `README.md`：harness 支持列表更新。

## [0.1.0] — 2026-10-11

首个公开版本。按 PRD v2.0
（`~/workspace/your_files/porter-prd-v20.md`）的完整 P0 范围：
自托管、用户所有的 coding-agent harness 记忆 + 状态层。

### 新增

**记忆核心（F1，PRD §8）**
- 记忆 CRUD：`POST /memories`（写入进待确认队列，
  HTTP 202）、`GET /memories`（按 status/tag/type 过滤）、
  `POST /memories/search`（pgvector 语义搜索，top-k，tag 过滤）、
  `POST /memories/:id/approve`、`DELETE /memories/:id`（真删除：
  行 + 向量移除，审计只留 id）。
- 写入去重（相似度 > 0.92 返回
  `duplicate_candidates`，而不是悄悄堆积）。
- 待确认记忆在批准前对搜索不可见。
- 每条搜索结果都标 `untrusted_data: true`（PRD §5：
  检出的记忆是下游 agent 的不可信输入）。
- Append-only 审计日志（`audit_log` 表由禁止 UPDATE/DELETE 的
  DB trigger 保护；删除事件只留元数据，不留内容）。

**OAuth 风格授权（F2，PRD §10）**
- client 注册（`POST /api/v1/clients`；secret 只显示一次，
  静态 argon2 哈希存储）。
- 标准授权码流程：`GET/POST /oauth/authorize` 同意屏
  （每 scope 中文解释、逐项勾选、拒绝 → `access_denied`），
  一次性 10 分钟 code，绑定 client。
- `POST /oauth/token`（authorization_code + refresh_token 两种
  grant）；access token 为 HS256 JWT（1h），refresh token 为
  opaque（30d）并轮换。
- refresh token 复用检测：宽限窗外复用则吊销该 client 全部
  token 并发出 `security` 审计事件（T2）。
- 即时撤销（`POST /oauth/revoke`，单个 token 或
  `revoke_all`）；经 jti 黑名单，吊销的 access token 在 60s 内失效。
- scope 目录：`memory.read`、`memory.read.<tag>`（10 个预设标签，
  用户可扩展）、`memory.write`、`memory.admin`、`state.export`、
  `state.import`。**服务端强制执行**——tag scope 在 repository 层
  过滤，fail-closed；client 传的 tags 永不能扩大 scope。
- 限流：每 client 60 req/min，in-memory token bucket（T5）。

**MCP server（F3，PRD §8）**
- tools：`memory_search`、`memory_add`（→ 待确认队列）、
  `memory_update`；tool 描述警告模型返回内容为不可信数据（T3）。
- 传输：**stdio**（本地 CLI）和 **Streamable HTTP**（`/mcp`）。
- 每次 tool 调用都有 Bearer token 鉴权 + scope 强制执行，
  与 REST API 同一套机制。
- 开箱即用的客户端配置：`clients/codex-config.toml`、
  `clients/cursor-mcp.json`、`clients/zed-settings.json`。

**用户面板（F4，PRD §8、§17）**
- 服务端渲染（HTMX），中文 UI，仅本机。
- 记忆（搜索：语义/全文、过滤器）、待确认队列
  （批准 / 编辑并批准 / 拒绝、重复提示）、接入方
  （scope、最近活跃、一键撤销）、审计流、快照、
  设置（自定义 scope 标签、全部导出、备份说明）。

**harness 记忆导入器（F7）**
- `porter import` CLI：把 coding harness 的本地记忆文件
  （先读 Codex `~/.codex/memories/`）读入待确认队列并标注来源。
  对 harness 文件只读——Porter 永不修改 harness 自身的存储。

**快照（F6 v1，PRD §9）**
- 版本化、Ed25519 签名的快照格式：实例密钥对首次使用时生成
  （私钥 0o600，存于 `<snapshot_dir>/.keys/`）；签名块随文件携带，
  另一台机器也能验证。`inspect`/`apply` 遇篡改 fail closed；
  旧的无签名文件接受但给出明确警告。Trust-on-first-use：
  任何合法密钥都能验证（伪造但合法的快照仍进待确认队列，
  永不直接激活）。
- 可选 AES-256-GCM 加密（用户密码）。
- `verify`（dry-run 列表）和 `apply`（冲突 → 待确认队列，
  永不静默覆盖）。面板提供创建/列表 UI。

**部署（F5）**
- `docker compose up --build` 拉起 api + Postgres 16/pgvector +
  embedder；migration 启动时自动执行。
- `DEPLOY.md`：完整的生产部署指南。

### 分阶段评审中发现的安全修复（全部在发布前修复）

- W1：`InsufficientScope` 误映射为 501（会导致 W2 中所有
  scope 拒绝失效）；asyncpg 连接池在 import 时绑定了一个一次性
  event loop（uvicorn 下每次查询都会失败）；校验错误泄漏了
  FastAPI 默认形状而非统一 `{error:{code,message}}`；无输入大小限制
  （接受 1MB 内容）；`superseded_by` FK 缺 `ON DELETE SET NULL`
  （破坏 true delete，US5）；approve 的 SELECT-then-UPDATE TOCTOU。
- W2 构建：撤销缓存按 `str(jti)` 做键，但用 `UUID` 对象失效 →
  被吊销的 token 仍然有效；tag 过滤 fail-open（空 scope/tag 交集
  返回全部）。
- W2 评审（P0）：开放的首次 client 注册 + 无 owner 鉴权的同意屏 +
  `0.0.0.0` 绑定 = 同网段邻居可在配置期自注册并自批
  `memory.admin` → 用一次性 setup token（`X-Porter-Setup-Token`）+
  默认仅 localhost 绑定修复。
- W2 评审（P1）：限流按未验证 JWT `sub` 做键（伪造 token 可绕过）；
  撤销路径的裸 `except Exception`；缺失 client 时跳过 argon2
  （时序侧信道）；静默的 refresh 宽限路径（现发出 `security` 审计事件）。
- 发布红队（P1）：`POST /oauth/authorize` 无 CSRF 保护——恶意网站可
  伪造跨站批准 POST，把 scope 授权给攻击者注册的 client
  （scope 提权）。用共享的同源检查（`porter/origin.py`，面板共用）修复；
  宽松 CORS（`*`/`*`/`*`）彻底移除。
- 发布红队（P1）：实现了 Ed25519 快照签名（PRD §9.2；此前延期且
  文档过度承诺）。实例密钥对、0o600 密钥文件、签名内嵌于文件
  （可跨机验证）；inspect/apply 遇篡改 fail closed；旧无签名文件
  接受但明确警告。Trust-on-first-use：任何合法密钥都能验证
  （伪造但合法的快照仍进待确认队列）。
- 发布红队（P2）：导入器跳过 > 8MB 文件（DoS 防护）；面板模板的
  状态插值转义（纵深防御）。

### 变更
- `AUTH_ENABLED` 默认为 `1`（开启 Bearer 强制）。
- API 默认绑定 `127.0.0.1`；compose 发布 `127.0.0.1:8000`。

### 已知限制（v0.1.0）
- 小机器上 embedder 默认为 hash 桩；设置
  `EMBEDDER_KIND=sentence-transformer`（BGE-M3，~2.3GB，需 2–4GB
  内存）或 `openai`（把记忆文本发给 OpenAI——需明确开启）。
- Migration SQL + pgvector 路径已评审但未在 CI 对真实 Postgres 执行
  （构建环境无 Docker）——见 SECURITY.md。
- 限流器按进程；多 worker 需要 Redis（P2）。
- v1 同意屏无登录——网络访问即信任边界（绑定 localhost / 可信内网）。
- `/sync/delta` 返回 501（P1 自动同步桩，PRD §9.5）。

## 延期到 P1/P2（按 PRD）
- P1：hub-and-spoke 自动同步（delta 游标、适配器）、记忆质量
  （自动提取、合并建议）、ChatGPT/Claude 导出文件的
  `porter-export` 导入器。
- P2：端到端加密同步、多用户、记忆分享（`memory.share`）、
  开放规范标准化。
