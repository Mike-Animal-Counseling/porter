[English](AUTOMATIC_MEMORY.md) | **中文**

# 自动记忆预览

Porter 可以在 Codex 或 Claude Code 回复结束时提取候选记忆，在下一条提问前检索已批准的内容。每个工作目录需单独开启，自动提取和回填可以分别开关。

按[配置示例](AUTOMATIC_MEMORY.md#connect-one-repository)安装 `porter-auto-memory`、添加专用项目标签，并为各工具配置带 `memory.read.<标签>` 权限的 OAuth 凭据。开启提取的工具还需 `memory.write`。把配置和凭据放在仓库外，分别通过 `PORTER_AUTO_MEMORY_CONFIG` 和配置里指定的环境变量传给客户端。

把[两个事件的配置](AUTOMATIC_MEMORY.md#enable-the-hooks)合并到对应工具的用户级 Hook 文件。Claude Code 的命令使用 `porter-auto-memory claude`。Codex 用 `/hooks` 核对并信任新条目。客户端须从已登记的工作目录启动。当前预览使用现有 OAuth access token，过期后通过原流程更新，再重启客户端。

- 回填时会向指定 Porter 实例发送至多 1,000 字符的提问，返回至多三条已批准的相关记忆。模型会收到标为不可信的历史参考资料。
- 提取时会发送本轮最终回复末尾至多 1,800 字符，由已配置的提取器产生候选，统一进入审核队列。该流程不读取旧聊天文件、工具输出或其他工作目录。
- 常见敏感模式会被跳过。只对允许将内容交给 Porter 和已配置处理服务的项目开启此功能。
- 可在配置中关闭任一开关，或启动客户端时设置 `PORTER_AUTO_MEMORY_PAUSE=1` 一起暂停。在 Porter 中撤销凭据后，客户端无法再取新内容，已送出的上下文可能仍留在客户端历史。
