<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md"><b>中文</b></a>
</p>

<h1 align="center">Porter</h1>
<p align="center"><i>数据只属于你。</i></p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/docker-ready-blue.svg" alt="Docker ready">
</p>

**Porter** 是 AI 编程助手的自托管记忆与状态层。每条记忆由你批准一次——之后你的任何 harness（Codex、Cursor、Claude Code 等 27 个）都能语义检索它。Scoped OAuth、人工批准队列、可选端到端加密。数据不出你的机器。

[![观看演示](assets/poster.png)](assets/porter-promo.mp4)

## 快速开始

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
docker compose up -d          # API + Postgres/pgvector + 网页面板
# 打开 http://127.0.0.1:8000/panel
```

接入 harness：从 [`clients/`](clients/) 复制对应模板，填入 token，重启 harness。完整步骤见 [`docs/USER_SETUP_CHECKLIST.zh-CN.md`](docs/USER_SETUP_CHECKLIST.zh-CN.md)。

## 特性

- **待确认队列**——AI 写的内容先进队列，你点头才生效
- **MCP 原生，27 个 harness**——stdio + Streamable HTTP 双传输
- **语义检索 + 去重**——pgvector 召回；疑似重复只提示、不自动合并
- **Scoped OAuth 2.0**——每个 harness 独立 token、按 tag 限读、一键撤销
- **跨 harness 同步**——A 里写入，B 里 60 秒内可读；冲突进队列、不丢失
- **端到端加密记忆**——XChaCha20-Poly1305，服务端只存密文
- **签名快照**——Ed25519 签名 + AES-256-GCM 加密的备份与恢复
- **双语面板**——设置里一键切换 English / 中文

## 原理

```
┌─────────┐   MCP（scoped token）  ┌────────┐
│ harness │ ─────────────────────▶ │ Porter │ ──▶ Postgres + pgvector
│ Codex…  │ ◀───────────────────── │  API   │      （你的机器）
└─────────┘   已批准的记忆         └────────┘
```

**两个记忆域。** Porter 把你的*个人*记忆（偏好、健康、金融、PII——
敏感度把关、人工审核）和每个*项目*的工作记忆（约定、决定、代码库知
识、任务状态——你从 Claude Code 切到 Codex/Cursor 继续做同一个项目时
跟着你走的行李）分开存。项目记忆在 `project:<name>` 作用域下自动批
准；个人记忆永远排队。见 SPECS.md §7.1.1。

## 文档

| 文档 | 内容 |
|---|---|
| [DEPLOY](DEPLOY.zh-CN.md) | Docker 部署、首次启动、生产注意事项 |
| [开发者指南](docs/DEVELOPER.zh-CN.md) | OAuth 流程、MCP 工具、Python/TypeScript 客户端 |
| [规范](SPECS.zh-CN.md) | 完整协议规范、PRD §14 威胁模型 |
| [安全](SECURITY.zh-CN.md) | 信任假设、漏洞披露 |
| [上线清单](docs/USER_SETUP_CHECKLIST.zh-CN.md) | 手把手：你在自己机器上要做的事 |
| [更新日志](CHANGELOG.zh-CN.md) | 发布历史 |

## 状态

v0.1.0 · 294 个测试全过。少数几项（真实 Postgres 迁移、真机 embedding 效果、真机 harness 握手）还需在真机上验证——见 [DEPLOY](DEPLOY.zh-CN.md)。

## License

MIT — 见 [LICENSE](LICENSE)。
