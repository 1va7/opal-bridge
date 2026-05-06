# Spec 002 — Completion of CC → Codex translator

> 范围：把 MVP（spec 001）里推迟的 CC → Codex 项目补齐。**不包含反向 Codex → CC**——那归 spec 003。
>
> 状态：已落地（commits 77f93c2、36547cd、c5bbdf2）。

## 1. 已完成

### 002a — `compact_boundary` 优雅处理

- 新增 canonical IR moment kind `SummaryCompaction`（带 trigger / before_tokens / after_tokens / lossy=True）
- CC ingest 不再对 `system / subtype=compact_boundary` 抛 `NotImplementedError`，而是产出一条 `SummaryCompaction` moment
- 紧跟的 `isCompactSummary:true` user 行变成普通 `UserText`（lossy 标记），保留摘要文字
- Codex render 把 SummaryCompaction 渲染成 developer message（注明 trigger / token 数）

### 002b — Edit/Write/MultiEdit 真翻译为 apply_patch

新增模块 `adapters/codex/apply_patch.py`：

- `FileStateCache` + `build_file_state(moments)` 走一遍 moments，从 `read_file` ToolResult 提取文件内容（剥掉 `cat -n` 行号），并模拟 `write_file`/`edit_file`/`multi_edit_file`/`delete_file`/`move_file` 来保持缓存与真实文件状态一致
- `patch_for_write/edit/multi_edit/delete/move` 根据 Lark grammar 拼出 apply_patch 字符串
- `to_relative(abs_path, cwd)` 处理绝对路径 → 相对（apply_patch 强制相对）
- Update File hunk 含 ≤3 行真实 context（来自 file_state 快照）；当 file_content 缺失时退化为无 context（lenient mode 仍可通过）
- write_file 覆盖现有文件用 `Delete File` + `Add File` envelope（一次原子）
- replace_all=true 多 occurrence 拆成多 hunk Update File

Render 在写 jsonl 前一次性算出 `dict[call_id, FileStateCache]`，传给 `render_tool_call`，避免每个工具自己重新扫一遍。

### 002c — Subagent inline strategy

CC ingest 自动扫 `<session-id>/subagents/agent-*.jsonl` 兄弟目录：

- 每个 `agent-<id>.jsonl` 翻译为独立 moment 列表，全部打上 `agent_scope = "subagent:<id>"`
- 关联的 `.meta.json`（`{agentType, description}`）保存到 `session.source_metadata.claude_code.subagent_meta`
- 入 `session.subagent_transcripts` dict（key=agent_id）

Codex render 加 `--subagent-strategy=inline`：

- 主 session 中的 `subagent_dispatch` ToolCall 与 sub transcript 通过 description 字符串匹配（main `tool_use.input.description` ↔ meta `description`），不匹配按时间顺序兜底
- 每个 sub transcript 在主 ToolResult 之后用 `--- begin/end subagent transcript (<id>) ---` developer message 包裹后 inline
- 未匹配的 sub transcript 在主 session 末尾追加 + warning

实测：本机 1.8MB 真实 session（4 个子 agent）成功 inline，输出 1117 行。

### 002d — 易用 CLI 子命令

`agent-resume list`：扫 `~/.claude/projects/`，按 mtime 倒序输出最近 N 条 session：mtime / 大小 / 行数 / 用过的工具列表 / 第一条 prompt / cwd。`--project <substr>` 可按 cwd 过滤。

`agent-resume smoke <CC-jsonl>`：translate + 自动 `codex exec resume <UUID> "<prompt>"`，然后报告 stdout 末尾，默认清理生成的 jsonl（`--keep` 保留）。

## 2. 测试覆盖

- 12 个 apply_patch 单元测试（patch grammar、context 抽取、file state walk）
- 3 个 E2E 测试（PoC fixture 翻译结构匹配）
- Live `codex exec resume` 验证：smoke 命令一次跑通，模型回 "WORKS"

## 3. 仍未做（推到 003）

- **反向 Codex → CC**（这是大头）
  - Codex `apply_patch` 解析回 CC Edit/Write/MultiEdit
  - Codex `compacted` → CC `compact_boundary` + isCompactSummary user
  - Codex `function_call name=update_plan` → CC TaskCreate/Update 流转
  - Codex sub-agent jsonl → CC subagents/ 副文件
- **Plan mode**：CC EnterPlanMode/ExitPlanMode ↔ Codex collaboration_mode 切换
- **TaskCreate stateful diff**（CC 多次 Task 调用 → Codex 单 update_plan 重发）
- **AskUserQuestion** 完整翻译（目前降级为 assistant text）
- **Hooks 与 ScheduleWakeup/Cron**（保持丢弃）
- **DAG 多 leaf**（MVP 选最近 leaf，多 leaf 选择交互）
- **Mode B/C fidelity**（外部 LLM 摘要）
- **MCP server**（暴露 resume RPC 给任何 MCP-aware agent）

## 4. 实证验证清单（已通过）

- [x] PoC fixture 翻译输出结构与手翻一致（3 测试）
- [x] phase 推断在多 CC line 共享 msg_id 时也能分辨 commentary / final_answer
- [x] tool_use ↔ tool_result 配对的 call_id 完整保留
- [x] thinking blocks 全删，signature 不进 jsonl
- [x] session_meta UUIDv7 通过 Codex resume 校验
- [x] compact_boundary 不再抛异常（实测含 compact 的 session 可翻译）
- [x] apply_patch grammar 在 lenient mode 下 Codex 能解析（unit 测试覆盖）
- [x] 1.8MB 真实 CC session（4 子 agent + ~580 行）翻译耗时 < 1s

## 5. 下一步衔接

`specs/003-bidirectional.md`（草稿待写）应优先：

1. Codex `ingest()` 函数：rollout jsonl → canonical Session
2. Claude Code `render()` 函数：canonical → CC jsonl 文件，落到 `~/.claude/projects/<encoded>/<UUIDv4>.jsonl`
3. apply_patch parser（Lark grammar 反向）：把 patch 字符串拆成 list[FileOp]
4. `agent-resume translate --from codex --to claude-code <UUID>` CLI 路径
5. Round-trip 测试：CC → Codex → CC 后 moments 等价（容忍已知 lossy 项）
