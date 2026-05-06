# ARCHITECTURE — agent-bridge

> 翻译器与 canonical session format 的设计文档。
> 本文件是 `docs/tool-mapping.md` 的工程化对应：mapping 决定**字段怎么对**，本文档决定**代码怎么组织**。
>
> 当本文档与 tool-mapping 冲突时，以 tool-mapping 为准（它直接对接两份实证调研）；本文档负责把那些规则落进可维护的 src/ 结构里。

---

## 1. 设计原则

1. **Canonical IR 优先，不写 N×M 直连**。每个 harness 配一对 adapter（ingest / render）。增加第 N 个 harness 只需 +2 个文件，不需要改其它任何 adapter。
2. **Lossless 优先；明示 lossy**。canonical IR 必须能表达每个 harness 的所有 wire-level 信息。无法表达的部分写到 `source_metadata` opaque blob 里保留——即使本翻译器看不懂，将来另一个 harness 也许能识别。
3. **结构对称**。CC 和 Codex 在 canonical 中地位完全平等。**不把 CC 当"主"或者把 Codex 当"主"**——这是常见错误，会让反方向翻译成为二等公民。
4. **副文件是一等公民**。CC 的 subagents/ tool-results/ tasks/，Codex 的 subagent jsonl，都不是边角料——canonical IR 必须把它们结构化进来。
5. **可被任何脚本 5 行读懂**。canonical 是 JSON，schema 写在版本字段里。不引入 protobuf / capnp / sqlite-only / SaaS 依赖。
6. **可在 IDE 里 grep 找到 every 字段的 origin**。adapter 内部禁止"魔法"——每个赋值要么有源数据来源注释，要么有"intentionally synthesized"标注。
7. **禁止破坏性副作用**。本翻译器永远不写 `~/.claude/projects/` 或 `~/.codex/sessions/` 之外的目录；永远不修改用户的真实 session（只读源、只写目标）。

---

## 2. 数据流总图

```
                    ┌────────────────┐
   CC jsonl ───────▶│ adapters/      │──┐
   (含 sidecar)     │   claude_code/ │  │
                    │     ingest.py  │  │
                    └────────────────┘  │
                                        ▼
                    ┌──────────────────────────────────┐
                    │   canonical IR  (in-memory)      │
                    │   ─ Session header               │
                    │   ─ moments[]                    │
                    │   ─ subagent_transcripts{}       │
                    │   ─ artifacts[] (大输出/图片)    │
                    │   ─ source_metadata{} (opaque)   │
                    └──────────────────────────────────┘
                                ▲                  │
                                │                  ▼
                    ┌────────────────┐    ┌────────────────┐
   Codex jsonl ────▶│ adapters/      │    │ adapters/      │──▶ Codex jsonl
   (含 subagent)    │   codex/       │    │   codex/       │   (落到 sessions/YYYY/MM/DD/)
                    │     ingest.py  │    │     render.py  │
                    └────────────────┘    └────────────────┘

                          ▲                       ▲
                          │                       │
                    ┌─────┴───────────────────────┴─────┐
                    │  src/translator.py                │
                    │   ─ pipeline(ingest → IR → render)│
                    │   ─ 三档 fidelity mode            │
                    │   ─ multi-file DAG 合并 (CC)      │
                    │   ─ subagent 策略选择             │
                    └───────────────────────────────────┘
                          ▲                       ▲
                          │                       │
                          └───────  CLI ──────────┘
                                    │
                            agent-resume
                            ├ export
                            ├ import
                            └ inspect
```

---

## 3. Canonical IR 设计

### 3.1 顶层结构

```jsonc
{
  "schema_version": "1.0.0",
  "session": { /* 见 §3.2 */ },
  "moments": [ /* §3.3 */ ],
  "subagent_transcripts": { /* §3.5 */ },
  "artifacts": [ /* §3.6 */ ],
  "source_metadata": { /* §3.7 */ }
}
```

文件存为 `.canonical.json`（单文件即一个 session），UTF-8 LF。

### 3.2 `session` header

