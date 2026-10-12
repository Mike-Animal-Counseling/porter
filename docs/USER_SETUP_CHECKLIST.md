# Porter 上线检查清单 — 需要你亲手做的事

> 代码 100% 写完了（273 个测试全绿），但有几件事必须在**你的机器**上做，我做不了。
> 按顺序走完，Porter 就跑起来了。预计 30–60 分钟。

---

## 1. 准备环境（5 分钟）

- [ ] 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Mac / Windows）或 Docker Engine（Linux）
- [ ] 安装 Python 3.12+（只用来跑导入器和客户端脚本）
- [ ] 拉代码：
  ```bash
  git clone https://github.com/Mike-Animal-Counseling/porter.git
  cd porter
  ```

## 2. 启动服务（10 分钟）

- [ ] 一条命令拉起全部（API + Postgres + pgvector + 面板 + embedding）：
  ```bash
  docker compose up -d
  ```
- [ ] 看日志拿一次性 setup token（只显示一次）：
  ```bash
  docker compose logs porter | grep PORTER_SETUP_TOKEN
  ```
- [ ] 生产环境务必设置 JWT 签名密钥（不设的话重启后所有 token 失效）：
  ```bash
  # .env 文件里加一行（.env 已在 .gitignore，不会进 git）
  PORTER_JWT_SECRET=<openssl rand -hex 32 的输出>
  ```
  然后 `docker compose up -d` 重启。

## 3. 打开面板，熟悉一下（5 分钟）

- [ ] 浏览器打开 http://127.0.0.1:8000/panel
- [ ] 设置页：确认语言（中文/English）、看看 scope tag 列表
- [ ] 记住：面板只能本机访问，这是设计（外网打不开是正常的）

## 4. 接你的 harness（每个 2 分钟，27 个里只接你用的）

- [ ] 到 `clients/` 目录找对应模板（文件名即 harness 名）
- [ ] 复制到模板头部注释里写的**目标路径**（如 Codex 的 `~/.codex/config.toml`）
- [ ] 把 `{{PORTER_TOKEN}}` 换成真实 token（面板 → 授权页 → 新建 client 拿）
- [ ] 重启那个 harness（Windsurf/Tabnine 必须完全重启才加载 MCP）

> 建议顺序：Codex → Claude Code → Cursor（你最常用的先）

## 5. 导入存量记忆（10 分钟）

- [ ] 先 dry-run 看会导入什么（**只读原文件，不碰你的 harness**）：
  ```bash
  python -m porter.import_cli --harness codex --dry-run
  ```
- [ ] 没问题就正式导入（进待确认队列，不会自动生效）：
  ```bash
  python -m porter.import_cli --harness codex
  ```
- [ ] 其他 harness 照此办理：`--harness claude-code|cursor|windsurf|cline|roo|gemini|qwen|junie|droid|tabnine|devin|openhands|chatgpt|claude`
- [ ] 去面板「待确认队列」逐条批准（或一键全部批准）

## 6. 端到端验证（5 分钟）

- [ ] 在 harness A 里说"记住：我对花生过敏"
- [ ] 面板待确认队列里批准它
- [ ] 在 harness B 里问"我对什么过敏"——应该能答出来
- [ ] 面板「同步」页能看到两个 adapter 的在线状态和 cursor

走完这一步，Porter 就正式接管你的记忆了。

---

## 诚实清单：我还没验证过的（需要你在真机上确认）

| # | 事项 | 为什么没验 | 你要做的 |
|---|------|-----------|---------|
| 1 | 真实 Postgres migration（004/005） | 我的环境没 Docker/PG | `docker compose up` 后看 migration 日志无报错 |
| 2 | BGE-M3 真实 embedding 质量 | 小 VM 跑不动大模型，测试用的是 HashEmbedder | 随便搜个语义相近的词，看召回准不准 |
| 3 | 真实 harness MCP 握手 | 这环境没装 Codex/Cursor | 第 4 步接完后，在 harness 里调一次 `memory_search` |
| 4 | Antigravity 的 Authorization 头 | 文档没写清 | 如果连不上，告诉我，我给它做免密本地模式 |

---

## 出问题找谁

- 部署问题：看 `DEPLOY.md`
- 接入开发：看 `docs/DEVELOPER.md`
- 报错贴给我：`docker compose logs porter` 的最后 50 行
