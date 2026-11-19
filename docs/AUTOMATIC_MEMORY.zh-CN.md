[English](AUTOMATIC_MEMORY.md) | **中文**

# 自动记忆预览

Porter 可以在 Codex 或 Claude Code 回复结束时提取候选记忆，在下一条提问前检索已批准的内容。每个工作目录需单独开启，自动提取和回填可以分别开关。

## 使用 `porter-connect` 连接

1. 用 `python -m pip install .` 安装与服务端相同版本的 Porter，并保留该 Python 环境。Windows 使用系统凭据管理器。Linux 需安装 `secret-tool`，客户端须在同一个已解锁 Secret Service 的桌面或 D-Bus 会话中运行。
2. 在管理面板创建自定义项目标签，并为每个项目和客户端各注册一个 OAuth 客户端。设置回调为 `http://127.0.0.1:8766/porter/callback`。端口被占用时，注册相同主机和路径的新端口，并传入 `--callback-port`。
3. 在终端运行以下命令。在隐藏提示中输入 OAuth 客户端密钥，再用已登录 Porter 管理面板的浏览器打开终端显示的网址，批准项目权限。

   ```console
   porter-connect install codex
   porter-connect connect codex . --url http://127.0.0.1:8000 --tag my_project --client-id PORTER_CLIENT_ID --recall --capture
   porter-connect doctor codex .
   ```

4. 替换成 `claude`，用独立的客户端 ID 和密钥连接。可单选 `--recall` 或 `--capture`。在 Codex 的 `/hooks` 中核对并信任安装项。从已登记的工作目录新开会话，每个 worktree 单独连接。安装时保留其他用户 Hook。隔离测试可在子命令前指定 `--config` 和 `--hooks-file`。
5. 让一个 Agent 留下合成决策，批准提取结果，然后在另一客户端的新会话中询问同一决策。可用 `porter-connect doctor codex . --renew` 检查凭据续期和项目读取权限。

`porter-connect pause codex .` 暂停当前连接，`resume` 恢复。`porter-connect disconnect codex .` 撤销该 OAuth 客户端的令牌，删除本地登记和系统凭据。离线时可用 `--local-only`，恢复后从管理面板完成撤销。先断开该客户端的全部项目，再用 `porter-connect uninstall codex` 移除安装项。Claude Code 使用对应的 `claude` 命令。

## 手工预览配置

按[配置示例](AUTOMATIC_MEMORY.md#manual-preview-setup)安装 `porter-auto-memory`、添加专用项目标签，并为各工具配置带 `memory.read.<标签>` 权限的 OAuth 凭据。开启提取的工具还需 `memory.write`。把配置和凭据放在仓库外，分别通过 `PORTER_AUTO_MEMORY_CONFIG` 和配置里指定的环境变量传给客户端。

把[两个事件的配置](AUTOMATIC_MEMORY.md#enable-the-hooks)合并到对应工具的用户级 Hook 文件。Claude Code 的命令使用 `porter-auto-memory claude`。Codex 用 `/hooks` 核对并信任新条目。客户端须从已登记的工作目录启动。当前预览使用现有 OAuth access token，过期后通过原流程更新，再重启客户端。

- 回填时会向指定 Porter 实例发送至多 1,000 字符的提问，返回至多三条已批准的相关记忆。模型会收到标为不可信的历史参考资料。
- 提取时会发送本轮最终回复末尾至多 1,800 字符，由已配置的提取器产生候选，统一进入审核队列。该流程不读取旧聊天文件、工具输出或其他工作目录。
- 常见敏感模式会被跳过。只对允许将内容交给 Porter 和已配置处理服务的项目开启此功能。
- 可在配置中关闭任一开关，或启动客户端时设置 `PORTER_AUTO_MEMORY_PAUSE=1` 一起暂停。在 Porter 中撤销凭据后，客户端无法再取新内容，已送出的上下文可能仍留在客户端历史。