```jsonc
{
  "id": "01J9XXXXXXXXXXXXXXXXXX",         // canonical session ID (ULID 优先；UUIDv7 也可)
  "source_harness": "claude-code|codex|hermes|other",
  "source_session_id": "<原 ID>",          // 原 harness 中的 ID（CC 是 v4 UUID，Codex 是 v7 UUID）
  "source_session_path": "<原文件绝对路径>",
  "cwd": "/Users/va7/Desktop/foo",
  "git": {
    "branch": "main",
    "commit": "abc123...",                  // 可空
    "repo_url": null                        // 可空
  },
  "model_hint": {
    "provider": "anthropic|openai|other",
    "name": "claude-opus-4-6",
    "reasoning_effort": null                // Codex 才有
  },
  "started_at": "2026-05-06T09:00:00.000Z",
  "ended_at": "2026-05-06T11:30:00.000Z",
  "duration_ms": 9000000,
  "permissions": {
    "approval": "default|on-request|never|granular|untrusted",
    "sandbox": "read-only|workspace-write|danger-full-access|external-sandbox",
    "writable_roots": ["/path/..."],          // 可空
    "network_access": false
  },
  "skills_inventory": [                       // 启动时该 session 能用到的 skill 名（snapshot）
    {"name": "feishu-doc-writer", "path": "<source>", "content_md": "<可空>"}
  ],
  "mcp_servers": [
    {"name": "...", "tools": ["...", "..."]}
  ],
  "memory_files": [                           // CLAUDE.md / AGENTS.md 之类的
    {"path": "/Users/va7/.claude/CLAUDE.md", "scope": "user", "content_md": "<内容>"}
  ],
  "stats": {
    "tokens_in": 0,
    "tokens_out": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "tool_call_count": 0
  }
}
```

**字段必填性**：
- 必填：`id`, `source_harness`, `source_session_id`, `cwd`, `started_at`
- 可空：其余皆可 null（adapter 不知道时不强行编造）

### 3.3 `moments[]`

每个 moment 是对话/会话上的一个原子事件。**严格按时间戳单调递增排序**。

通用字段：
```jsonc
{
  "ts": "2026-05-06T09:00:01.234Z",
  "kind": "<one of below>",
  "source_ref": {                       // 可追溯到原 jsonl 的哪一行
    "file": "/path/to/cc-session.jsonl",
    "line": 42,
    "uuid": "<原 uuid>"                  // CC 有，Codex 没
  },
  "agent_scope": "main|subagent:<name>",  // sidechain=true 的事件用 subagent:<name>
  "lossy": false,                         // 此 moment 是否在翻译过程中已发生有损降级
  "lossy_reason": null                    // lossy=true 时给出原因
  // 后面跟 kind-specific 字段
}
```

#### Kind 列表

##### 3.3.1 `user_text`

用户发的文本消息。
```jsonc
{
  "kind": "user_text",
  "text": "...",
  "promptId": "<可空>",                   // CC 才有
  "attachments": [                        // 内联引用的附件 ID
    {"kind": "file", "ref": "artifact:abc"}
  ]
}
```

##### 3.3.2 `assistant_text`

模型生成的文本。
```jsonc
{
  "kind": "assistant_text",
  "text": "...",
  "phase": "commentary|final_answer|null"  // 推断或原值
}
```

##### 3.3.3 `thinking`

extended thinking 文字。signature/encrypted_content 不存在 canonical 里——只存文字。
```jsonc
{
  "kind": "thinking",
  "text": "...",
  "format": "plaintext|summary|redacted",
  "lossy": true,                          // 总是 true，因为 signature/encrypted_content 不可保留
  "lossy_reason": "harness-specific signature/encrypted_content not portable"
}
```

##### 3.3.4 `tool_call`

```jsonc
{
  "kind": "tool_call",
  "tool": "shell",                        // canonical 工具名（见 §3.4）
  "call_id": "toolu_X" 或 "call_X",        // 沿用原 ID
  "args": {                                // canonical args schema (见 §3.4)
    "command": "ls /tmp"
  },
  "wire_native": {                         // 原 wire 名 + 原 args，给反向翻译留底
    "harness": "claude-code",
    "name": "Bash",
    "input": {"command":"ls /tmp", "description": "list tmp"}
  }
}
```

##### 3.3.5 `tool_result`

