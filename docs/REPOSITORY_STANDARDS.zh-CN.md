[English](REPOSITORY_STANDARDS.md) | **中文**

# 仓库规范

Porter 参考持续维护的 [FastAPI 官方全栈模板](https://github.com/fastapi/full-stack-fastapi-template)
采用统一 Python 项目配置、本地与 CI 共用检查、可选开发工具、自动化测试和容器验收。
这些工程做法不依赖模板中的 React 与 SQLModel；Porter 保留现有 FastAPI 面板与 asyncpg 仓库。

## 已实现的检查

| 项目 | 仓库实现 |
| --- | --- |
| 打包 | `pyproject.toml` 提供 PEP 621 元数据，版本读取 `VERSION`，声明 MIT 与项目链接；源码包和 wheel 包含迁移与面板资源 |
| 开发环境 | 轻量 `.[dev]`，显式安装 `.[ml]` 才下载模型依赖，Python 3.12 为基准 |
| 代码风格 | 固定 Ruff 版本，对应用、脚本、Python 客户端和测试统一检查、导入排序与格式化 |
| 本地检查 | 可选 pre-commit 钩子运行与 CI 相同的 Ruff 命令 |
| 跨平台文件 | `.editorconfig` 与 `.gitattributes` 约定 UTF-8/LF，忽略运行数据 |
| CI | 只读令牌、不保留 checkout 凭据、Action 固定提交、任务超时、取消被新提交替代的运行 |
| 验证 | Windows/Linux 测试、真实 Postgres 迁移、隔离 Docker 的 OAuth/MCP/快照/持久化验收，以及安装后 wheel 检查 |
| 依赖维护 | 核心/模型的已测试版本约束；Dependabot 每周提议 Python/Actions 更新，每月提议 Docker 更新，不自动合并 |
| 运行配置 | 明确的域名允许列表、TLS 代理公开来源、统一布尔开关与无效存储模式拒绝 |
| 贡献流程 | 中英同步说明、PR 验证清单、明确说明私密漏洞报告的可用条件 |

配置依据 [Ruff 官方文档](https://docs.astral.sh/ruff/configuration/)、
[Python 打包指南](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
与 [Dependabot 配置说明](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)。
当前约束文件不等于带哈希、跨平台解析的完整锁文件；更新依赖后需执行
[贡献指南](../CONTRIBUTING.zh-CN.md) 中的验证。

这些检查不能替代所有 harness 的实际接入或模型质量评测。
远程 Actions 与 Dependabot 要在改动进入 GitHub 后才运行；分支保护和私密漏洞报告
需由维护者在仓库设置中开启。目前没有宣称全项目类型检查通过；现有动态仓库与 SDK
接口需要逐步补齐类型后再引入这一门槛。
