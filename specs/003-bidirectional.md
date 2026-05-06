# Spec 003 — Bidirectional translation (CC ↔ Codex)

> 范围：实现 Codex → CC 反向翻译；apply_patch 反向解析；CC render；
> CLI 双向 wired；round-trip 测试。
>
> 状态：已落地（commit `da45c79` 后续）。

## 1. 已完成

### 003a — CC render PoC

手工构造 5 moments 的 canonical Session → `claude_code/render.py` → 落到 `~/.claude/projects/-private-tmp-poc-resume-test/<UUIDv4>.jsonl` → `cd /tmp/poc-resume-test && claude --resume <UUID> -p "..."` → 模型回复成功。

关键发现：CC 编码 cwd 时**先做 realpath**——`/tmp` ≡ `/private/tmp` 在 macOS 下，编码必须基于 realpath 结果，否则 `claude --resume` 在原 cwd 下找不到。`encode_cwd` 现 `os.path.realpath` + `unicodedata.normalize("NFC")`。

### 003b — Codex ingest

`adapters/codex/ingest.py`：

- 顶层 RolloutItem 5 种全部识别：`session_meta` / `turn_context` / `response_item` / `event_msg` / `compacted`
- response_item 子类型：`message` / `reasoning` / `function_call` / `function_call_output` / `custom_tool_call` / `custom_tool_call_output` / `web_search_call` / `image_generation_call` / `compaction` / `context_compaction`
- event_msg 去重表：`agent_message`/`user_message`/`exec_command_end`/`patch_apply_end`/`web_search_end`/`token_count`/`task_*`/`turn_*`/`thread_*` 等都跳过（已被 response_item 表达）
- `_classify_shell_cmd`：识别 `cat -n` / `head -n` / `tail -n +N` / `sed -n 'M,Np'` / `rg --files -g <p>` 反向回 canonical `read_file`/`find_files`，保护 round-trip
- 未知 namespace 的 function_call → `mcp_call`（保留 server/tool/args）

### 003c — apply_patch 反向解析

`adapters/codex/apply_patch_parser.py`：

- 手写 lenient parser（不引入 lark 依赖）
- 解析 `*** Begin Patch ... *** End Patch` 信封
- 三种 op：`Add File` / `Update File`（含 `Move to`、多个 `@@` hunk）/ `Delete File`
- 每个 hunk 拆出 context_before / removed / added 三组行
- ingest 把单个 `custom_tool_call apply_patch` 拆成多个 canonical ToolCall（call_id 后缀 `__0` `__1` ...），canonical 工具分别是 `write_file` / `edit_file` / `multi_edit_file` / `delete_file` / `move_file`

### 003d — CC render

`adapters/claude_code/render.py`：

- canonical Session → 单个 `<UUIDv4>.jsonl` 落到 `~/.claude/projects/<encoded-cwd>/`
- 严格 linear `parentUuid` 链（首行 null，后续指前行 uuid）
- moment 派发：UserText/AssistantText/ToolCall/ToolResult/Thinking(drop)/Attachment/SummaryCompaction/ModeChange/PlanUpdate/Notification(drop)
- canonical 工具名反向回 CC 的 `Bash` / `Read` / `Edit` / `Write` / ...；优先用 `wire_native` 字段做高保真还原
- SummaryCompaction → 两行：`system / subtype=compact_boundary` + 紧跟 `user / isCompactSummary=true`，匹配 CC 期望的对子结构

### 003e — CLI 双向 wired

- `translate --from codex --to claude-code <jsonl>` 一条命令搞定
- `smoke --from codex <jsonl>` 自动选择 `claude --resume` 而非 `codex resume`
- smoke 在 `claude --resume` 时 `cd` 到 session.cwd 让 CC 找到 encoded-cwd 目录
- `translator.translate()` 注册了两个方向；hermes 等仍抛 NotImplementedError

### 003f — Round-trip 测试

`src/tests/test_round_trip.py`：

1. **CC → Codex → CC**：fixture 进 → 翻译两次 → 摸用户/助手/工具调用计数，断言无丢失
2. **Codex ingest 不崩**：合成 6 行 Codex jsonl，断言 moment 计数正确
3. **apply_patch 多 op 拆分**：3 op envelope（Add + Update + Delete）→ 3 个 canonical ToolCall

加上原 e2e 与 apply_patch 单元测试，目前 18 测试全过。

## 2. 端到端验证（人工跑）

| 方向 | 命令 | 结果 |
|---|---|---|
| CC → Codex | `agent-resume smoke <CC>.jsonl` | 模型回 `CC2CDX-OK` ✓ |
| Codex → CC | `agent-resume smoke --from codex <Codex>.jsonl` | 模型回 `CDX2CC-OK` ✓ |
| 真实 ~563KB Codex session → CC | `claude --resume <UUID> -p "..."` | 模型回 `REVERSE-WORKS` ✓ |

## 3. 已知局限（在 README/Quick start 也写了一份）

- DAG 多 leaf 不选（默认时间排序最新）
- TaskCreate / TaskUpdate 流转还是 1:1 映射，不是 stateful diff
- Plan mode 切换在 canonical 是 ModeChange，但 render 端只发 `permission-mode` 行，未与 CC 实际的 `EnterPlanMode/ExitPlanMode` 工具对子建立精确语义
- AskUserQuestion 在 reverse 时降级为 shell echo
- CC `thinking` 与 Codex `reasoning.encrypted_content` 都不可跨 harness 携带，全部 drop
- Multi-op apply_patch 拆出的 sub-call_ids 形如 `<orig>__<idx>`，原 Codex `function_call_output` 只有一个 → 在 round-trip 中只有第一个 sub-call 拿到 output，其余 sub-call 没 ToolResult 配对（不影响 model resume，但 CC 严格场景可能露馅）

## 4. 接下来

`specs/004-mcp-server.md`（草稿待写）方向：

- 暴露 `agent-bridge mcp serve` 子命令，stdio MCP server
- Tools: `list_sessions(harness?)` / `translate(source, from, to)` / `resume_in(target_harness, session_id, prompt?)` 
- 任何 MCP-aware host（Claude Desktop / Cline / Roo Code / Codex / Cursor）都能查询到自己/对方的 session 历史并要求翻译

`specs/005-pixel-distill-integration.md` 方向：在像素级蒸馏的 `src/ingest/agents/` 加 canonical 输入源，让 CC/Codex/Hermes 全部走同一个 events.sqlite 时间线。