```jsonc
{
  "kind": "tool_result",
  "call_id": "toolu_X",                    // 配对的 tool_call.call_id
  "output_text": "...",                    // 文本结果（必填）
  "output_blocks": [                       // 可选：图片 / 富内容
    {"type": "image", "ref": "artifact:abc"}
  ],
  "is_error": false,
  "exit_code": 0,                           // shell 类工具才有
  "duration_ms": 123,
  "external_ref": "/tool-results/abc.txt"   // 大输出外置时
}
```

##### 3.3.6 `subagent_call`

子 agent 调用 + 结果。子 agent 内部 transcript 不内联在 moment 里，而是放到 `subagent_transcripts[<agent_id>]`（§3.5）。

```jsonc
{
  "kind": "subagent_call",
  "agent_id": "agent-X",
  "agent_type": "general-purpose",
  "task_description": "Search for X",
  "prompt": "<full prompt>",
  "model_hint": "sonnet",
  "run_async": false,
  "result": {                              // null 表示尚未完成 (异步未回来)
    "text": "<final markdown>",
    "stats": {"tools": 5, "tokens": 1234, "duration_ms": 30000}
  },
  "transcript_ref": "subagent_transcripts.agent-X"  // 指向 §3.5 中的 key
}
```

##### 3.3.7 `attachment`

由 harness 注入到对话上下文的附加内容（CC 的 `attachment` 行 + Codex 的 developer message envelope）。

```jsonc
{
  "kind": "attachment",
  "subtype": "memory|skill_listing|skill_invoked|task_reminder|file|image|edited_file|date_change|queued_command|command_permissions|env",
  "data": {                                // subtype-specific
    // memory: {"path": "...", "scope": "user|project", "content_md": "..."}
    // skill_listing: {"skills": [{"name":"...","description":"..."}]}
    // file: {"path": "...", "ref": "artifact:abc"}
    // env: {"cwd":"...", "platform":"darwin", "date":"2026-05-06"}
  }
}
```

##### 3.3.8 `mode_change`

permission_mode / collaboration_mode 切换。
```jsonc
{
  "kind": "mode_change",
  "from": "default",
  "to": "plan",
  "fields_changed": {
    "approval": "on-request",
    "sandbox": "read-only"
  }
}
```

##### 3.3.9 `plan_update`

CC TaskCreate/Update 系列，或 Codex update_plan。
```jsonc
{
  "kind": "plan_update",
  "items": [
    {"id": "task-1", "title": "...", "status": "pending|in_progress|completed", "description": "..."}
  ],
  "diff": null                              // 第一次为 null；后续可填差异
}
```

##### 3.3.10 `summary_compaction`

CC compact_boundary + isCompactSummary user 配对，或 Codex compacted RolloutItem。
```jsonc
{
  "kind": "summary_compaction",
  "trigger": "auto|manual",
  "before_tokens": 171705,
  "after_tokens": 8473,
  "summary_text": "<isCompactSummary user 内容 或 replacement_history 总览>",
  "replacement_history_ref": null,         // 如果用 Codex 风格保留新 history，引用一段 moments 子集
  "lossy": true,
  "lossy_reason": "encrypted_content/signature not portable; full pre-compaction history not preserved"
}
```

##### 3.3.11 `error`

```jsonc
{
  "kind": "error",
  "message": "...",
  "subtype": "api_error|tool_error|network|other",
  "retry_info": {"attempts": 3, "max": 10}
}
```

##### 3.3.12 `notification`

后台 task 完成 / hook 触发等。
```jsonc
{
  "kind": "notification",
  "subtype": "task_complete|hook_stop|user_interrupt",
  "content": "...",
  "ref": null
}
```

##### 3.3.13 `metadata`

UI / telemetry only，不影响对话上下文。可选保留。
```jsonc
{
  "kind": "metadata",
  "subtype": "title|turn_duration|stats|...",
  "data": {...}
}
```

### 3.4 Canonical 工具命名与 args schema

每个 canonical 工具有：
- `tool` 名（snake_case，跨 harness 一致）
- input schema（JSON Schema）
- output schema (`tool_result.output_text` 是默认表达，但有些工具有结构化输出)

