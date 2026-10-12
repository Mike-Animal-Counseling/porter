[English](SECURITY.md) | **中文**

# 安全政策 —— Porter v0.1.0

Porter 存储你的个人 AI 记忆。安全是一等需求，不是 backlog：
每个构建阶段（W1、W2、面板、MCP）之后都针对下述威胁模型做了
对抗性代码 + 安全评审，所有 P0/P1 问题都在发布前修复
（见 CHANGELOG.md）。

## 威胁模型（PRD §14）与本版本缓解措施

| # | 威胁 | 缓解措施（v0.1.0） |
|---|---|---|
| T1 | 恶意/被攻陷的 client 超 scope 读取记忆 | Scope 在每个请求上**服务端**强制执行（`require_scopes`）；`memory.read.<tag>` 在 repository 层过滤，**fail-closed**（scope∩tag 为空 → 返回空并审计）。client 传的 `tags` 永不能扩大 scope。OAuth 同意 POST 有同源 CSRF 守卫（`porter/origin.py`），恶意网站无法伪造批准来提权；宽松 CORS 已彻底移除。已用对抗性探测验证。 |
| T2 | token 被盗/重放 | access token 为 HS256 JWT，1h TTL，`alg` 固定（算法混淆探测 → 401）。refresh token 为 opaque，用后轮换；**宽限窗外复用会吊销该 client 的全部 token**并发出 `security` 审计事件。经 jti 黑名单撤销——吊销的 token 远早于 60s 预算即 401（US3）。 |
| T3 | 记忆内容污染下游 agent（prompt 注入） | 每条检索路径都给结果打 `untrusted_data: true`；MCP tool 描述指示模型把返回内容当数据、而非指令对待。写入走待确认队列（人工把关）。 |
| T4 | 本地 DB 被盗 / 服务器被攻陷 | 自托管：物理机安全是运维者的责任（见 DEPLOY.md 文档）。可选的静态防护：绑定 localhost、加密备份（运维者管理）。`.env.example` 只含占位符；默认无遥测、无外部调用。 |
| T5 | 恶意 client 刷写 | `memory.write` 只到待确认队列（永不直接发布）；限流 60 req/min/client（in-memory token bucket，按**已验证**的 client 身份分桶——评审中发现的伪造 `sub` 绕过已修复）；面板一键封禁 client。 |
| T6 | 快照文件在 apply 前被篡改 | `inspect`/`apply` 验证 Ed25519 签名——不匹配 fail closed。实例密钥对（0o600）；签名随文件携带以便跨机验证。Trust-on-first-use：任何*合法*密钥都能验证（你的机器各有自己的密钥）；旧无签名文件仅在明确警告下接受，应用的内容一律进待确认队列（人工把关），永不直接进入激活记忆。写入前先展示 dry-run 列表；可选 AES-256-GCM 用户密码加密。 |
| T7 | 审计日志被篡改以掩盖读取 | `audit_log` 为 append-only：Postgres trigger 禁止 UPDATE/DELETE（纵深防御——应用层 bug 也删不掉）；应用没有该表的更新/删除路径。删除事件只留元数据、不留内容（US5）。批准记为可见性变更审计。 |

## 信任假设（部署前必读）

1. **网络访问 ≈ owner（v1）。**同意屏和面板在 v0.1.0 中无登录。
   服务器**默认绑定 `127.0.0.1`**，首次 client 注册需要一次性
   setup token（`X-Porter-Setup-Token`，未设置则每进程自动生成）。
   不要把 API 端口暴露到不可信网络，除非前面加了鉴权。
2. **JWT secret。**生产环境设置 `PORTER_JWT_SECRET`。若未设置，
   每进程生成随机密钥（启动时大声警告），重启后所有 token 失效。
3. **Embedder 选择是隐私选择。**`EMBEDDER_KIND=openai`
   会把你的记忆文本发给 OpenAI。默认（小机器 `hash` 桩、
   Docker 中 `sentence-transformer`/BGE-M3）全部本地。
4. **限流器按进程。**多 worker 部署需要共享存储（Redis）——P2。

## 已验证 vs. 明确残留缺口

**本环境已验证**（89/89 测试 + 对抗性探测）：
scope fail-closed 行为、JWT 篡改/重放拒绝、撤销即时性、
refresh 复用全量吊销、限流绕过关闭、审计 append-only
（in-memory 镜像 + trigger SQL 检查）、true-delete
（行 + 向量消失、审计无内容）、快照签名验证、导入器只读行为。

**此处未验证——必须在 CI / 首次真实部署时验证：**
- Migration SQL（`001`–`003`）和审计 trigger 对真实
  `pgvector/pg16` Postgres（构建环境无 Docker；SQL 用解析器校验 +
  trigger 逻辑在测试中镜像）。
- BGE-M3 embedding 质量/延迟（构建 VM 剩余内存 <300MB；
  接口 + HTTP 链路已测，模型下载推迟到 Docker）。
- 真实 harness 握手（Codex/Zed/Cursor 配置按 MCP 规范编写，
  但未对真实 CLI 测试）。
- `docker compose up` 端到端（compose YAML 已校验，Dockerfile
  已评审，此处未执行）。

## 报告漏洞

这是个人自托管项目。如果你发现安全问题：

- **不要**开公开 issue 贴 exploit 细节。
- 联系：`[security contact — maintainer to fill in]`（维护者待填写），
  附描述和（如可能）最小复现。
- 目标：7 天内确认；修复 + 发布 + CHANGELOG 条目。

## 运维者加固清单（见 DEPLOY.md）

- [ ] `PORTER_JWT_SECRET` 和 `PORTER_SETUP_TOKEN` 设为强随机值
- [ ] `POSTGRES_PASSWORD` 已改掉 compose 默认值
- [ ] 绑定本机或置于带鉴权的反向代理 + TLS 之后
- [ ] 加密备份已配置并测试过恢复
- [ ] `EMBEDDER_KIND` 已有意识地选择（本地 vs OpenAI）
