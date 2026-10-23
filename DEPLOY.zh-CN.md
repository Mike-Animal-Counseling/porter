[English](DEPLOY.md) | **中文**

# 部署与接入 Porter

本指南部署的是本机运行、单 owner 的 alpha。先接入一个客户端，再完成
[简短的用户验收清单](docs/USER_SETUP_CHECKLIST.zh-CN.md)。
[验收记录](docs/ACCEPTANCE.zh-CN.md) 区分了已自动验证的服务能力和待完成的真实工具、浏览器检查。
配置模板不等于兼容认证。

## 1. 环境与启动

- Git、Python 3.12+，以及带 Compose 2.24.4+ 的 Docker。
- 本机 8000（API/面板）、8001（模型）、5432（数据库）端口可用，或修改 Compose 映射。
- 首次启动需联网下载镜像、依赖和模型。实测 BGE-M3 模型缓存约 3.8 GB，镜像另需磁盘空间。
  支持 CPU；模型容器的内存上限为 4 GB。API、数据库和 Docker 还需额外内存，
  这不是对整机最低配置的保证。

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git
cd porter
python scripts/setup_local.py
docker compose up --build -d
docker compose ps
docker compose logs --tail 50 embedder api
```

验收未合并的修复时，应使用候选 PR 的来源分支。初始化脚本创建私有 `.env`，
生成稳定的随机凭据；已有文件会保留，缺失项请对照 [`.env.example`](.env.example)。
虽然示例里原生开发环境的仓库模式为 `memory`，Docker API 会明确使用持久化 PostgreSQL。

模型镜像默认使用 CPU 版 PyTorch，已测约束保存在 `requirements-embedder-lock.txt`。
首次模型下载可能较慢，API 会等待模型服务健康。就绪后访问
**http://127.0.0.1:8000/healthz** 应返回 200。不要通过删除数据卷解决启动错误。

打开 **http://127.0.0.1:8000/panel**。Docker 转发的浏览器连接需要在 `/owner/login`
使用 `.env` 中的 `PORTER_SETUP_TOKEN` 解锁。签名 owner 会话持续 12 小时，也保护
OAuth 授权页面。原生 loopback URL 保留本机 owner 访问；自定义域名需要登录。
面板默认英语，可在设置中切换中英文；Docker 将语言偏好保存在 `porter-data` 数据卷。

## 2. 创建受限客户端，取得 token

在 owner 面板打开「接入方 / Clients → 新建」。每个工具独立创建客户端，
私下保存返回的 `client_id` 与仅显示一次的 `client_secret`。
本指南手动 OAuth 示例使用精确 redirect URI：
`http://127.0.0.1:18765/callback`。

只请求实际需要的权限：

| 用途 | scopes |
|---|---|
| 工作记忆写入工具 | `memory.read.work memory.write` |
| 工作记忆读取工具 | `memory.read.work` |
| 读取全部已批准记忆 | `memory.read`，范围更大，需主动决定 |
| owner 管理 | `memory.admin`，包含全部权限，应与普通工具分开 |
| 快照 API 操作 | 按需使用 `state.export` / `state.import` |

`memory.write` 允许提交新记忆，分级审批可能让低风险内容直接生效；它不授予读取权限。
更新现有记忆必须同时具备 write 和对目标的 read 权限。
显式合并/解决冲突、实际写入服务端文件和管理设备密钥封套需要 admin。
owner 面板可以完成人工审核，无需将 admin 发给编程工具。

### 授权码交换

在 owner 浏览器中打开以下 URL，替换 client ID，并使用新生成的、不可预测的 state：

```text
http://127.0.0.1:8000/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http%3A%2F%2F127.0.0.1%3A18765%2Fcallback&scope=memory.read.work%20memory.write&state=YOUR_RANDOM_STATE
```

