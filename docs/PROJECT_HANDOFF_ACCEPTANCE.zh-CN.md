[English](PROJECT_HANDOFF_ACCEPTANCE.md) | **中文**

# 项目接力验收

Windows 真机使用 Codex CLI 0.160.0 和官方 DeepSeek Harness 内置 CLI 0.2.2-rc.1，DSH 使用现有 DeepSeek 账号模型。测试用独立客户端配置、Windows 凭据管理器、PostgreSQL 16 + pgvector 和 Docker 27.4.0。所有事实和文件出处均为合成数据。

| 检查 | 实测结果 |
|---|---|
| Codex → DeepSeek Harness | Codex 读取测试文件，留下已验证的结论、文件行号和下一步。批准后，新的 DSH 会话通过 MCP 读到同一条记录，并返回准确结论和出处。 |
| DeepSeek Harness → Codex | DSH 用托管 stdio 适配器提交由测试操作者验证的记录和出处。批准后，新的 Codex 会话在首轮利用 Hook 回填，返回准确结论和出处。 |
| 续期和断开 | 模拟本地令牌到期后，两端各自向真实 Porter 服务续期并更新系统凭据库。断开删除本地凭据，服务端拒绝已撤销令牌。 |
| 项目隔离、离线和已有 Hook | 换目录后新开的 Codex 会话未收到测试记录。Porter 不可达时，模型仍完成当前轮。安装后原有 Hook 仍执行。 |
| 纠错和撤销 | 下一次项目 API 检索返回版本 2。删除后，两个版本都没有被检索返回。 |
| Linux 候选包干净安装 | Debian Linux x64、Python 3.12、官方 Codex 0.160.0 和 GNOME Secret Service。新的 Codex 会话读到已批准记录，并续期系统凭据。 |

DSH 首轮需明确调用 MCP 搜索，Codex 通过 `UserPromptSubmit` Hook 回填。Claude Code 适配器有合成测试覆盖，此次实机双向接力采用 DSH。Linux DSH 和桌面 GUI 尚未纳入实测兼容范围。

## 性能与合成对照

- 两个方向都在接手方首轮成功，无需手动补充结论。Windows 托管适配器这次运行的四个模型回合各耗时 8.16–11.41 秒，这是对话耗时。
- Windows 每种路径串行回放 24 个合成 Hook 事件。不带空格的路径 p95 为 641 毫秒，带空格的路径为 656 毫秒。统计包括 Hook 进程启动与经本地 API 检索 Postgres。Hook 网络超时预算为 1.5 秒。
- [合成检索对照](GRAPHITI_SHADOW.zh-CN.md)中，旧排序在 18 次查询里有 16 次过期命中，现行 Postgres 为 0 次。Memmy v1.2.6 的 Memory Service 2.1.7 使用同一批事实，每题串行运行两次。普通、关系、时态 Recall@5 分别为 0.3333、0.0000、0.3333，HTTP p95 为 3,204 毫秒。测试的单用户命名空间配置返回了 2 次其他项目候选，撤销 ID 命中为 0。此对照使用本地 HTTP 服务和 OpenAI 向量模型，关闭代理历史扫描，并在临时构建中修复了 Windows 文件刷盘启动问题。结果只适用于该组数据和配置。
- 实机对话使用已有模型账号，未归集逐轮实付费用。Memmy 与旧 Porter 的对照限于检索服务，未覆盖原生首轮续接。[Graphiti 报告](GRAPHITI_SHADOW.zh-CN.md)记录了模型 tokens 实测量。

## 自动化覆盖

- Windows 源码测试 733 项通过，4 项按环境跳过。独立 Docker 验收另跑了真实 PostgreSQL 和部署 API，覆盖事件幂等、审批与变更记录、权限拒绝、版本、到期、快照和 API 容器重建。
- 在已备份的 14 项迁移数据库上实测升级到 015，再把备份恢复到独立数据库。原活动记录 ID 保留，数据库恢复到第 14 项迁移。
- 50 条合成结构化交接中，40 条有效样本均保留预期类型和出处，10 条无效样本均被拒绝。此项检验的是结构化采集校验。
- [Graphiti 影子评测](GRAPHITI_SHADOW.zh-CN.md)单独记录合成检索对照。Graphiti 未接入线上读路径。

## 暂停与回滚

对每个项目和客户端运行 `porter-connect pause <客户端> <项目目录>` 暂停回填。已经送达的上下文可能仍留在旧会话。用 `disconnect` 撤销凭据，再用 `uninstall` 移除托管 Hook 或 DSH 补丁文件。

升级前用 PostgreSQL 自定义格式备份数据库，并记录当时的软件包、镜像和配置。回退前暂停写入。把升级前备份还原到独立数据库，核对迁移数和预期的活动记录，再将旧软件包或镜像指向该数据库。保留升级后的数据库，直到核对完备份时间点之后的写入。迁移 015 增加采集队列，没有自动降级步骤。关闭 Graphiti 影子评测不会改变数据库连接或既有 Wiki 工具。
