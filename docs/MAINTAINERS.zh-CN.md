[English](MAINTAINERS.md) | **中文**

# 维护者指南

Porter 是 alpha 项目。CI 通过只能证明某个具体提交通过了相应检查，不能代表所有产品流程完成。
[验收记录](ACCEPTANCE.zh-CN.md)和[检查报告](REVIEW.zh-CN.md)应区分自动、人工和未验证项目。
`initial push (1/13)` 到 `(13/13)` 表示上传批次，不是完成里程碑。

## 仓库管理

这些设置位于 GitHub；给分支添加 YAML 不会自动开启它们。

- 仓库 description 和 topics 应与 README 一致。只有维护公开站点时才填写 homepage；
  localhost 不是项目主页。仓库采用 MIT 许可证，Python 包名是 `porter-memory`。
- 保持私密漏洞报告、依赖漏洞提醒、自动安全修复提案和 secret push protection 开启。
  更新提案仍需审查，并不等于修复已经验证。
- 为 `main` 配置必须通过 PR 和 CI 的保护规则。检查项来自 `.github/workflows/ci.yml`：
  `quality`、`package`、`test (ubuntu-latest)`、`test (windows-latest)`、`postgres`、`deployment`。
  应选 Actions 实际报告的名称。有第二位维护者时要求他人审查；避免设置导致唯一维护者无法处理紧急修复。
  先准备针对 `refs/heads/main` 的规则、审查绕过权限并确认六项检查已有运行记录，再启用。
  若 Muse 或其他自动化仍直接推送，应先与其约定 PR 流程；强制 PR 会阻止原来的直接推送。
- 发布可用于行为投诉的私密维护者联系方式。私密安全公告只处理安全问题，不能代替行为投诉渠道。
  不要编造邮箱或承诺无法持续履行的响应时间。

2026-10-20 已配置 description/topics、私密漏洞报告、依赖漏洞提醒和自动安全修复提案；
secret scanning 与 push protection 已提前开启。当时 `main` 尚无保护规则/rulesets，
也没有已发布 release。发版时应复核实时设置；这是有日期的记录，不是永久保证。

## 审查改动

使用 Python 3.12，按[贡献指南](../CONTRIBUTING.zh-CN.md)安装轻量开发依赖，在仓库根目录运行：

```bash
python -m ruff check .
python -m ruff format --check --quiet .
python scripts/check_repository.py
python -m pre_commit validate-config
python -m pip check
python -m pytest -q
python -m build
```

仓库检查器只用标准库，不联网、不写文件；检查 Markdown UTF-8、本地文件链接和大小写、
双语对应链接、必要社区文件、包元数据和迁移编号唯一性。它不校验远端 URL、
Markdown 锚点，也无法判断文字描述是否真实。

未配置 `PORTER_TEST_*` 时，普通测试会跳过外部服务测试，因此单元测试全绿不等于部署验收通过。
改动鉴权、持久化、迁移、容器或 MCP 时还应运行：

```bash
python scripts/verify_deployment.py
```

脚本使用 `.env.review` 创建隔离的 `porter-review` Compose 项目，执行真实 PostgreSQL/API
测试并验证 API 重建持久化。它保留命名卷；加 `--keep-running` 还会保留运行的服务。
直接运行 PostgreSQL 测试时，使用名称以 `_test` 结尾的专用数据库，不能指向用户生产库。
不要删除用户卷来让测试通过。真实 BGE-M3 质量和真实工具对话与 hash 基础设施验收是不同项目，
应记录实际执行的是哪一种。

已知漏洞公告核对与运行验收是不同项目。有日期的[依赖审计](DEPENDENCY_AUDIT.zh-CN.md)
保留原始发现、修复与完整的最终 OSV 复查及覆盖限制。依赖改动后应刷新该记录，
不能只用零条 Dependabot 告警作为依据。

界面改动要检查桌面/手机、键盘、空状态/错误状态与无障碍。客户端适配应至少在目标工具中完成一次
真实对话。记录浏览器/工具版本及限制。验收材料不能包含真实记忆、凭据、Cookie 或未脱敏 `.env`。

## 准备发布

目前没有自动发布工作流，也不代表已经发布到 PyPI。CI 构建的 Python 分发包只是审查材料；
维护者需要有意识地在以下检查后创建 GitHub release。

1. 选择版本，修改唯一版本来源 `VERSION`，并检查构建/安装后的 `porter.__version__`。
   预期验收条件完成前，项目应继续标为 alpha。
2. 将中英 `CHANGELOG` 中对应 Unreleased 内容整理为带日期的版本章节，说明最终行为、兼容性、
   迁移要求与已知限制。按需更新 README 和验收链接；人工待验收项目不能改成通过。
3. 在隔离环境对新库和已备份的旧版本数据库执行迁移。已发布迁移名称保持稳定，新增行为用新迁移。
   回退数据库可能需要恢复备份，不能只启动旧镜像。升级真实实例前应备份并演练恢复。
4. 从候选提交构建源码包和 wheel，像 `package` CI 一样在 checkout 外验证安装、迁移、面板资源和
   命令行入口。检查源码包没有私有数据、凭据、除 `.env.example` 外的 `.env` 文件、模型缓存或本地验收
   输出。使用已测试约束运行 `pip check`。
5. 确认所有必需 CI 在同一个候选提交上通过，并完成适用的[验收门槛](ACCEPTANCE.zh-CN.md)。
   草稿 PR 和配置解析成功不代表真实工具对话可用。安全报告先私密处理，再协调公开细节。
6. 审查/合并后，在已验收提交上创建附注 `vX.Y.Z` 标签和对应 GitHub release；alpha 发布标记为
   prerelease。发布说明包含双语变化、升级/备份步骤、已测试 Python/Docker/客户端版本和剩余限制。
   若附分发包，使用同一提交构建的产物，并附 SHA-256 校验值。

标签、合并和发布是独立的显式操作，检查器和 Docker 验收脚本都不会执行它们。
未来若加入 PyPI 分发，应先明确包名所有权、trusted publishing 和发布来源，再添加自动发布流程。

## 发布后

在干净环境从实际发布产物安装，核对标签、发布说明和下载校验值与已验收提交一致。
用虚构数据复现并分流 bug；修复后重跑相关检查。保留明确限制，避免承诺所有集成均可用。
