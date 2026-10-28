<p align="center">
  <a href="CONTRIBUTING.md">English</a> &nbsp;|&nbsp; <a href="CONTRIBUTING.zh-CN.md"><b>中文</b></a>
</p>

# 为 Porter 做贡献

感谢你愿意帮忙。Porter 还很早期，刻意保持小而精——好的贡献都是聚焦的。

## 基本原则

- **两域模型。** 每个功能都要清楚自己服务哪个域：**personal**（个人记忆，敏感度分级 + 人工审核）或 **project**（项目的工作记忆，跨 harness 携带）。如果你的改动模糊了边界，请在 PR 里说明。
- **明确自动化边界。** 分级模式可立即激活符合条件的项目写入；个人/敏感/冲突写入和 Ollama 提取结果需要审核。严格模式将所有新写入排队。保留这些边界，策略改动应在 PR 说明。
- **英文优先，双语文档。** 每个文档都是英文主文件 + `*.zh-CN.md` 姊妹文件，顶部互链。代码注释用英文。
- **仓库里不放垃圾。** 不提交 `.venv`、`__pycache__`、`.env`、快照和临时文件。提交前看一眼 `.gitignore`。

## 开发环境

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -c requirements-lock.txt -e ".[dev]"
python -m pre_commit install
python -m ruff check .
python -m ruff format --check --quiet .
python scripts/check_repository.py
python -m pytest -q
python -m build
```

使用 Python 3.12（`.python-version` 与 CI 的基准版本）。默认测试使用**内存仓库**，重型模型后端使用桩替代，不需要 GPU 或下载模型。开发依赖只含测试、代码检查、提交钩子和打包工具；大型模型需显式安装 `.[ml]`。Docker 默认使用 CPU 版 PyTorch。

运行 `python scripts/verify_deployment.py` 可执行隔离的 Docker 验收（要求 Compose 2.24.4+），覆盖真实 Postgres 迁移、OAuth、HTTP/stdio MCP、快照与 API 容器重建后的持久化。它使用 `.env.review`、独立端口和 `porter-review` 项目，保留命名卷；加 `--keep-running` 可保留 18000 端口的面板。常规测试在没有相应 `PORTER_TEST_*` 环境变量时跳过外部服务测试。

Ruff 和 pytest 配置统一放在 `pyproject.toml`，包版本以 `VERSION` 为准。用 `python -m ruff check . --fix` 与 `python -m ruff format .` 自动整理代码；提交钩子和 CI 使用同样的检查命令。CI 还构建源码包和 wheel，并在仓库目录外验证安装后的迁移与静态资源。

`requirements-lock.txt` 与 `requirements-embedder-lock.txt` 是已测试的版本约束，不是带哈希的通用锁文件。依赖更新后，在干净的 Python 3.12 环境解析依赖，运行 `pip check`、单元/集成测试和 Docker 验收，再更新对应约束；未执行的检查不能标为通过。

## 协作流程

1. 非 trivial 的改动先开 issue——一段讲清 *为什么* 的话，胜过一个突然的 PR。
2. 从 `main` 拉分支：`feat/<短名>` 或 `fix/<短名>`。
3. PR 保持聚焦；一个 PR 只做一件事。
4. 每个 PR 必须：
   - `python -m pytest -q` 全绿（新行为要有测试），
   - `python -m ruff check .` 与 `python -m ruff format --check --quiet .` 通过，
   - 运行 `python scripts/check_repository.py`，检查本地文档链接、双语对应与打包约定，
   - 文档中英双语同步更新（README / SPECS / DEVELOPER / CHANGELOG，按需），
   - 填好 PR 模板，包括安全说明部分。

## 安全相关贡献

Porter 存的是用户在意的东西。改动如果涉及认证、OAuth scope、加密、E2EE、同步，或任何可能成为信任边界的地方，请跑 PRD §14（T1–T7）相关的对抗探针，并在 PR 里说明跑了哪些。漏洞报告走 `SECURITY.md` 的流程——**疑似漏洞永远不要开公开 issue**。

## 行为准则

友善、直接、善意推定。详见 `CODE_OF_CONDUCT.md`。

发布与仓库管理见[维护者指南](docs/MAINTAINERS.zh-CN.md)。
用[验收门槛](docs/ACCEPTANCE.zh-CN.md)区分自动化覆盖与真实用户验证。