| Canonical | CC 名 | Codex 名 | Args schema 关键字段 |
|---|---|---|---|
| `shell` | `Bash` | `exec_command` / `shell` / `shell_command` | `command: string`, `workdir?: string`, `timeout_ms?: int`, `run_in_background?: bool`, `escalate?: bool` |
| `read_file` | `Read` | (cat 包裹) | `path: string`, `offset?: int`, `limit?: int`, `format_hint?: "text|image|notebook|pdf"` |
| `write_file` | `Write` | (apply_patch Add/Update File 整替换) | `path: string`, `content: string`, `overwrite: bool` |
| `edit_file` | `Edit` | (apply_patch Update File 单 hunk) | `path: string`, `old: string`, `new: string`, `replace_all?: bool` |
| `multi_edit_file` | `MultiEdit` | (apply_patch Update File 多 hunk) | `path: string`, `edits: [{old, new}]` |
| `delete_file` | (Bash rm) | (apply_patch Delete File) | `path: string` |
| `move_file` | (Bash mv) | (apply_patch Update File + Move to) | `from: string`, `to: string` |
| `find_files` | `Glob` | (rg --files 包裹) | `pattern: string`, `path?: string`, `limit?: int` |
| `search_text` | `Grep` | (rg 包裹) | `pattern: string`, `path?: string`, 全部 rg 参数透传 |
| `web_fetch` | `WebFetch` | (curl 包裹 / web_search open_page) | `url: string`, `prompt?: string` |
| `web_search` | `WebSearch` | `web_search` (Responses builtin) | `query: string`, `allowed_domains?: [string]`, `blocked_domains?: [string]` |
| `notebook_edit` | `NotebookEdit` | (整文件 apply_patch) | `notebook_path: string`, `cell_id?: string`, `new_source: string`, `cell_type?: "code|markdown"`, `edit_mode?: "replace|insert|delete"` |
| `update_plan` | `TaskCreate`/`TaskUpdate` | `update_plan` | `items: [{title, status, description?}]` |
| `worktree_enter` | `EnterWorktree` | (git worktree add) | `name?: string`, `path?: string` |
| `worktree_exit` | `ExitWorktree` | (git worktree remove) | `action: "keep|remove"`, `discard_changes?: bool` |
| `ask_user` | `AskUserQuestion` | `request_user_input` (plan mode) | `questions: [{text, multi_select?, choices?}]` |
| `view_image` | (Read image) | `view_image` | `path: string`, `detail?: "auto|low|high|original"` |
| `subagent_dispatch` | `Agent` | `spawn_agent` v2 | `agent_type: string`, `task: string`, `prompt: string`, `model_hint?: string`, `run_async?: bool` |
| `skill_invoke` | `Skill` | (developer message marker) | `skill_name: string` |
| `mcp_call` | `mcp__<server>__<tool>` | `function_call` with `namespace` | `server: string`, `tool: string`, `args: object` |
| `schedule_task` | `ScheduleWakeup`/`Cron*` | (无；标记 lossy) | `cron?: string`, `prompt: string`, `recurring: bool` |

未来扩展：每加一种 canonical 工具，在此表新增一行；adapter 各自实现 mapping。

### 3.5 `subagent_transcripts`

dict，key 是 `agent_id`，value 是该子 agent 的完整 moments 列表（递归结构）。

```jsonc
{
  "subagent_transcripts": {
    "agent-X": [
      { "kind": "user_text", "text": "<prompt from main>", ... },
      { "kind": "assistant_text", "text": "...", ... },
      { "kind": "tool_call", ... },
      ...
    ],
    "agent-Y": [...]                        // 嵌套子 agent
  }
}
```

每个子 agent 自己也有 `session.id`、`source_session_id`、`cwd` 等——**结构完全相同**，但放在 dict value 而不是顶层。

如果用户选择 fidelity Mode A（faithful），render 时把这些 transcript 单独输出为目标 harness 的副 jsonl 文件；Mode C / B 则可能 inline 或仅保留 result。

### 3.6 `artifacts[]`

大块二进制 / 文本资源（图片、tool_results 外置文件、文件附件等）。

```jsonc
{
  "artifacts": [
    {
      "id": "abc",
      "kind": "image|text_blob|notebook|pdf|other",
      "size_bytes": 1234,
      "mime": "image/png",
      "stored_at": "data/artifacts/abc.png",  // 相对 canonical 文件目录
      "preview": "...",                        // 可选：文本前 2KB 预览
      "source_ref": "<原文件路径>"
    }
  ]
}
```