检查权限后批准。此手动流程不要求运行回调服务器，浏览器可能显示连接失败。
从地址栏读取 `code` 和 `state`，确认 state 与原请求一致。
使用相同的精确 redirect URI 交换短期、一次性的 code。

Bash 示例，所有占位符在本地替换：

```bash
PORTER_CLIENT_ID='YOUR_CLIENT_ID'
PORTER_CLIENT_SECRET='YOUR_CLIENT_SECRET'
PORTER_AUTH_CODE='YOUR_ONE_TIME_CODE'
curl --fail-with-body -sS http://127.0.0.1:8000/oauth/token \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode "code=$PORTER_AUTH_CODE" \
  --data-urlencode 'redirect_uri=http://127.0.0.1:18765/callback' \
  --data-urlencode "client_id=$PORTER_CLIENT_ID" \
  --data-urlencode "client_secret=$PORTER_CLIENT_SECRET"
# 响应包含 access_token、refresh_token、expires_in 和 scope。
export PORTER_TOKEN='ACCESS_TOKEN_FROM_RESPONSE'
```

PowerShell 对应写法：

```powershell
$porterBase = 'http://127.0.0.1:8000'
$porterClientId = Read-Host 'Client ID'
$porterClientSecret = Read-Host 'Client secret'
$porterPair = Invoke-RestMethod -Method Post -Uri "$porterBase/oauth/token" -Body @{
    grant_type = 'authorization_code'
    code = (Read-Host 'One-time code')
    redirect_uri = 'http://127.0.0.1:18765/callback'
    client_id = $porterClientId
    client_secret = $porterClientSecret
}
$env:PORTER_TOKEN = $porterPair.access_token
```

客户端配置使用 **access token**，不是 client secret。通过 HTTP 连接时，owner setup token、
JWT 签名密钥和数据库密码都留在服务端。token 是凭据，不要将这些响应放进截图、issue 或提交文件。

### 刷新与撤销

access token **一小时**过期。向相同的 `/oauth/token` 端点提交
`grant_type=refresh_token`、`refresh_token`、`client_id` 和 `client_secret`。
接着上面的 PowerShell 示例：

```powershell
$porterPair = Invoke-RestMethod -Method Post -Uri "$porterBase/oauth/token" -Body @{
    grant_type = 'refresh_token'
    refresh_token = $porterPair.refresh_token
    client_id = $porterClientId
    client_secret = $porterClientSecret
}
$env:PORTER_TOKEN = $porterPair.access_token
```

refresh token 会轮换，必须保存新返回的值。更新配置中的 access token 后重新连接或重启工具；
静态 MCP 模板不会自动刷新。在「接入方 / Clients」撤销该客户端；需要恢复时重新授权取得新凭据。

## 3. 接入 MCP：优先 HTTP

Codex 将 [`clients/codex-http-config.toml`](clients/codex-http-config.toml)
中的配置块合并到现有配置，不要替换整个文件：

```toml
[mcp_servers.porter]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "PORTER_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 60
```

从设置了 `PORTER_TOKEN` 的环境启动或重启工具。同一服务名只选择 HTTP 或 stdio 之一。
维护者保留的 BGE 验收环境使用 **18010** 端口；标准 Compose 为 **8000**。
验收地址仅在该机器上可用。

2026-10-19，已安装的 Codex CLI 0.160.0 成功解析两种模板。
`codex mcp get porter --json` 只检查配置解析，不调用工具。
独立 HTTP/stdio MCP 客户端已通过部署验收。
配置格式参考 [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp/)。

**2026-10-22**，原生 Codex CLI **0.160.0** 完成合成 `memory_add`，原生 OpenCode **2.1.2**
在全新对话中实际搜索并取得同一个 active 项目 ID。审核、受限隐私拒绝、API 重建和独立同权限凭据撤销也已通过。
准确范围及验收时遇到的模型冷启动超时，见 [当日原生验收记录](docs/NATIVE_CLIENT_ACCEPTANCE.2026-10-22.zh-CN.md)。

