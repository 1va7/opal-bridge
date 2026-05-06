# Spec 004 — MCP server + cross-harness hooks

> 范围：把 agent-bridge 的核心能力暴露给所有 MCP-aware host；为 CC 与 Codex 都装 hook，让 session 翻译"自动发生"。
>
> 状态：MCP server 已落地（`src/agent_bridge/mcp_server.py`，6 工具，2 集成测试通过）。Codex notify hook 落地中。

## 1. MCP server

### 1.1 安装与配置

```bash
# 已经在 pyproject.toml 里，pip install -e . 自带
.venv/bin/pip install -e .

# 启动 stdio 服务（不要在 shell 里直接跑——给 MCP host 用）
agent-resume mcp serve

# 一键拿到 host 配置 JSON：
agent-resume mcp config-snippet
```

把 `config-snippet` 输出的 JSON 粘到对应 host 的 MCP 配置：

| Host | 配置文件 | 说明 |
|---|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` | mac 路径 |
| Claude Code | `.claude/mcp.json` 或全局 settings | 用 `claude mcp add` 命令 |
| Cursor | `~/.cursor/mcp.json` | |
| Cline (VS Code) | Cline settings | |
| Codex | `~/.codex/config.toml` 加 `[mcp_servers.agent-bridge]` 段 | |
| 任意 MCP host | 标准 stdio 模式 | command + args |

### 1.2 暴露的 6 个工具

| Tool | 输入 | 输出 |
|---|---|---|
| `list_sessions` | `harness, days?, limit?, include_translated?` | `{harness, count, sessions:[{session_id, path, cwd, first_prompt, size_bytes, mtime_iso}]}` |
| `translate_session` | `source_path, from_harness, to_harness, subagent_strategy?` | `{target_session_id, target_path, resume_command, warnings}` |
| `sync_now` | `direction?, days?, max_bytes?` | `{translated, skipped_existing, skipped_active, skipped_too_big, failed, failures}` |
| `find_session` | `session_id` | `{path, harness, cwd, mtime}` |
| `prepare_resume` | `target_harness, session_id` | `{target_session_id, target_path, harness, resume_command, cwd, translated, warnings?}` |
| `resume_with_prompt` | `target_harness, session_id, prompt, timeout_sec?` | `{output, stderr?, exit_code, target_session_id, command, translated}` |

### 1.3 典型调用模式

#### A. "我刚在 Codex 写了一段，回 Claude Desktop 接着聊"

User → Claude Desktop:
> 把我刚才在 codex 里那段代码讨论搬过来

Claude Desktop 调：
```
list_sessions(harness="codex", days=1, limit=5)        # 找最近的
prepare_resume(target_harness="claude-code", session_id="<UUIDv7>")
```
返回 `resume_command`。Desktop 把命令展示给用户，或者自己 spawn `claude --resume`.

#### B. "对面那段对话有用，但我懒得切窗口——就在这儿继续问"

```
resume_with_prompt(
    target_harness="codex",
    session_id="<CC session id>",
    prompt="基于刚才的讨论，给我三个具体方案"
)
```
直接返回模型答复（带翻译副作用，但调用方不感知）。

#### C. 后台保持双向同步

```
sync_now(direction="both", days=1)        # 一次性
```
调用方按需轮询，或者交给系统的 watch 守护进程。

### 1.4 测试

```bash
.venv/bin/python -m pytest src/tests/test_mcp_server.py -v
```

两个集成测试覆盖：
- 启动 server，list_tools 返回 6 个工具
- 调用 sync_now / list_sessions，断言返回 dict 字段

## 2. Codex hook（notify）

待研究 agent 报告完成后落地。

## 3. CC hook（已存在）

`agent-resume install-hook --target claude-code` 写入 `~/.claude/settings.json` 的 Stop chain。每次 CC 一段对话停下来就触发 `agent-resume sync --direction cc-to-codex`。