artifact 落到 canonical 文件**同级目录**的 `artifacts/` 子目录。一份 canonical session 总是对应一对：`<id>.canonical.json` + `<id>/artifacts/`。

### 3.7 `source_metadata`

无法解释但要保留的 harness-specific 字段。例如 CC 的 `caller`、`promptId`、`attribution-snapshot`，Codex 的 `encrypted_content`、`reasoning_effort` 等。

```jsonc
{
  "source_metadata": {
    "claude_code": {
      "version": "2.1.131",
      "userType": "external",
      "entrypoint": "cli",
      "permission_mode_history": [...],
      "preserved_segments": [...]            // 有损降级时把丢的字段塞这里
    },
    "codex": {
      "cli_version": "0.128.0",
      "originator": "codex-tui",
      "personality": "pragmatic",
      "collaboration_mode_settings": {...},
      "raw_session_meta": {...}              // 整个 SessionMeta 反向时回填
    }
  }
}
```

**保留原则**：
- adapter 不识别的字段一律塞进 `source_metadata.<harness>` 下
- render 时如果 target_harness 与 source_harness 同名，把 `source_metadata.<harness>` 反向回填
- 不同 harness 的 `source_metadata` 字段相互不可见

---

## 4. Adapter 接口

每个 harness 提供一对纯函数：

```python
# adapters/<harness>/ingest.py

def ingest(session_path: Path, *, follow_subagents: bool = True,
           fidelity: Literal["A", "B", "C"] = "A") -> CanonicalSession:
    """
    Read a native session (and optional sidecars) into canonical form.
    - session_path 指向原 harness 的 session 文件入口（不是目录）
    - follow_subagents=True 时递归 ingest 所有子 agent transcript
    - fidelity 影响 attachment / metadata 截断策略
    """
    ...

# adapters/<harness>/render.py

def render(session: CanonicalSession, *, target_dir: Path,
           fidelity: Literal["A", "B", "C"] = "A",
           subagent_strategy: Literal["inline", "split", "drop"] = "inline") -> RenderResult:
    """
    Write canonical session into target harness's expected disk layout.
    Returns RenderResult(session_id, primary_path, sidecar_paths).
    """
    ...
```

`CanonicalSession` 是 §3 定义的 dataclass，强校验（pydantic v2 或 dataclasses + jsonschema）。

`RenderResult`：
```python
@dataclass
class RenderResult:
    session_id: str             # 目标 harness 中可被 resume 的 ID
    primary_path: Path          # 主 session 文件
    sidecar_paths: list[Path]   # 子文件（subagent jsonl / tool-results / 等）
    resume_command: str         # 完整 CLI 命令（"codex resume --include-non-interactive <id>" 等）
    warnings: list[str]         # 翻译期 warning
```

### 4.1 Adapter 注册

`adapters/__init__.py`：
```python
ADAPTERS = {
    "claude-code": (claude_code.ingest, claude_code.render),
    "codex":       (codex.ingest, codex.render),
    "hermes":      (hermes.ingest, hermes.render),  # 后期
}
```

### 4.2 Adapter 必须满足的不变量

1. **round-trip metadata 完整**：`ingest(path) → render(target_dir)` 后再 `ingest(target_dir/<file>)`，`source_metadata.<self>` 字段应保持稳定。
2. **moments 顺序保留**：ingest 不重排，render 不打乱时间戳单调性。
3. **call_id 对**：每个 `tool_call` 必须找到对应 `tool_result`（除非异步未完成）；render 时配对不被破坏。
4. **不写出 session 之外**：render 函数严格限制写入路径（用 `Path.is_relative_to(target_dir)` 检查）。

---

## 5. 翻译流水线

### 5.1 单 session 翻译

```python
# src/translator.py
def translate(source_path: Path, source_harness: str,
              target_harness: str, target_dir: Path,
              *, fidelity="C", subagent_strategy="inline") -> RenderResult:
    ingest_fn, _ = ADAPTERS[source_harness]
    _, render_fn = ADAPTERS[target_harness]
    canonical = ingest_fn(source_path, fidelity=fidelity)
    canonical = apply_fidelity_filter(canonical, fidelity)   # §5.3
    return render_fn(canonical, target_dir=target_dir,
                     fidelity=fidelity, subagent_strategy=subagent_strategy)
```

