<p align="center">
  <a href="CONTRIBUTING.md">English</a> &nbsp;|&nbsp; <a href="CONTRIBUTING.zh-CN.md"><b>中文</b></a>
</p>

# 为 Porter 做贡献

感谢你愿意帮忙。Porter 还很早期，刻意保持小而精——好的贡献都是聚焦的。

## 基本原则

- **两域模型。** 每个功能都要清楚自己服务哪个域：**personal**（个人记忆，敏感度分级 + 人工审核）或 **project**（项目的工作记忆，跨 harness 携带）。如果你的改动模糊了边界，请在 PR 里说明。
- **可移植性是核心，敏感度是次要。** 默认一切自动流动；审核队列是拦垃圾的，不是拦你的。
- **英文优先，双语文档。** 每个文档都是英文主文件 + `*.zh-CN.md` 姊妹文件，顶部互链。代码注释用英文。
- **仓库里不放垃圾。** 不提交 `.venv`、`__pycache__`、`.env`、快照和临时文件。提交前看一眼 `.gitignore`。

## 开发环境

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # 全量测试，必须全绿
```

测试默认跑 SQLite，并 **stub 掉** 重型可选依赖（`transformers`、`torch`、BGE-M3/Qwen3）。贡献代码不需要 GPU，也不需要下载模型。Docker/Postgres 路径用 `docker compose up` 验证（见 `docs/USER_SETUP_CHECKLIST.md`）。

## 协作流程

1. 非 trivial 的改动先开 issue——一段讲清 *为什么* 的话，胜过一个突然的 PR。
2. 从 `main` 拉分支：`feat/<短名>` 或 `fix/<短名>`。
3. PR 保持聚焦；一个 PR 只做一件事。
4. 每个 PR 必须：
   - `python -m pytest -q` 全绿（新行为要有测试），
   - `python -m pyflakes porter scripts` 干净，
   - 文档中英双语同步更新（README / SPECS / DEVELOPER / CHANGELOG，按需），
   - 填好 PR 模板，包括安全说明部分。

## 安全相关贡献

Porter 存的是用户在意的东西。改动如果涉及认证、OAuth scope、加密、E2EE、同步，或任何可能成为信任边界的地方，请跑 PRD §14（T1–T7）相关的对抗探针，并在 PR 里说明跑了哪些。漏洞报告走 `SECURITY.md` 的流程——**疑似漏洞永远不要开公开 issue**。

## 行为准则

友善、直接、善意推定。详见 `CODE_OF_CONDUCT.md`。
