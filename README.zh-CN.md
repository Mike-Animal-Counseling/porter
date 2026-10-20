<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md"><b>中文</b></a>
</p>

<h1 align="center">Porter</h1>
<p align="center"><i>你的记忆，打包带走。任何 harness 都能用。</i></p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/docker-ready-blue.svg" alt="Docker ready">
</p>

**Porter** 是 AI 编程助手的自托管记忆层——你 AI 工具的"行李员"。每个
编程 harness 都会积累记忆：你的偏好，加上每个项目的工作记忆（规范、
决策、代码库知识、AGENTS.md/CLAUDE.md 这类指示文件）。今天这些记忆都
锁在各自工具里。项目做到一半从 Claude Code 切到 Codex，一切从零开始。
Porter 把它们打包，带着你走。


## 两件行李

- **个人（Personal）**——你自己的记忆：偏好、健康、财务、PII。
  敏感度把关、人工审核。你的东西，到哪都跟着你。
- **项目（Project）**——一个项目的全部工作记忆：规范、决策、任务状态、
  代码库知识。打上 `project:<name>` 标签、自动生效，你在哪个 harness
  里，它就被哪个 harness 拉走。这就是跟着你跨工具走的那件行李。

审批是拦垃圾的，不是拦你的：低风险记忆自动流动，只有敏感或冲突的才
等你点头。见 SPECS.md §7.1.1。

## 快速开始

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python scripts/setup_local.py # 生成并保存稳定的随机密钥
docker compose up --build -d  # API + Postgres/pgvector + 网页面板
# 打开 http://127.0.0.1:8000/panel
```

接入 harness：从 [`clients/`](clients/) 合并配置模板，填入 token、共享签名密钥、
数据库地址和项目路径，然后重启。27 个工具提供的是配置模板，真实连接仍需逐个验证。
完整步骤见 [`docs/USER_SETUP_CHECKLIST.zh-CN.md`](docs/USER_SETUP_CHECKLIST.zh-CN.md)。

## 功能

- **分级审批**——低风险记忆自动生效（有审计、可撤销）；敏感/冲突的进队列等你
- **MCP 原生，27 个 harness**——stdio + Streamable HTTP 传输
- **跨 harness 共享记忆**——共用数据库；客户端通过 `sync_delta(project="…")` 拉取项目增量，轮询和上下文注入由 adapter 完成
- **记忆导入器**——一次性导入各 harness 的原生记忆文件（AGENTS.md、CLAUDE.md、`.cursorrules`……）
- **语义搜索 + 去重**——pgvector 召回；近似重复只标记、不静默合并
- **Scoped OAuth 2.0**——每 harness 独立 token、按标签限读、一键撤销
- **E2EE 记忆**——XChaCha20-Poly1305；服务端永远只看到密文
- **签名快照**——Ed25519 签名、AES-256-GCM 加密的备份与恢复
- **双语面板**——设置里一键切换 English / 中文

## 工作原理

```
┌─────────┐   MCP（scope token）   ┌────────┐
│ harness │ ────────────────────▶ │ Porter │ ──▶ Postgres + pgvector
│ Codex…  │ ◀──────────────────── │  API   │      （你的机器）
└─────────┘   已生效的记忆         └────────┘
```

## 文档

| 文档 | 内容 |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Docker 部署、首次启动、生产注意事项 |
| [docs/DEVELOPER.zh-CN.md](docs/DEVELOPER.zh-CN.md) | OAuth 流程、MCP 工具、Python/TypeScript 客户端 |
| [SPECS.zh-CN.md](SPECS.zh-CN.md) | 完整协议规范、PRD §14 威胁模型 |
| [SECURITY.md](SECURITY.md) | 信任假设、漏洞披露 |
| [docs/USER_SETUP_CHECKLIST.zh-CN.md](docs/USER_SETUP_CHECKLIST.zh-CN.md) | 手把手：你在自己机器上要做的事 |
| [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md) | 发布历史 |
| [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md) | 轻量开发环境与检查命令 |
| [docs/REPOSITORY_STANDARDS.zh-CN.md](docs/REPOSITORY_STANDARDS.zh-CN.md) | 打包、Ruff、CI 与依赖维护规范 |

## 状态

v0.1.0 · 2026-10-19 本地完整验证：558 项通过，无跳过项。真实 Postgres、Docker、OAuth、两种 MCP、快照恢复和容器重建均已验收；BGE-M3 已实际运行并做中英文小样本评估。Codex CLI 0.160.0 成功解析两种接入模板；各工具的对话使用和视觉验收仍待完成。详见 [检查与修复报告](docs/REVIEW.zh-CN.md)。

## 许可

MIT — 见 [LICENSE](LICENSE)。