### 5.2 CC 多 jsonl DAG 合并

CC 同 project 目录下可有多文件，且通过 parentUuid 形成 DAG。`claude_code/ingest.py` 必须实现：

```python
def _build_dag(project_dir: Path) -> list[CanonicalSession]:
    """
    Read all *.jsonl in project_dir, build DAG, find leaves,
    return one CanonicalSession per leaf (user picks which to translate).
    """
```

CLI 要支持 `--leaf <uuid>` 让用户选 leaf；默认选最新（按 timestamp）。

### 5.3 Fidelity filter

```python
def apply_fidelity_filter(session: CanonicalSession, mode: str) -> CanonicalSession:
    if mode == "A":
        return session                       # 原样
    elif mode == "B":
        return summarize_to_handoff(session) # 用外部 LLM 做摘要
    elif mode == "C":
        return hybrid_keep_last_n(session, n=10)
```

`summarize_to_handoff` 需要 LLM；翻译器只调一次（顶层 + 每个 subagent transcript 一次）。LLM 可配置（默认 gpt-5-mini 或 haiku-4-5），不强依赖 OpenAI。

### 5.4 Subagent 策略实现

```python
def materialize_subagents(session: CanonicalSession, strategy: str,
                           render_fn) -> tuple[CanonicalSession, list[Path]]:
    if strategy == "inline":
        return _inline_subagents(session), []
    elif strategy == "split":
        sidecars = []
        for agent_id, transcript in session["subagent_transcripts"].items():
            sub_session = build_sub_session(transcript, parent=session)
            res = render_fn(sub_session, target_dir=...)
            sidecars.append(res.primary_path)
        return session_without_transcripts(session), sidecars
    elif strategy == "drop":
        return drop_subagents_keep_results(session), []
```

---

## 6. 项目布局

```
agent-bridge/
├── README.md
├── AGENTS.md
├── pyproject.toml                      # uv / poetry / pip 都行；建议 uv
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md                 ← 本文件
│   ├── claude-code-harness.md
│   ├── codex-harness.md
│   └── tool-mapping.md
├── specs/
│   ├── 001-translator-mvp.md
│   ├── 002-bidirectional.md            # 后续
│   └── 003-hermes-adapter.md           # 后续
├── src/
│   ├── agent_bridge/                   # 包名（pip install -e . 可用）
│   │   ├── __init__.py
│   │   ├── canonical/                  # canonical IR 类型与校验
│   │   │   ├── __init__.py
│   │   │   ├── schema.py               # pydantic models
│   │   │   ├── tool_names.py           # canonical tool 注册表
│   │   │   └── validate.py             # 不变量 check
│   │   ├── adapters/
│   │   │   ├── __init__.py             # ADAPTERS 注册表
│   │   │   ├── claude_code/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── ingest.py
│   │   │   │   ├── render.py
│   │   │   │   ├── tool_map.py         # CC 工具名 ↔ canonical
│   │   │   │   ├── attachments.py      # CC attachment 处理
│   │   │   │   ├── dag.py              # 多文件 DAG 重建
│   │   │   │   ├── encoded_path.py     # cwd → encoded dir 算法
│   │   │   │   └── subagent.py         # subagent 文件读写
│   │   │   └── codex/
│   │   │       ├── __init__.py
│   │   │       ├── ingest.py
│   │   │       ├── render.py
│   │   │       ├── tool_map.py
│   │   │       ├── apply_patch.py      # patch 解析与生成（核心 unhappy path）
│   │   │       ├── rollout_item.py     # RolloutItem 类型
│   │   │       └── path.py             # Codex sessions/YYYY/MM/DD/ 路径生成
│   │   ├── translator.py               # 主流程
│   │   ├── fidelity.py                 # mode B/C 实现
│   │   ├── llm.py                      # 可选 LLM 摘要客户端（mode B/C 用）
│   │   └── cli.py                      # argparse / typer
│   └── tests/
│       ├── fixtures/                   # 测试数据
│       │   ├── cc-simple.jsonl
│       │   ├── cc-with-bash.jsonl
│       │   ├── cc-with-subagent/
│       │   ├── codex-simple.jsonl
│       │   └── codex-with-patch.jsonl
│       ├── test_canonical.py
│       ├── test_cc_ingest.py
│       ├── test_cc_render.py
│       ├── test_codex_ingest.py
│       ├── test_codex_render.py
│       ├── test_translator_e2e.py
│       └── test_round_trip.py
├── data/                                # gitignore 之外的临时数据
│   ├── fixture/                         # PoC 期 fixture 与报告
│   └── artifacts/                       # 翻译 artifact 输出
└── scripts/
    ├── poc_cc_to_codex.py               # 一次性脚本，§7 PoC 落地
    └── inspect_session.py                # debug 工具
```