### OpenCode V2（2.1.2）

2026-10-22，OpenCode 官方 V2 更新接口返回版本 **2.1.2**，包名 `@opencode/cli`。
按 [V2 官方安装指南](https://opencode.ai/v2/docs/) 安装，之后用 `opencode --version` 确认。
旧包 `opencode-ai` 和 [`clients/opencode.json`](clients/opencode.json) 使用 V1 配置形状；
按照 V2 步骤操作时，不要安装 V1 或照搬其字段。

将 [`clients/opencode-v2-http.json`](clients/opencode-v2-http.json) 复制为验收项目目录下的
`opencode.json`；V2 会发现这个项目文件，无需修改全局配置。
使用保留的本机验收服务时，将 URL 端口改为 18010。
V2 的服务放在 `mcp.servers` 下，用 `disabled` 替代 V1 的 `enabled`，OAuth 字段改用 snake_case。
模板的 `oauth: false` 表示使用已取得的 bearer token，不会关闭 Porter 的认证。
`codemode: false` 让验收对话直接调用 Porter 工具。
详见 [V2 官方 MCP 指南](https://opencode.ai/v2/docs/mcp-servers/)。

给 OpenCode 进程设置该客户端自己受限的 `PORTER_TOKEN`，然后运行
`opencode --standalone PATH_TO_ACCEPTANCE_FOLDER`。私有服务会继承这个环境，
之前运行的共享后台服务未必会继承。standalone 本身不隔离已保存账号；
若验收要全部保存在工作区，启动器还需为子进程设置私有 XDG 配置/数据/缓存/状态目录和 `OPENCODE_DB`。

在 OpenCode 内输入 `/connect` → OpenAI，出现 **Sign in with ChatGPT** 时选择该项，
由你本人完成浏览器授权，再输入 `/models`。本轮核对的 V2 源码也有旧 Codex 设备码登录方式；
运行 `opencode auth login openai --standalone` 可以查看实际提供的选项。
这是 OpenCode 的模型账号登录，与连接 Porter 的 MCP token 是两件事；不要复制 Codex 的登录文件。
用 `/mcps` 查看 Porter 状态。模型账号步骤见
[V2 官方登录指南](https://opencode.ai/v2/docs/cli/providers/)。

**2026-10-22 本环境实测：** OpenCode 2.1.2 已读取项目配置、替换 bearer token 环境变量，
使用 `openai/gpt-6.1-sol` 和保存的 ChatGPT OAuth 完成真实 Porter 工具调用。
每个 OpenCode 测试子进程均移除了 `OPENAI_API_KEY`，以确认使用保存的登录。仅通过 API 检查时，
先读取 `/api/plugin` 初始化内置插件，再查询 `/api/mcp`；先前的空列表来自初始化顺序，
不是模板格式错误。结果覆盖实测账号、版本和显式 MCP 调用，不保证所有账号都能使用同一模型。
浏览器交互与用户签收仍待完成。

### 可信本机 stdio 方案

在实际启动服务的 Python 环境安装：

```bash
python -m pip install -c requirements-lock.txt -e .
```

合并对应 stdio 模板，例如 [`clients/codex-config.toml`](clients/codex-config.toml)，替换：

- `PORTER_TOKEN`：该工具自己的受限 access token。
- `PORTER_REPO=postgres` 和 `DATABASE_URL`：与 API 相同的持久化数据库，
  包含随机生成的密码及主机映射端口。
- `PORTER_JWT_SECRET`：与 API 相同的签名密钥。
- `PORTER_EMBEDDER=http` 与 `EMBEDDER_URL=http://127.0.0.1:8001`：
  与已有向量一致的模型服务。
- `PYTHONPATH`：仓库绝对路径；`command`：已安装 Porter 的 Python，
  Windows 通常使用 `python` 或虚拟环境中的绝对路径。

不要把 Docker 环境里的 `EMBEDDER_URL=http://embedder:8001` 复制到主机进程，
该主机名只在 Compose 网络内部可用。这些共享设置必须一致，否则 stdio 进程可能使用独立、
临时的内存仓库，或认证失败。仅向与服务端同样可信的工具提供数据库和签名凭据。

## 4. 导入器与配置模板

**此矩阵列出仓库实现，不是实际客户端兼容认证。**「导入器」表示有读取该路径/格式的解析器，
使用样本目录测试；「模板」表示仓库提供了配置文件。
原生存储、配置路径和传输支持可能随已安装版本变化。
**Codex 0.160.0 与 OpenCode 2.1.2** 的显式 HTTP 流程已于 2026-10-22 通过，
见 [原生客户端记录](docs/NATIVE_CLIENT_ACCEPTANCE.2026-10-22.zh-CN.md)。
其他行仍只说明已有实现或模板，不代表原生客户端已验收。

| 工具 / 来源 | 导入器读取内容 | 已包含的 MCP 模板 | 导入参数 |
|---|---|---|---|
| Codex CLI | `~/.codex/memories/**/*.md` | `codex-http-config.toml` / `codex-config.toml` | `codex` |
| Claude Code | `~/.claude/projects/*/memory/*.md`、`MEMORY.md`、`CLAUDE.md` 初始指示 | `claude-code-mcp.json` | `claude-code` |
| Windsurf | `~/.codeium/windsurf/memories/*.md`，存在时读取 | `windsurf-mcp_config.json` | `windsurf` |
| Cline | 项目 `memory-bank/*.md` | `cline-mcp.json` | `cline --project-root PATH` |
| Roo Code | 项目 `memory-bank/*.md`、`.roo/rules/*.md` | `roo-mcp.json` | `roo --project-root PATH` |
| Gemini CLI | 用户/项目 `GEMINI.md` 初始指示 | `gemini-settings.json` | `gemini` |
| Qwen Code | 用户/项目 `QWEN.md` 初始指示 | `qwen-settings.json` | `qwen` |
| Cursor | 项目 `.cursor/memories/` 社区约定 | `cursor-mcp.json` | `cursor --project-root PATH` |
| Junie | 用户/项目 `.junie/guidelines.md` 初始指示 | `junie-mcp.json` | `junie` |
| Factory Droid | 用户/项目 `AGENTS.md` 初始指示 | `droid-mcp.json` | `droid` |
| Tabnine | 项目 `TABNINE.md` 初始指示 | `tabnine-settings.json` | `tabnine --project-root PATH` |
| Devin | 项目 `.devin/**/*.md` 初始指示 | `devin-mcp_config.json` | `devin --project-root PATH` |
| OpenHands | 项目 `.agents/skills/*/SKILL.md` 引用 | `openhands-mcp.json` | `openhands --project-root PATH` |
| ChatGPT 导出 | `conversations.json` 或导出 ZIP | 无 | `chatgpt --file PATH` |
| Claude 导出 | `conversations.json` 或导出 ZIP | 无 | `claude --file PATH` |
| Zed | 无原生存储导入器 | `zed-settings.json` | — |
| OpenCode | 无原生存储导入器 | `opencode-v2-http.json`（V2）/ `opencode.json`（旧 V1） | — |
| Amp | 无原生存储导入器 | `amp-settings.json` | — |
| Kilo Code | 无原生存储导入器 | `kilocode-mcp.json` | — |
| Kimi CLI | 无原生存储导入器 | `kimi-mcp.json` | — |
| Copilot CLI | 无原生存储导入器 | `copilot-cli-mcp.json` | — |
| Aider | 无原生存储导入器 | `aider-conf.yml` | — |
| Continue.dev | 无原生存储导入器 | `continue-config.json` | — |
| Zencoder | 无原生存储导入器 | `zencoder-settings.json` | — |
| Neovim | 无原生存储导入器 | `neovim-mcphub-servers.json` | — |
| Emacs / gptel | 无原生存储导入器 | `emacs-gptel-mcp.el` | — |
| Google Antigravity | 无原生存储导入器 | `antigravity-mcp_config.json`；bearer 头未实测 | — |
| Kiro | 无原生存储导入器 | `kiro-mcp.json` | — |
| Augment Code | 无原生存储导入器 | `augment-mcp.json` | — |

以上模板文件均位于 [`clients/`](clients/)。只将所需配置块合并到现有配置，
目标路径参考模板注释，并确认安装版本能接受。
格式测试不会联网复核所有厂商的最新 schema，也不能证明握手成功。

### 可选导入

首次验收不需要导入私人数据。导入器只读原文件；正式提交前先预览：

```bash
python -m pip install -c requirements-lock.txt -e .
python -m porter.import_cli --harness codex --dry-run
python -m porter.import_cli --harness codex --api-url http://127.0.0.1:8000
# 正式导入从环境变量读取 PORTER_TOKEN。
```

项目来源添加 `--project-root /path/to/project`。
`--harness all --dry-run` 可检查受支持的文件导入器；导出来源单独使用
`--harness chatgpt|claude --file PATH`。非默认端口需填写正确 `--api-url`。
token 需要 write，以及你明确决定授予的 read 范围。

ChatGPT/Claude 导出属于尽力迁移，不是完整聊天归档：每段对话生成 episode 摘要，
最多选取一个匹配关键词的 fact，置信度为 0.5。ZIP 在内存中处理并限制大小、条目数。
初始指示文件也可能是低置信度候选。服务端会脱敏已识别的 secret 模式，但不保证能识别每一个。
请在 Inbox 审核导入记录后再使用；真实工具文件形状可能不同。

## 5. 验证使用并理解自动化边界

运行 [两个工具的合成数据清单](docs/USER_SETUP_CHECKLIST.zh-CN.md)，要求真实调用
`memory_add` / `memory_search` 并比较 ID。仅从聊天历史得出的自然语言答案不能证明跨工具召回。

默认 `PORTER_APPROVAL_MODE=tiered` 可能直接批准低风险写入，
或合并可读、类型和标签一致的极相似记忆。个人、敏感、冲突或低置信度候选进入审核。
`strict` 会让所有新写入排队；批准不会扩大 token 的读取范围。

`sync_delta` 提供增量数据。适配器/客户端需要自行翻完分页、确认进度、继续轮询，
并将内容注入上下文。安装模板不会完成这些动作，也不会抓取所有聊天。
未实现轮询客户端时，Sync 页面不会凭空出现其在线状态。

默认提取器为正则 stub；可选 Ollama 需要显式指定本地模型，
其 `memory_capture` 事实始终进入人工审核。普通保存与交接无需它。
OpenAI 提取未实现；ML 审批和 wiki 图谱是另外的可选能力。
见 [提取器设置](docs/EXTRACTION.zh-CN.md)。

## 6. 持久化与备份

- `porter-db`：PostgreSQL 记忆、OAuth 和审计数据。
- `porter-data`：设置、快照与签名密钥。
- `porter-models`：下载的模型缓存。

保护并保留稳定的 `.env`。使用 `docker compose down` 停止而不擦除数据；
**`docker compose down -v` 会删除命名数据卷**。

可在 owner 面板创建密码加密的快照，下载到备份位置，单独保存密码，并在隔离实例测试恢复。
快照导出选定的 active 记忆，不是整个数据库、OAuth 客户端和部署配置的完整备份。

完整灾难恢复还需要 PostgreSQL 备份、`porter-data` 数据卷和受保护的配置/密钥。
以下方式可避免二进制数据库导出经过文本转换：

```bash
docker compose exec -T db pg_dump -U porter -d porter -Fc -f /tmp/porter-backup.dump
docker compose cp db:/tmp/porter-backup.dump ../porter-backup.dump
```

示例将备份复制到源码目录之外；请选择私有位置，并按私人数据保护。
后续备份使用新的文件名。不要使用 PowerShell 文本重定向传递
`pg_dump -Fc` 二进制内容。完整数据库恢复会改变实例状态，
先在独立数据库演练，再操作工作服务。

## 排错

| 现象 | 先检查 |
|---|---|
| API 未就绪 | `docker compose ps`、模型/API 日志、端口冲突和首次模型下载 |
| 模型不健康 | 下载网络、缓存、实际内存使用；实测缓存约 3.8 GB，模型容器上限 4 GB，其他服务另需资源 |
| 401 或 token 过期 | 对本服务刷新 token，更新后重新连接/重启工具 |
| 403 | 已批准 scopes 与目标可见性；不要直接发 admin 绕过 |
| 没有搜索结果 | active 状态、标签、正确地址/端口、相同模型和数据库 |
| stdio 退出 | Python 环境、包导入、数据库地址与共享签名密钥 |
| 导入找不到文件 | 没有匹配原生文件是正常结果；检查 dry-run 和路径覆盖参数 |
| 面板反复登录 | 本机端口、setup token 与浏览器 cookie 支持 |
| Host 被拒绝，400 | 使用允许的本机地址，或按下节配置精确域名/来源 |

分享日志前去除凭据和记忆内容；日志也可能包含本机路径和标识。
不要为解决数据或模型错误而静默使用空的临时内存仓库。

## 域名校验与反向代理

默认接受 `localhost`、`127.0.0.1`、`[::1]`，
其他 Host 在 owner/认证路由之前被拒。自定义域名配置：

```dotenv
PORTER_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],memory.example.com
PORTER_PUBLIC_ORIGIN=https://memory.example.com
```

host 列表只用精确名称，不带协议、端口、通配符。
public origin 使用浏览器实际访问的协议/主机/可选端口，不带路径或凭据。
带鉴权的 TLS 代理必须保留 Host。内置命令禁用转发头信任；
自定义域名即使经过 loopback 代理也需要 owner 登录，HTTPS origin 为 owner cookie 设置 Secure。
标准部署不等于可公开运行的多租户托管方案。

原生布尔配置如 `AUTH_ENABLED` 和 `PORTER_WIKI_ENABLED`
接受 1/0、true/false、yes/no、on/off，非法值阻止启动。
`PORTER_REPO` 只接受 memory/postgres。生产环境保留认证，明确使用 PostgreSQL。

## 维护者可重复验收

[检查报告](docs/REVIEW.zh-CN.md) 记录了历史全量 650 项测试基线与全部 14 个迁移；
[原生客户端记录](docs/NATIVE_CLIENT_ACCEPTANCE.2026-10-22.zh-CN.md) 记录了选定工具的实际调用。
变更后重新运行隔离服务验收：

```bash
python -m pip install -c requirements-lock.txt -r requirements-test.txt
python scripts/verify_deployment.py --keep-running
```

使用 Compose 项目 `porter-review`、私有 `.env.review`、
55432/18001/18000 端口及确定性的 hash 向量。它验证基础设施，不衡量语义模型质量，
也不需要下载模型。不传 `--keep-running` 时，验收容器会停止，命名卷保留。

真实 BGE-M3 模型验收是单独检查。可选地针对标准模型端口运行样本评估：

```bash
python scripts/eval_embeddings.py --embedders hash,http --http-url http://127.0.0.1:8001
```

更换向量模型前，使用独立数据库或重新生成**全部已有向量**。
维度相同不代表不同模型的向量兼容。更广泛的模型对比不属于入门或本次 alpha 签收门槛。
选定原生客户端的流程已通过；浏览器操作与用户签收仍按
[ACCEPTANCE.zh-CN.md](docs/ACCEPTANCE.zh-CN.md) 保留为待完成。
