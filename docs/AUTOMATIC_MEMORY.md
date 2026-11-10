**English** | [中文](AUTOMATIC_MEMORY.zh-CN.md)

# Automatic memory preview

Porter can capture candidate facts after a Codex or Claude Code turn and look up approved memories before the next prompt. Each repository opts in separately. Capture and recall can be enabled independently.

## Connect one repository

1. Run the updated Porter server. In your local Python 3.12+ environment, run `python -m pip install .` from the same checkout to install `porter-auto-memory`.
2. Register a dedicated scope tag for the repository in the Porter owner panel. Create an OAuth client for each connected tool and grant `memory.read.<tag>`. Also grant `memory.write` to clients that capture. See the [OAuth setup](DEVELOPER.md).
3. Save the configuration outside the repository and point `PORTER_AUTO_MEMORY_CONFIG` at its absolute path. Supply the indicated OAuth access token in the tool process environment.

```json
{
  "projects": [
    {
      "root": "/absolute/path/to/my-project",
      "base_url": "http://127.0.0.1:8000",
      "tag": "my_project",
      "token_env": "PORTER_PROJECT_TOKEN",
      "recall": true,
      "capture": true
    }
  ]
}
```

Use a separate configuration or token environment variable for each tool when assigning independent credentials. Access tokens expire according to Porter's OAuth settings. This preview reads the supplied access token, so refresh it through the existing OAuth flow and relaunch the client when it expires. Use an HTTPS origin for a remote server.

## Enable the hooks

Merge the two event groups into your user-level Codex `hooks.json` or Claude Code `settings.json`. Preserve unrelated hooks. For Claude Code, replace both `porter-auto-memory codex` commands with `porter-auto-memory claude`. Use an absolute executable path if the hook process lacks the Python environment on its PATH.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "porter-auto-memory codex", "timeout": 5}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "porter-auto-memory codex", "timeout": 10, "async": true}]}
    ]
  }
}
```

Review and trust the new entries in Codex with `/hooks`. Launch the tool from the exact project directory in the configuration. Each worktree needs its own opt-in entry.

Try a synthetic decision such as “The project decided to run pytest on every release.” Approve the extracted fact in Porter's inbox. In a fresh session in the same configured project, ask how releases are tested. Recall requires an approved matching record and a valid scoped token.

## Data and controls

- Recall sends up to 1,000 characters of the submitted prompt to the configured Porter server. It returns up to three approved records as labelled, untrusted reference material. The connected assistant can send that context to its model provider.
- Capture sends up to 1,800 characters from the final assistant reply. Porter uses its configured [extractor](EXTRACTION.md) and always queues the extracted facts for review with the configured tag. Earlier chats, transcripts, files and tool outputs are outside this path.
- Common sensitive patterns are skipped as a best-effort filter. Only enable hooks for projects whose text you can share with your Porter instance and configured processing endpoints.
- A local SQLite receipt stores event hashes for seven days to suppress repeated capture attempts. Uncertain requests are not retried automatically. The receipt contains no text or tokens.
- Set `PORTER_AUTO_MEMORY_PAUSE=1` in the assistant's environment to pause both hooks, or turn off either switch in the configuration. Revoke the client in Porter to stop future access. Context already delivered to an assistant may remain in its transcript.

[Codex hooks](https://learn.chatgpt.com/docs/hooks) · [Claude Code hooks](https://code.claude.com/docs/en/hooks)