---

## 7. CLI 设计

### 7.1 命令一览

```
agent-resume export <SOURCE>
  --from claude-code|codex
  --leaf <uuid>                     # CC DAG 多 leaf 时选
  -o <out.canonical.json>
  --include-subagents
  --fidelity A|B|C

agent-resume import <CANONICAL>
  --to claude-code|codex
  --target-dir <path>               # 默认 ~/.claude/projects/.../  或 ~/.codex/sessions/...
  --subagent-strategy inline|split|drop
  --fidelity A|B|C
  --print-resume-command            # 输出可粘贴的 resume 命令

agent-resume translate <SOURCE>
  --from claude-code --to codex     # 直跳，等价 export | import
  --leaf ...
  --fidelity ...
  --subagent-strategy ...

agent-resume inspect <SESSION>
  --format pretty|json|tree
  # 不论 source 还是 canonical，统一 dump 摘要
```

### 7.2 默认行为

- 输出始终落到 target harness 期望的目录（CC `~/.claude/projects/<encoded>/`、Codex `~/.codex/sessions/YYYY/MM/DD/`）
- 默认 `--fidelity C`、`--subagent-strategy inline`
- 默认输出 resume 命令到 stdout 最后一行
- `--dry-run` 模式只报告会写哪些文件，不实际写

### 7.3 错误处理

- 翻译失败的部分 moment 用 `kind: "error"` 占位，不让整个 session 翻译失败
- 失败统计在 RenderResult.warnings 列出
- 致命错误（如目标目录不存在权限）抛 `BridgeFatalError`

---

## 8. 测试策略

### 8.1 单元测试（必须）

每个 adapter 的 ingest / render 都要有：
- 空 session（仅 metadata）
- 单 user/assistant turn
- 含 1 个 shell 调用
- 含 1 个 file edit 调用
- 含 1 个 subagent 调用
- 含 compact_boundary
- 含 1 张图片附件

### 8.2 Round-trip 测试（必须）

```python
def test_round_trip_cc():
    canonical1 = cc.ingest(fixture("cc-simple.jsonl"))
    cc.render(canonical1, target_dir=tmpdir)
    canonical2 = cc.ingest(tmpdir / "<encoded>" / "<id>.jsonl")
    assert moments_equiv(canonical1, canonical2)   # 忽略 ts 微秒差
```

`moments_equiv`：忽略 ts 精度，比 kind/text/tool args/call_id 等关键字段。

### 8.3 跨 harness E2E

```python
def test_cc_to_codex_to_cc():
    cc1 = cc.ingest(fixture("cc-simple.jsonl"))
    codex.render(cc1, target_dir=tmpdir/"codex")
    codex_session_path = ... # 找出 render 写出的文件
    canonical2 = codex.ingest(codex_session_path)
    cc.render(canonical2, target_dir=tmpdir/"cc-back")
    cc_back_path = ... # find written
    canonical3 = cc.ingest(cc_back_path)
    assert moments_equiv(cc1, canonical3, mode="lossy_ok")  # 接受已知有损
```

### 8.4 Live resume 测试（可选，需要二进制）

CI 跳过；本地手动跑：
```python
def test_live_codex_resume(monkeypatch):
    if not shutil.which("codex"): pytest.skip()
    res = translate(fixture("cc-simple.jsonl"), source="claude-code",
                    target="codex", target_dir=Path.home()/".codex"/"sessions"/...)
    out = subprocess.run(["codex", "exec", "resume", "--include-non-interactive",
                          res.session_id, "Reply with 'OK'"], capture_output=True)
    assert "OK" in out.stdout.decode()
```

