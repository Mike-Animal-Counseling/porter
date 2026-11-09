# Porter

**在 AI 编程工具之间共享项目上下文。**

[English](README.md) · Alpha · MIT

Porter 是面向 AI 编程助手的自托管记忆服务。在一个工具里保存项目决策，再通过 MCP 从另一个工具找回。数据库由你管理，每个客户端的读写权限也由你决定。

## 实际怎么用

**Codex 写入一条项目决策 → Porter 保存 → OpenCode 在新对话中找回。** [真实客户端验收](docs/NATIVE_CLIENT_ACCEPTANCE.2026-10-22.zh-CN.md)确认两个工具拿到了同一条记忆 ID。

- **审核写入。** 敏感或冲突的内容进入审核收件箱。严格模式下，每条新写入都需审核。
- **限制访问。** 分别为客户端授予可撤销的 OAuth 凭据，支持读取、写入和按标签限读。
- **本地保存。** 用 PostgreSQL 和 pgvector 检索记忆，也可在管理面板导出交接简报和备份。

当前 Alpha 面向单一所有者。助手需主动调用 MCP 工具，普通记忆以明文保存在你的服务器上。部署边界见[安全策略](SECURITY.zh-CN.md)。

## 快速开始

需要 Git、Python 3.12+ 和 Docker（Compose 2.24.4+）。首次启动会下载镜像和模型，实测模型缓存约 3.8 GB。

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git
cd porter
python scripts/setup_local.py
docker compose up --build -d
docker compose ps
```

服务健康后打开[本地面板](http://127.0.0.1:8000/panel)。如提示解锁，Windows 双击 `Open-Porter.cmd`，macOS/Linux 运行 `python scripts/open_panel.py`。

按[部署指南](DEPLOY.zh-CN.md)接入第一个 HTTP MCP 客户端。用[接入清单](docs/USER_SETUP_CHECKLIST.zh-CN.md)验证一次保存和检索。

## 文档

- [开发者指南](docs/DEVELOPER.zh-CN.md)和[协议规范](SPECS.zh-CN.md)
- [验收标准和测试记录](docs/ACCEPTANCE.zh-CN.md)
- [贡献指南](CONTRIBUTING.zh-CN.md)和[维护者指南](docs/MAINTAINERS.zh-CN.md)

[MIT 许可](LICENSE) · [第三方组件](THIRD_PARTY.zh-CN.md)
