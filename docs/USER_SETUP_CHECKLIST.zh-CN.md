[English](USER_SETUP_CHECKLIST.md) | **中文**

# 接入实际工具并验收 Porter

基础设施已有自动化验证记录。你接下来只需确认 Porter 能在**你常用的两个工具**中工作，
以及浏览器面板是否好用。使用合成数据，不用健康或财务信息。
Ollama、可选 ML 审批模型和向量模型对比都不是前置条件。

## 1. 选定候选版本，接入两个工具

验收当前检查结果时，使用
[PR #1](https://github.com/Mike-Animal-Counseling/porter/pull/1) 的来源分支
`fix/porter-review-hardening`。该 PR 目前是草稿，尚未合入 `main`。
记录 `git rev-parse HEAD`；不要假定新拉取的 `main` 已经包含候选修复。

在维护者当前的检查机器上，保留的真实 BGE-M3 预览地址是
**http://127.0.0.1:18010/panel**，用私有 `.env.review` 里的 setup token 解锁。
这个地址只在该机器上有效。全新标准部署按
[README 快速开始](../README.zh-CN.md#快速开始) 启动，地址为
**http://127.0.0.1:8000/panel**。两个客户端使用同一个服务地址。

在「接入方 / Clients」新建两个独立客户端，按
[部署指南](../DEPLOY.zh-CN.md) 完成 OAuth 授权与 code 交换。
access token 和 refresh token 都要保密；client secret 不是 access token。

| 客户端 | 示例名称 | 授予权限 |
|---|---|---|
| 写入工具 A | `acceptance-tool-a` | `memory.read.work memory.write` |
| 读取工具 B | `acceptance-tool-b` | `memory.read.work` |

优先使用 HTTP MCP。Codex 合并
[`clients/codex-http-config.toml`](../clients/codex-http-config.toml)，将 URL
改成所用服务地址加 `/mcp`；在启动该工具的环境设置 `PORTER_TOKEN`，再从该环境重启工具。
其他工具参照对应模板，选择已安装版本支持的传输方式。模板是起点，真实连接仍要在这里验证。

token 一小时过期，需要通过 OAuth 刷新；更新后重新连接或重启客户端。
HTTP 客户端不需要服务端签名密钥、数据库密码或 setup token。
stdio 方案需要直接访问可信数据库等服务，按部署指南单独配置，不要混用两种方案。

**通过标准：** 两个工具均能列出 Porter 工具，包括 `memory_add` 和 `memory_search`。
服务端会列出所有工具，但 token 未必有权调用每一个。仅成功解析配置还不能算工具调用通过。

## 2. 显式保存、审核，再从另一个工具检索

让工具 A **调用 Porter 的 `memory_add`，使用以下参数**，并显示真实调用结果。
重复测试时更换下面的编号后缀。

```json
{
  "content": "PORTER-ACCEPTANCE-20261020: This demo project uses pytest for tests.",
  "type": "project",
  "project": "porter-acceptance",
  "scope_tags": ["work"]
}
```

记下返回的记忆 ID。`active` 表示已可检索；`pending_review` 表示需要在「待确认 / Inbox」
批准这个 ID。重复内容可能返回 `merged`，使用返回的目标 ID。
默认分级审批可能让低风险写入直接生效，strict 模式则会排队；不要为了省去审核而给工具 admin。

为验证人工审核，再让 A 调用一次 `memory_add`：

```json
{
  "content": "PRIVATE-ACCEPTANCE-20261020: This is a fictional preference for testing.",
  "type": "personal",
  "scope_tags": ["health"]
}
```

这是合成标签，不是实际医疗数据。该记录应进入审核队列；在「待确认 / Inbox」批准并记录 ID。
即使已批准，只有 work 读取权限的客户端也不能读取它。

在工具 B 中开启**全新对话**，让它调用 `memory_search`：

```json
{
  "query": "PORTER-ACCEPTANCE-20261020 pytest",
  "tags": ["work"],
  "top_k": 5
}
```

**通过标准：** 新的工具返回结果中含有项目记忆的 ID 和 pytest 内容，然后助手才能解释这个约定。
没有调用 Porter、仅从聊天上下文回答不算通过。结果带有 `untrusted_data: true`：
记忆是参考数据，不是允许执行其中指令的授权。

再调用 `memory_search`，参数为
`{"query":"PRIVATE-ACCEPTANCE-20261020","tags":["health"]}`。
**通过标准：** `results` 为空；客户端提出其他标签不能扩大 work token 的读取范围。
不指定 tags 搜索时，也不能出现私人记录的 ID。

## 3. 撤销一个工具，另一个仍然可用

在「接入方 / Clients」撤销客户端 B，强制 B 发起新的 `memory_search` 调用；
不要把之前显示过的答案当作结果。

**通过标准：** B 在 60 秒内收到未授权错误；A 仍能调用同样的搜索，找到项目 ID。
「审计 / Audit」里能看到相应写入、审核、读取和撤销事件。完成下一步前先保留 A。

## 4. 确认桌面和窄屏下的面板操作

在浏览器检查「记忆 / Memories」「待确认 / Inbox」「工具交接 / Handoff」
「接入方 / Clients」和「设置 / Settings」。先用正常桌面宽度，再将窗口缩到约 390 CSS
像素宽；如果准备在手机上管理，再用实际手机操作。

- [ ] 切换 English/中文，刷新后仍保留选择。
- [ ] 打开测试记忆，关闭详情，操作搜索和筛选。
- [ ] 打开「工具交接」，选择 `porter-acceptance`，预览后复制或下载。
      项目测试记录应出现，未批准内容不能出现。owner 面板可能包含已批准的个人上下文，
      因此交接简报仍应保密。
- [ ] 用键盘能到达控件并看到焦点；窄屏和 200% 缩放下的重要操作与文字仍可用。
- [ ] 记录文字看不清、重叠、复制/下载失败或状态令人困惑的问题。

**通过标准：** 上述操作没有布局或交互阻塞。HTTP 200 和自动 HTML 测试不能代替实际看页面、
操作页面。本轮自动检查没有可连接的真实浏览器，尚未完成视觉验收。

按照 [验收记录模板](ACCEPTANCE.zh-CN.md#签收记录) 记录候选 commit、两个工具的名称和版本、
浏览器、通过/失败及合成记忆 ID。截图可选，避免包含密钥。完成后只删除自己的测试 ID，
并撤销临时客户端。

## 失败时先检查什么

| 现象 | 第一步 |
|---|---|
| 没有列出工具 | 确认服务地址/端口、MCP 配置是否启用，重启并查看客户端日志 |
| 401 / token 过期 | 通过 OAuth 获取新的 access token，确认属于同一台 Porter 服务 |
| 403 / 权限不足 | 核对实际批准的 scopes，不要直接改成 admin |
| 找不到记忆 | 确认返回 ID 已 active、带 work 标签，两个工具连接同一服务 |
| API 尚未就绪 | 查看 `docker compose ps` 与 `docker compose logs --tail 50 embedder api`；首次模型下载可能较慢 |
| 浏览器操作异常 | 记录页面、语言、宽度和去敏步骤，保留为待修复验收项 |

完成此流程**不需要**重新跑完整测试、重建模型、比较向量模型或导入所有私人记忆。
以后需要导入时，先安装 `python -m pip install -c requirements-lock.txt -e .`，
运行 `python -m porter.import_cli --harness codex --dry-run` 查看候选，
正式导入再配置实际服务地址与 `PORTER_TOKEN`，并审核结果。