---

## 9. 与「像素级蒸馏」的整合（长期方向）

**MVP 不做**。但架构必须为此预留接口。

### 9.1 像素级蒸馏的现有结构（来自 `/Users/va7/Desktop/0423 像素级蒸馏/`）

```
src/
├── ingest/
│   ├── agents/        # 已扫 ~/.claude/projects/*.jsonl
│   ├── feishu/
│   ├── browser/
│   └── ...
├── normalize/         # 各源 → 统一 Event schema
├── timeline/          # events.sqlite
├── thread/            # 跨天串联
├── cluster/           # 工作类型层级
└── kg/                # 实体关系图
```

### 9.2 整合点

**短期（MVP+1）**：在像素级蒸馏新增一个 ingest 子源 `src/ingest/agents/canonical/`，直接读 agent-bridge 输出的 `*.canonical.json`（而不是 CC/Codex jsonl 直接），把 moment 序列归一化为 Event。

**中期**：把 canonical IR 直接沉淀到像素级蒸馏的 `events.sqlite` + 一张新表 `tool_calls`（带 args/result/parent_call_id）：
```sql
CREATE TABLE tool_calls (
  call_id TEXT PRIMARY KEY,
  event_id INTEGER REFERENCES events(event_id),
  canonical_tool_name TEXT,
  args_json TEXT,
  result_json TEXT,
  parent_call_id TEXT
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  source_harness TEXT,
  cwd TEXT,
  started_at TEXT,
  ended_at TEXT,
  episode_id INTEGER REFERENCES episodes(episode_id)
);
```

**长期**：MCP server (`src/mcp/server.py`) 暴露：
- `recall(query, time_range)`：跨 source 检索
- `current_context(project)`：当前 episode + thread 状态
- `who_is(name)`、`what_is(project)`：KG 查询
- `resume_for(session_id, target_agent)`：用 canonical IR 反向 render 出目标 agent session

任何接 MCP 的 agent 都能 query 这一份单一事实源。

### 9.3 字段对齐

agent-bridge 的 canonical `moment.kind` 对应像素级蒸馏的 Event：
- `user_text` / `assistant_text` / `thinking` → 一种 `Event(source="agent", kind="msg")`
- `tool_call` / `tool_result` → `Event(source="agent", kind="tool")` + `tool_calls` 表
- `attachment` → `Event(source="agent", kind="context_attach")`

像素级蒸馏的 `WorkEpisode` 概念跨源串联：当 agent 干的事（CC/Codex/Hermes session）和人在飞书/浏览器里干的事属于同一个项目时，`thread` 和 `episode` 把它们绑到一起。

---

## 10. 不在 MVP 范围内（明确推迟）

- ❌ 反向 Codex → CC 翻译（先完成 CC → Codex 再做反向）
- ❌ Hermes adapter
- ❌ Mode B（外部 LLM 摘要）的实现，先只提供 Mode A 与 Mode C 的占位
- ❌ Live resume 测试集成进 CI
- ❌ MCP server
- ❌ 与像素级蒸馏的真实集成
- ❌ Web UI / TUI 浏览器
- ❌ 加密 / 隐私脱敏

MVP 的窄定义见 `specs/001-translator-mvp.md`。

---

## 11. 不变量速查（实现时反复对照）

1. canonical IR `moments[]` 严格按 ts 单调递增
2. 每个 `tool_call` 对应一个 `tool_result`（除非显式 async 未完成）
3. `call_id` 跨 harness 沿用，不重铸
4. session.id 重铸（adapter 各自生成原 harness 期望的 UUID 风格）
5. `source_metadata.<harness>` 在自家 round-trip 时必须保持
6. `lossy=true` 的 moment 必须填 `lossy_reason`
7. adapter 写盘只能写到 `target_dir` 之内
8. ingest 是只读，永不修改源文件
9. canonical JSON 文件 + artifacts/ 目录是配对的，不可分离
10. CLI `--dry-run` 模式不能产生任何文件副作用

---

> 落实路径：当前 PoC（`scripts/poc_cc_to_codex.py`）出验证结论后，按 `specs/001-translator-mvp.md` 启动 src/ 实现，第一个 milestone 是 §8.1 单元测试通过 + §8.2 round-trip 通过。
