[English](USER_SETUP_CHECKLIST.md) | **中文**

# Porter 上线检查清单 — 需要你亲手做的事

> 核心实现已纳入自动化测试；完整部署、模型效果和各工具的真实连接仍需验收。
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
  python scripts/setup_local.py
  docker compose up --build -d
  ```
- [ ] 从 `.env` 读取 `PORTER_SETUP_TOKEN`。启动辅助脚本会同时生成稳定的
  JWT 签名密钥和数据库密码；已有 `.env` 不会覆盖，需要自行补齐缺失值。
- [ ] 使用 `docker compose logs api` 查看启动状态。

## 3. 打开面板，熟悉一下（5 分钟）

- [ ] 浏览器打开 http://127.0.0.1:8000/panel
- [ ] 如果跳转到 `/owner/login`，使用 `.env` 中的 `PORTER_SETUP_TOKEN` 解锁。
- [ ] 安装本地包：`python -m pip install -c requirements-lock.txt -e .`。
- [ ] 设置页：确认语言（中文/English）、看看 scope tag 列表
- [ ] 记住：面板只能本机访问，这是设计（外网打不开是正常的）

## 4. 接你的 harness（每个 2 分钟，27 个里只接你用的）

- [ ] 到 `clients/` 目录找对应模板（文件名即 harness 名）
- [ ] 复制到模板头部注释里写的**目标路径**（如 Codex 的 `~/.codex/config.toml`）
- [ ] 面板新建 client 后，按 [DEPLOY.md](../DEPLOY.md) 完成 OAuth 授权和 code 交换，取得 access token；client secret 不是 access token。
- [ ] Codex 优先使用 `clients/codex-http-config.toml`，在启动环境设置 `PORTER_TOKEN`；不需要数据库密码或服务端签名密钥。
- [ ] stdio 模板填写 `{{PORTER_TOKEN}}`、`{{PORTER_JWT_SECRET}}`、`DATABASE_URL` 和 `PYTHONPATH`；密钥和数据库密码必须与 `.env` 一致。Windows 的 `command` 使用 `python` 或 Python 可执行文件路径。
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
| 1 | 你的部署与数据 | 2026-10-18 本机已通过全部 11 个迁移与隔离 Docker 验收 | 运行 `python scripts/verify_deployment.py`，再检查正式部署 |
| 2 | BGE-M3 对你个人数据的质量 | 已实测 CPU 推理与小型中英样本，效果并不完美 | 查看 `docs/embedding-eval.2026-10-18.json`，用自己的检索词复测 |
| 3 | 真实 harness 的工具使用 | Codex 0.160.0 已解析两种模板，尚未运行各工具的实际对话 | 第 4 步接完后，在 harness 里调一次 `memory_search` |
| 4 | Antigravity 的 Authorization 头 | 未在真实安装环境中验证 | 确认客户端支持发送 bearer 头后再使用模板 |
| 5 | ML 分级模型（zero-shot）延迟/质量 | 本轮未评测该可选后端 | 安装 `.[ml]`，设 `PORTER_TRIAGE_MODEL=zeroshot`，用代表性记忆检查 triage reason |
| 6 | Embedder A/B（qwen3/harrier vs bge-m3） | 需 CPU/GPU 时间 + 模型下载 | 跑 `python scripts/eval_embeddings.py --embedders qwen3,bge-m3,harrier,embeddinggemma` 对比 |

---

## 出问题找谁

- 部署问题：看 `DEPLOY.md`
- 接入开发：看 `docs/DEVELOPER.md`
- 排错查看：`docker compose logs api` 的最后 50 行，分享前去除密钥。
