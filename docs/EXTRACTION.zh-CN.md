[English](EXTRACTION.md) | **中文**

# 从聊天提取候选记忆

`memory_capture` 把调用方提供的一段聊天整理成候选记忆。它不会自动录制所有
对话；由接入的工具决定何时发送文本。提取后的记录进入收件箱或既有审批流程。

默认 `PORTER_LLM_EXTRACTOR=stub` 使用中英文关键词规则，无需语言模型。
BGE-M3 继续独立负责记忆检索的向量表示。日常使用可以保持这些默认设置。

## 可选的本地 Ollama

在自己的 Ollama 服务中安装模型后，配置 Porter：

```dotenv
PORTER_LLM_EXTRACTOR=ollama
PORTER_OLLAMA_MODEL=<已安装的模型名称>
PORTER_OLLAMA_URL=http://localhost:11434
PORTER_LLM_TIMEOUT=30
```

Docker Desktop 使用 `http://host.docker.internal:11434`；Linux 容器需填写私有
网络中可达的服务地址。仓库默认不会安装模型或启用 Ollama。修改环境变量后，
重启 API/stdio 进程。模型需支持 Ollama chat 的结构化输出及 `think=false`。
提取器请求 16K 上下文，源 JSON 限制为 8,000 个 UTF-8 字节，留出指令和输出
空间。长对话需拆分为多次调用，超出限制会在请求模型前直接拒绝。

提取器只向配置的服务发送本次提交的文本。使用本机端点时在本地处理；配置
远程端点时，该端点会收到文本。API 和 stdio 若都用于提取，应配置一致；
HTTP MCP 使用 API 的设置。

模型输出经过大小限制和 schema 校验，置信度固定为保守的 0.7，**所有 Ollama
候选记忆都需要人工确认**，包括项目记忆。模型不能指定项目/工作区标签或授权；
调用方提供的 source tags 仍需通过工作区角色检查。PostgreSQL 中一批提取记录
以事务保存；结构或权限校验失败不会保存任何记录。异常返回
`extractor_unavailable`，错误信息不包含聊天原文。可选 wiki 使用相同后端，
提取服务故障时检索退回普通搜索。

实现依据 [Ollama 官方 chat API](https://docs.ollama.com/api/chat)。
OpenAI 提取器仍未实现。

## 验证边界

测试覆盖畸形输出、超时、超大响应、标签注入、人工确认及后台执行。
可显式启用真实模型测试，只发送固定的合成对话：

```powershell
$env:PORTER_TEST_OLLAMA_URL='http://localhost:11434'
$env:PORTER_TEST_OLLAMA_MODEL='<已安装的模型名称>'
python -m pytest -q tests/test_ollama_integration.py
```

2026-10-20 使用本机 Ollama 0.33.2 和 Qwen3 0.6B，四段中英文合成样例均返回
合法结构，也验证了 MCP 提取后进入待确认队列的流程。但该小模型错误地把
一句问候也当成了持久事实。它只能证明接入可运行，实际提取质量仍需针对目标
数据评估。原始结果见 [小样本记录](extraction-eval.2026-10-20.json)。
