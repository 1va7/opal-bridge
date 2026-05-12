# Tool Mapping — Claude Code ↔ Codex CLI

> 本文是 **agent-bridge 翻译器**的实现规范。每一条 mapping 都附条件、降级策略、证据来源。
>
> 上游证据：
> - `claude-code-harness.md`（CC 实证调研，1637 行，证据来自 v2.1.131 二进制 strings + 55 份 jsonl + sdk-tools.d.ts + 多篇逆向博客）
> - `codex-harness.md`（Codex 实证调研，1512 行，证据来自 `openai/codex` `06e5dfa` commit 源码 + 本机 0.128.0 9 份 jsonl）
>
> 引用约定：`(cc §3.4)` = claude-code-harness.md 第 3.4 节。`(codex §3.6)` 同理。
>
> 写法约定：能往 wire 上落的字段写英文 + JSON；解释、原理、决策走中文。

---

## 0. TL;DR — 必须先记住的七条铁律

1. **不通过 canonical**：本文档定义 CC↔Codex **直接** mapping；翻译器内部仍走 IR，但每条 IR 字段都直接对应到这张表的某一行。
2. **session 不是数组而是图（CC 端）/ 流（Codex 端）**：CC 是 DAG（`parentUuid`），同一目录可有多文件；Codex 是单文件 append-only 的 JSONL。翻译时 CC→Codex 要拍平到主链；Codex→CC 要给每行铸造 `parentUuid` 形成线性 DAG。
3. **resume 不接文件路径**：CC `claude --resume <UUID>`，Codex `codex resume <UUID>` 或 `codex resume <thread_name>`。两边都要求文件落在它们规定的目录下，由它们自己 discover。**翻译器只负责落对路径**。
4. **"思考"是单向有损的双方都死路**：CC 的 `thinking` 有 Anthropic signature，伪造的 signature 在下一次 API 调用会被拒；Codex 的 `reasoning.encrypted_content` 是 OpenAI 不透明 token，外人无法生成。结论：**两边的思考链都丢，只保留摘要文字**。
5. **system prompt 不可硬抄**：CC 的 system prompt 不写入 jsonl，每 turn 由 harness 实时拼接（CLAUDE.md / skills / tools / env）。Codex 的 `SessionMeta.base_instructions.text` 写入但巨大（~21KB）。**翻译时不要复制 system，让目标 harness 自己生成**，只迁移那些影响行为的可移植部分（CLAUDE.md/AGENTS.md，skill 列表）。
6. **Edit/Write 必须退化为 apply_patch**：Codex 端的唯一编辑器是 `apply_patch`（Lark grammar）。CC 的 Edit/Write/MultiEdit/NotebookEdit 全部走它。反向只能 best-effort：apply_patch 的 hunk 拆成 CC 多个 Edit 调用。
7. **结构化 todo / subagent / skill / plan 都半残**：这四件是 CC 比 Codex 浓的特性，反方向相对干净。Codex 的 `update_plan` 是 CC `TaskCreate` 的子集；`spawn_agent` 与 CC `Agent` 概念可对应但 wire 完全不同；`skills` 两边都有但注入路径不同。**默认翻译策略：保留 metadata，工具调用降级为 developer message**。

---

## 1. 顶层格式映射

### 1.1 文件级

| 维度 | Claude Code | Codex CLI | 说明 |
|---|---|---|---|
| 单文件容器 | `<UUIDv4>.jsonl` | `rollout-<ts>-<UUIDv7>.jsonl` | 都是 JSONL；CC 文件名只有 UUID，Codex 含时间戳 |
| 文件位置 | `~/.claude/projects/<encoded-cwd>/` | `~/.codex/sessions/YYYY/MM/DD/` | CC 按 cwd 分目录；Codex 按日期分目录 |
| 项目识别 | 从目录名反算（不可逆，必须读 jsonl 内 `cwd` 字段） | 从每行 jsonl 的 `cwd` 字段过滤 | Codex 显式存 cwd，CC 隐式 |
| Append-only | ✓ 但同 project 可有多个 jsonl | ✓ 每个 session 一个文件 | CC 的 DAG 让多文件可拼成一个对话 |
| 副文件 | `<sess>/subagents/*.jsonl`、`<sess>/tool-results/*.txt`、`tasks/<sess>/*.json`、`file-history/<sess>/` | `session_index.jsonl`（命名索引）、`logs_2.sqlite`（缓存） | CC 副文件多得多；Codex 几乎全在主文件 |
| 索引必要性 | `sessions-index.json` **不可信**，必须扫目录 (cc §1.5) | `session_index.jsonl` 可读但**不要写**（Codex 自己会回填 SQLite） | 翻译器**永远扫目录**，**永远不写索引** |

### 1.2 顶层 record type 映射

CC 11 顶层 type + 6 system subtype 映射到 Codex 5 RolloutItem variant：

| CC type / subtype | Codex `type` | 方向 | 转换规则 | 损耗 |
|---|---|---|---|---|
| `user`（普通文本） | `response_item` (`payload.type=message role=user`) | 双向 | content 字符串 → `[{type:input_text, text}]` | 无 |
| `user`（含 tool_result） | `response_item` (`payload.type=function_call_output` 或 `custom_tool_call_output`) | 双向 | 拆 content 数组每个 `tool_result` block 为一个独立 RolloutItem | 无 |
| `user`（含 text content block） | `response_item.message role=user` | 双向 | content 数组的 `{type:text}` 项合并 | 无 |
| `user`（`isCompactSummary:true`） | `compacted` (CC→Codex) / `response_item.message role=developer`（Codex→CC 反向退化） | CC→Codex 优先 | 见 §4.5 | 中 |
| `assistant`（thinking） | `response_item.reasoning` | 双向 | thinking text → `summary[].text`；signature/encrypted_content 都丢 | **大** |
| `assistant`（text） | `response_item.message role=assistant content=[{type:output_text}]` | 双向 | phase 默认 `final_answer` | 无 |
| `assistant`（tool_use） | `response_item.function_call` 或 `custom_tool_call`（按工具决定，§3） | 双向 | 同 turn 的多个 tool_use 拆为多个 RolloutItem | 无 |
| `system, subtype=compact_boundary` | `compacted` 顶层 | CC→Codex | `replacement_history` 用 boundary 之后的 isCompactSummary user 文字 | 中 |
| `system, subtype=turn_duration` | — | 丢弃 | 仅 telemetry | 无 |
| `system, subtype=api_error` | `event_msg.error` | 双向 | message → `error.message`；retry 信息丢失 | 小 |
| `system, subtype=away_summary` | — | 丢弃 | UI-only | 无 |
| `system, subtype=local_command` | `response_item.message role=developer`（包 `<local-command-stdout>` 标签） | CC→Codex | 内容作为 developer 注入 | 小 |
| `system, subtype=stop_hook_summary` | — | 丢弃 | hook 由用户在新 harness 重配 | 无 |
| `attachment` | `response_item.message role=developer` 或 `role=user` | CC→Codex | 见 §4.6 | 小-中（取决于子类型） |
| `file-history-snapshot` | — | 丢弃 | Codex 无 rewind | 无（不影响对话） |
| `permission-mode` | `turn_context.approval_policy` + `sandbox_policy` | 双向 | 见 §6.2 | 小 |
| `queue-operation` | `response_item.message role=user`（注入 `<task-notification>` block） | CC→Codex | enqueue 内容塞下一 turn 前 | 无 |
| `last-prompt` / `ai-title` / `custom-title` / `agent-name` | `event_msg.thread_name_updated`（仅 title） | CC→Codex（仅 title） | 其余丢 | 无 |
| `summary`（v2.0 旧式） | `compacted`（带 message 字段） | CC→Codex | leafUuid 信息丢失 | 小 |

反方向 Codex → CC 的映射：

| Codex `type` | CC type | 方向 | 转换规则 | 损耗 |
|---|---|---|---|---|
| `session_meta` | （写入 jsonl 第一行的 metadata，但 CC 没有等价单独行） | Codex→CC | metadata 写到首行 user message 的字段（cwd/sessionId/version/gitBranch）；不单独成行 | 无 |
| `turn_context` | （同样无单独行）+ `permission-mode` 行（如 policy 变化） | Codex→CC | policy/cwd 信息塞到下一条 user/assistant 行字段；permissionMode 变化时写 `type:permission-mode` | 小 |
| `response_item.message` (role=user) | `type:user` 行 | 双向 | content `input_text` → 字符串或 `[{type:text}]` | 无 |
| `response_item.message` (role=assistant) | `type:assistant` 行 | 双向 | output_text → `[{type:text}]`；phase 信息丢失 | 小 |
| `response_item.message` (role=developer) | `type:user` 行 + 包 `<system-reminder>` 标签 或 `attachment` 行 | Codex→CC | 见 §4.7 | 小 |
| `response_item.reasoning` | `assistant.message.content[].type=thinking`（**signature 必须删**） | 双向 | summary[].text → thinking 文本；encrypted_content 丢 | **大** |
| `response_item.function_call` | `assistant.message.content[].type=tool_use` | 双向 | 工具名翻译见 §3；arguments 字符串解析为 input | 工具相关 |
| `response_item.function_call_output` | `user.message.content[].type=tool_result` | 双向 | output 字符串包进 tool_result.content | 无 |
| `response_item.custom_tool_call` (apply_patch) | `assistant.message.content[].type=tool_use` (Edit/Write 系列) | 双向 | 见 §3.1 | 中 |
| `response_item.custom_tool_call_output` | `user.message.content[].type=tool_result` | 双向 | metadata.exit_code/duration_seconds 丢失 | 小 |
| `response_item.web_search_call` | `assistant.tool_use name=WebSearch` | 双向 | action.query → input.query；查询动作 `open_page`/`find_in_page` 在 CC 没等价 | 中 |
| `response_item.image_generation_call` | `assistant.message.content[].type=text`（降级） | Codex→CC | CC 无 builtin image_generation | 大 |
| `response_item.compaction` (id=`compaction`/`compaction_summary`) | `system, subtype=compact_boundary` + 紧跟 `isCompactSummary` user | Codex→CC | encrypted_content 丢 | 大 |
| `compacted` (顶层) | `system, subtype=compact_boundary` + 紧跟一行带 `replacement_history` 文字摘要的 isCompactSummary user | Codex→CC | replacement_history 序列回译为对应 CC 行（在 boundary 之后） | 中 |
| `event_msg.user_message` | （主 user 行已有，丢 UI 镜像） | 丢弃 | duplicate | 无 |
| `event_msg.agent_message` | （主 assistant 行已有，丢 UI 镜像） | 丢弃 | duplicate | 无 |
| `event_msg.task_started` / `task_complete` | （由 message 序列隐含；CC 用 `stop_reason` 标记） | 丢弃 | turn 边界由 user 行重启隐含 | 小 |
| `event_msg.token_count` | `assistant.message.usage` | Codex→CC | 字段映射见 §6.4 | 小（reasoning_output_tokens 无对应） |
| `event_msg.exec_command_end` | （tool_result 已有；丢） | 丢弃 | duplicate of function_call_output | 无 |
| `event_msg.patch_apply_end` | （tool_result 已有；丢） | 丢弃 | duplicate of custom_tool_call_output | 无 |
| `event_msg.web_search_end` | （tool_result 已有；丢） | 丢弃 | duplicate | 无 |
| `event_msg.error` | `system, subtype=api_error` | Codex→CC | `message` → `error.error.message` | 小 |
| `event_msg.turn_aborted` | （CC 无对应；丢，可在 user 上标 metadata） | 丢弃 | — | 小 |
| `event_msg.thread_rolled_back` | （CC 无对应；丢） | 丢弃 | — | 小 |
| `event_msg.thread_name_updated` | `type:ai-title` 或 `custom-title` | 双向 | thread_name → aiTitle/customTitle | 无 |
| `event_msg.view_image_tool_call` | `assistant.tool_use name=Read` (path=image) | Codex→CC | 注入 read 调用 | 小 |
| `event_msg.context_compacted` | （已由 `compacted` 顶层覆盖；丢） | 丢弃 | duplicate | 无 |

### 1.3 路径与 ID 算法

#### CC 项目目录编码（`cc §1.2`）

```python
def cc_encode_cwd(cwd: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
    if len(s) <= 200:
        return s
    return s[:200] + '-' + format(stable_hash32(cwd), 'x')  # base36 in CC, hex 也可，但 CC 用 base36
```

**警告**：把 `小红书图文裂变` 之类非 ASCII 全塌成 `-`，目录冲突在 ≤200 字符时不加 hash。**翻译器写入 CC jsonl 时一定要先 `cc_encode_cwd(cwd)`** 算出目录名。

#### Codex 路径

```python
def codex_path(home: str, ts: datetime, uuid: str) -> str:
    yyyy = ts.strftime("%Y"); mm = ts.strftime("%m"); dd = ts.strftime("%d")
    fname = f"rollout-{ts.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid}.jsonl"
    return f"{home}/sessions/{yyyy}/{mm}/{dd}/{fname}"
```

UUID 用 v7（`uuid7()`）。Codex 0.128 比较宽松能接 v4，但 SQLite 索引可能拒绝 → **统一发 v7**。

#### Session ID 选择

| 场景 | UUID 风格 | 决策 |
|---|---|---|
| Codex → CC | UUIDv4 | CC 强制 v4 校验。从 Codex UUIDv7 重铸 v4，原 v7 写入 CC user 行的 metadata 字段（自定义 `_origin_uuid`）保留可逆性 |
| CC → Codex | UUIDv7 | 重铸 v7。原 v4 sessionId 写入 SessionMeta 的可选 metadata（自定义字段 Codex 会忽略但保留） |
| 翻译跨多文件 CC project（DAG） | 选用户钦定的 leaf UUID 作为 sessionId | DAG 要先合并到主链，主链最末一行的 uuid 作为锚点 |

#### Tool call ID 沿用

CC 的 tool call ID 在不同版本观察到两种前缀：
- `toolu_<22>`（v2.1.131 + 多数版本）
- `tooluse_<22>`（v2.1.107 实测，PoC 时遇到）

Codex 是 `call_<24>`。所有形式都是不透明字符串，长度无强校验，前缀不影响 Codex resume（PoC 验证）。**直接复用，不重铸**。这是配对 tool_use ↔ tool_result 的唯一可靠 key。

---

## 2. 内容块映射 (content blocks)

CC 的 `message.content` 是数组，元素类型 `type`：`text` / `tool_use` / `tool_result` / `thinking` / `redacted_thinking` / `image`。

Codex 的内容根据 `response_item.payload.type` 直接分流，没有"块数组"层级——一个 ResponseItem 是一个原子。这是两边模型的根本差异。

### 2.1 同 turn 内多块 → 多个 Codex item

CC 一行 assistant 可以有：

```json
{"message":{"content":[
  {"type":"thinking","thinking":"..."},
  {"type":"text","text":"我来运行这个命令"},
  {"type":"tool_use","id":"toolu_01...","name":"Bash","input":{...}}
]}}
```

→ Codex 拆成 3 行 RolloutItem（按数组顺序保持 timestamp 单调递增）：

```json
{"type":"response_item","payload":{"type":"reasoning","summary":[{"type":"summary_text","text":"..."}],"content":null,"encrypted_content":null}}
{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"我来运行这个命令"}],"phase":"commentary"}}
{"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"...\",\"workdir\":\"...\"}","call_id":"toolu_01..."}}
```

**注意 phase**：assistant 在 tool_use 之前的 text 是 `commentary`（中途叙述），在所有 tool 完成之后的 text 是 `final_answer`。这是 Codex 的语义，CC 没区分——但反向翻译（Codex→CC）时全部并入 `[{type:text}]` 即可，模型自己理解。

### 2.2 反向 Codex → CC 的合并

Codex 同 turn 里相邻同 role 的 `message` 项，合并到一个 CC assistant/user 行的 content 数组里：

```
turn:
  reasoning → thinking
  message commentary → text
  function_call → tool_use
  function_call_output → tool_result（这是下一个 user 行）
  message final_answer → text
  → 合并到 CC：
    assistant 行 content = [thinking, text, tool_use]
    user 行 content = [tool_result]
    assistant 行 content = [text]
```

### 2.3 Image 内容

| CC | Codex |
|---|---|
| `{type:"image", source:{type:"base64", data, media_type}}` | `{type:"input_image", image_url, detail}` |
| `{type:"image", source:{type:"url", url}}` | 同上 |

CC 直接 base64 内嵌；Codex 写 `data:image/png;base64,...` 形式的 data URL（也支持 https URL）。`detail`：CC 没明确字段，默认看作 `high`。

转换：

```python
def cc_image_to_codex(block):
    src = block["source"]
    if src["type"] == "base64":
        url = f"data:{src['media_type']};base64,{src['data']}"
    else:
        url = src["url"]
    return {"type": "input_image", "image_url": url, "detail": "high"}
```

### 2.4 Redacted thinking

CC `{type:"redacted_thinking", data:"<bytes>"}` 是 Anthropic 内部加密思考片段。

→ Codex：转成 `reasoning` item，`summary=[]`，`content=null`，`encrypted_content=null`。**整段无文字内容**。模型读到等同于"这里有过一段思考但被遮蔽了"——Codex 模型也能 tolerate。

反方向不会出现（Codex 没有 redacted）。

### 2.5 Phase 推断（CC→Codex）

Codex `MessagePhase = commentary | final_answer | null`。CC 没有这个字段。推断规则：

```python
def infer_phase(assistant_blocks, position):
    # assistant_blocks 是当前 turn 内 assistant 的所有 content blocks
    # position 是当前 text block 在数组中的索引
    has_tool_after = any(b["type"] == "tool_use" for b in assistant_blocks[position+1:])
    return "commentary" if has_tool_after else "final_answer"
```

边界：如果整个 turn 没有 tool_use，单纯文本回复，`phase = final_answer`。

---

## 3. 工具清单总表

下表覆盖 CC 27 个工具 + Codex 全部出厂工具。列：

- **wire** = 工具调用时模型给的 `name`，决定 jsonl 写入字段
- **CC→Codex / Codex→CC**：转换策略关键字。详见 §3.x
- **损耗**：A=无损 / B=语义无损但字段精简 / C=结构性降级 / D=纯丢弃

| CC 工具 | Codex 等价 | wire (CC) | wire (Codex) | CC→Codex | Codex→CC | 损耗 |
|---|---|---|---|---|---|---|
| `Bash` | `exec_command` (现代) / `shell` (旧) | tool_use `Bash` | function_call `exec_command` | argv→cmd 字符串、`run_in_background`→PTY | cmd→`["bash","-lc",cmd]` 或直传 | A |
| `Read` | `exec_command cat`（无 builtin Read） | tool_use `Read` | function_call `exec_command` | 转 `cat -n <path>` 或 `sed -n 'M,Np'` | 模式匹配 cat → 反转 Read（不可靠，建议保留 exec_command） | B |
| `Edit` | `apply_patch` (Update File) | tool_use `Edit` | custom_tool_call `apply_patch` | 见 §3.1 | 拆 hunk → Edit 调用 | C |
| `Write` | `apply_patch` (Add File 或 Update File 全替换) | tool_use `Write` | custom_tool_call `apply_patch` | 见 §3.1 | Add File → Write | A |
| `MultiEdit` (废弃) | `apply_patch` (多 hunk Update File) | tool_use `MultiEdit` | custom_tool_call `apply_patch` | edits[] → 多 hunk | 反向 → MultiEdit 或多次 Edit | A |
| `NotebookEdit` | `apply_patch` (整文件 Update) | tool_use `NotebookEdit` | custom_tool_call `apply_patch` | 重组 ipynb JSON 后整文件替换 | apply_patch ipynb → NotebookEdit replace 整文件 | C |
| `Glob` | `exec_command rg --files -g <pattern>` | tool_use `Glob` | function_call `exec_command` | 直转 shell | 字符串匹配 → Glob（不可靠） | B |
| `Grep` | `exec_command rg <args>` | tool_use `Grep` | function_call `exec_command` | 参数表见 §3.4 | rg 命令解析回 Grep input（best-effort） | B |
| `WebFetch` | `exec_command curl` 或 `view_image`（图） | tool_use `WebFetch` | function_call `exec_command` | curl + 用 user message 包 url+prompt | curl 还原 → WebFetch（best-effort） | C |
| `WebSearch` | `web_search`（OpenAI builtin） | tool_use `WebSearch` | response_item `web_search_call` | input.query → action.search | web_search_call → WebSearch tool_use | A |
| `TaskCreate` / `TaskUpdate` / `TaskList` | `update_plan` | tool_use `TaskCreate` 等 | function_call `update_plan` | 见 §3.6 | 直译 step↔description | C |
| `TaskGet` / `TaskOutput` / `TaskStop` | — (Codex 无后台 task 系统) | tool_use `TaskOutput` 等 | — | 丢，串行化 | — | D |
| `Agent` (subagent) | `spawn_agent`/`wait_agent`/`close_agent` (v1/v2) | tool_use `Agent` | function_call `spawn_agent` | 见 §3.7 | 见 §3.7 | C |
| `Skill` | (Codex skills 注入到 developer message + 普通工具执行) | tool_use `Skill` | response_item `message role=developer` | 见 §3.8 | developer message 中含 SKILL 标记 → Skill | C |
| `EnterPlanMode` / `ExitPlanMode` | `turn_context.collaboration_mode = plan` | tool_use `EnterPlanMode` 等 | turn_context 字段 | 见 §3.9 | mode 切换 → 注入 EnterPlanMode tool_use | C |
| `EnterWorktree` / `ExitWorktree` | `exec_command git worktree add/remove` | tool_use `EnterWorktree` 等 | function_call `exec_command` | shell 等价 | 模式匹配 git worktree（不可靠） | B |
| `AskUserQuestion` | `request_user_input`（仅 plan mode）/ 降级为 assistant text | tool_use `AskUserQuestion` | function_call `request_user_input` 或 message | 见 §3.12 | 反向直译 | C |
| `CronCreate` / `CronList` / `CronDelete` | — | tool_use `CronCreate` 等 | — | 丢 + developer 提示 | — | D |
| `ScheduleWakeup` | — | tool_use `ScheduleWakeup` | — | 丢 + developer 提示 | — | D |
| `mcp__<server>__<tool>` | `function_call name=<tool> namespace=<server>` (codex §9.2) | tool_use `mcp__...` | function_call (含 namespace) | 直译，名字拆开 | namespace + name 重新拼回 mcp__ 前缀 | A |
| — | `view_image` | — | function_call `view_image` | （CC→Codex 不会出现） | tool_use `Read`（path=image，CC 自动识别） | A |
| — | `request_permissions` (granular 模式) | — | function_call `request_permissions` | （CC→Codex 不会出现） | 丢（CC 无对应） | D |
| — | `code_mode` 系列 | — | function_call `code_mode_*` | — | 拆为多个独立 tool_use（best-effort） | C |
| — | `create_goal` / `get_goal` / `update_goal` | — | function_call | — | 丢或转为 TaskCreate | D |

**总条目**：CC 28 工具（含废弃）→ Codex 5 主力工具 + ~15 辅助；Codex → CC 反向 ~15 工具 + 几个 CC 没的概念。

---

## 3.1 Edit / Write / MultiEdit / NotebookEdit ↔ apply_patch

这是翻译器最关键也最容易翻车的部分。Codex 端**所有**文件编辑都走 `apply_patch`（Lark grammar，见 codex §5）。

### 3.1.1 Write → apply_patch (Add File)

CC：
```json
{"type":"tool_use","name":"Write","id":"toolu_X","input":{
  "file_path":"/Users/alice/Desktop/agent-bridge/src/foo.py",
  "content":"import os\nprint(os.getcwd())\n"
}}
```

Codex（绝对路径 → 相对 cwd 的相对路径，因为 apply_patch 强制相对）：
```json
{"type":"custom_tool_call","name":"apply_patch","call_id":"toolu_X",
 "input":"*** Begin Patch\n*** Add File: src/foo.py\n+import os\n+print(os.getcwd())\n*** End Patch\n"}
```

**绝对→相对路径算法**：
```python
def cc_to_codex_path(abs_path: str, cwd: str) -> str:
    p = Path(abs_path).resolve()
    c = Path(cwd).resolve()
    try:
        return str(p.relative_to(c))
    except ValueError:
        # 文件在 cwd 之外（如 /tmp/foo），保留绝对路径
        # apply_patch grammar 不强校验，但工具描述说 "File references can only be relative"
        # 风险：模型/Codex 可能拒；建议先 chdir 到包含路径，或用 cd && exec_command 兜底
        raise CrossCwdError(abs_path, cwd)
```

**特殊情况：覆盖现有文件的 Write**。CC 的 Write 在文件已存在时是覆盖整个文件。apply_patch 用 `Add File` 在已存在文件上会失败（Codex 实测），所以必须改成：

```
*** Begin Patch
*** Update File: src/foo.py
@@
-<old line 1>
-<old line 2>
-...
+import os
+print(os.getcwd())
*** End Patch
```

需要先 Read 出旧内容才能构造完整 hunk。**翻译器实现**：在翻译 Write 调用之前先看会话里是否有对该文件的 Read 记录，若没有，必须降级为 `Delete File` + `Add File` 的组合（一段 patch 内可多个 op）：

```
*** Begin Patch
*** Delete File: src/foo.py
*** Add File: src/foo.py
+import os
+print(os.getcwd())
*** End Patch
```

### 3.1.2 Edit → apply_patch (Update File)

CC：
```json
{"type":"tool_use","name":"Edit","input":{
  "file_path":"/repo/app.py",
  "old_string":"def greet():\n    print(\"Hi\")",
  "new_string":"def greet():\n    print(\"Hello, world!\")"
}}
```

Codex apply_patch 的 hunk 格式要求：
1. 至少 ≤3 行 context（`@@` 开头）让解析器定位
2. `-` 行 = old_string 的每一行
3. `+` 行 = new_string 的每一行
4. context 必须 byte-exact

转换算法：
```python
def edit_to_apply_patch(edit_input, file_content_at_time):
    """file_content_at_time 是该 Edit 操作时文件的全文（必须从前序 Read 调用回放）"""
    old = edit_input["old_string"]
    new = edit_input["new_string"]
    relpath = cc_to_codex_path(edit_input["file_path"], cwd)

    # 找 old 在 file 中的位置
    idx = file_content_at_time.find(old)
    if idx == -1:
        raise EditNotFoundError("old_string not found in file content")

    # 取前后 context (≤3 行)
    pre = file_content_at_time[:idx].splitlines()[-3:]
    post = file_content_at_time[idx + len(old):].splitlines()[:3]

    lines = ["*** Begin Patch", f"*** Update File: {relpath}", "@@"]
    for l in pre:    lines.append(f" {l}")
    for l in old.splitlines(): lines.append(f"-{l}")
    for l in new.splitlines(): lines.append(f"+{l}")
    for l in post:   lines.append(f" {l}")
    lines.append("*** End Patch")
    return "\n".join(lines) + "\n"
```

**风险点**：
- 翻译器要"重放"文件内容来构造 hunk。如果会话里没有该文件的 Read 调用记录，就拿不到上下文 → 可以退化为 `@@`（无 header）+ 仅 `-` `+` 行（无 context）；apply_patch parser 在 Lenient 模式下能接受，但 byte 匹配仍要求 old_string 在文件里唯一。
- `replace_all: true` 时一次 Edit 对应**多个** apply_patch hunk（每个 occurrence 一个）。

### 3.1.3 MultiEdit → apply_patch（同文件多 hunk）

CC：
```json
{"type":"tool_use","name":"MultiEdit","input":{
  "file_path":"/repo/app.py",
  "edits":[
    {"old_string":"...A...","new_string":"...A'..."},
    {"old_string":"...B...","new_string":"...B'..."}
  ]
}}
```

→ Codex（一个 `*** Update File` 下两个 `@@` hunk）：

```
*** Begin Patch
*** Update File: app.py
@@
 <ctx>
-...A...
+...A'...
 <ctx>
@@
 <ctx>
-...B...
+...B'...
 <ctx>
*** End Patch
```

Hunk 出现顺序按 edits 数组顺序；context 各自独立计算。

### 3.1.4 反向 apply_patch → CC

每个 `*** Update File` hunk 对应一次 CC Edit（或者合并成 MultiEdit）：

```python
def apply_patch_to_cc_edits(patch_str, cwd):
    parsed = parse_lark(patch_str)  # 用 Lark grammar 重解析
    edits = []
    for op in parsed.operations:
        if op.kind == "add":
            edits.append({"tool":"Write", "input":{
                "file_path": str(Path(cwd) / op.path),
                "content": "\n".join(op.lines) + "\n"
            }})
        elif op.kind == "delete":
            # CC 无 Delete tool；用 Bash rm
            edits.append({"tool":"Bash", "input":{
                "command": f"rm '{Path(cwd) / op.path}'",
                "description": "Delete file (translated from apply_patch)"
            }})
        elif op.kind == "update":
            for hunk in op.hunks:
                old_lines = [l[1:] for l in hunk.lines if l.startswith("-")]
                new_lines = [l[1:] for l in hunk.lines if l.startswith("+")]
                edits.append({"tool":"Edit", "input":{
                    "file_path": str(Path(cwd) / op.path),
                    "old_string": "\n".join(old_lines),
                    "new_string": "\n".join(new_lines)
                }})
            if op.move_to:
                edits.append({"tool":"Bash", "input":{
                    "command": f"mv '{Path(cwd)/op.path}' '{Path(cwd)/op.move_to}'"
                }})
    return edits
```

**几个 hunk 是合成 MultiEdit 还是拆成多个 Edit**：MultiEdit 在当前 CC（v2.1.131）虽 [bin] 仍注册但已不在 system prompt 里；模型不会 spontaneously 调用它。**翻译器统一拆成多次 Edit**，更安全。

### 3.1.5 NotebookEdit → apply_patch

NotebookEdit 是按 cell 操作的（`replace`/`insert`/`delete`），但 apply_patch 没有 cell 语义。**唯一可行路径**：把 .ipynb 当成普通 JSON 文件，整文件 Update File。

代价：模型要看到完整 ipynb（可能很大）；翻译器可省略 patch 体的 hunks，直接用 `*** Update File` + `*** End of File` 标记表示整文件替换（apply_patch 的等价用法）。

实操建议：**翻译器对 NotebookEdit 的转换给一个 hint 说明**，让目标 agent 用 exec_command + json 操作处理 .ipynb，而不是真的塞进 apply_patch 里。

---

## 3.2 Bash ↔ exec_command / shell

### 3.2.1 字段对齐

| CC `Bash.input` | Codex `exec_command.arguments` | 备注 |
|---|---|---|
| `command` (string) | `cmd` (string) | 直译 |
| `description` (string) | — | 丢，仅 UI |
| `timeout` (ms) | `yield_time_ms` 或 `timeout_ms` | yield_time 是"等多久就把 stdout 返回"，CC 没等价；建议 timeout→`max(yield_time, default 1000)` |
| `run_in_background` (bool) | （隐含；用 `exec_command` + 不 wait） | Codex 通过 PTY session_id 实现"后台" |
| `dangerouslyDisableSandbox` | `sandbox_permissions: "require_escalated"` + `justification` | 见 codex §6.2 |

### 3.2.2 实例

CC：
```json
{"type":"tool_use","name":"Bash","input":{
  "command":"npm test",
  "description":"Run test suite",
  "timeout":120000
}}
```

Codex：
```json
{"type":"function_call","name":"exec_command","call_id":"toolu_X",
 "arguments":"{\"cmd\":\"npm test\",\"workdir\":\"/Users/alice/repo\",\"yield_time_ms\":1000,\"max_output_tokens\":12000}"}
```

`arguments` 必须是 stringified JSON（Responses API 规范）。`workdir` 从 CC `cwd` 字段取。

### 3.2.3 后台任务的特殊处理

CC `run_in_background:true` → 启动一个 background task（写到 `~/.claude/tasks/<sess>/<id>.json`）。Codex 没有 first-class 后台 task，但 `exec_command` 不指定 `yield_time_ms` 短值时，命令长跑会被自动归到 PTY session（返回 `session_id`），后续用 `write_stdin` poll。

**翻译策略**：CC 后台 Bash → Codex `exec_command` 设 `yield_time_ms: 1000`（短超时）+ 后续如果模型查询同 task，转成 `write_stdin {session_id, chars:"", yield_time_ms}` 的 polling 调用。

`TaskOutput` ↔ `write_stdin`：

| CC `TaskOutput.input` | Codex `write_stdin.arguments` |
|---|---|
| `task_id` (background bash 的 id) | `session_id` (number) — 需要从前一次 `exec_command` 输出的 `session_id` 字段提取 |
| `block: true` | `yield_time_ms: 30000` 或更长 |
| `timeout` | `yield_time_ms` |

`TaskStop` → 没有直接等价；可发 `write_stdin {session_id, chars:""}` (Ctrl+C) 或 `exec_command {cmd:"kill -9 <pid>"}`。

### 3.2.4 反向 exec_command → Bash

```python
def codex_exec_to_cc_bash(args):
    return {
        "command": args["cmd"],
        "description": "(translated)",
        "timeout": args.get("yield_time_ms", 120000) * 60  # Codex yield_time 是短轮询，CC timeout 是真超时；放大
    }
```

**`workdir`**：CC Bash 没有 `workdir` 字段，必须用 `cd` 包：

```python
if args.get("workdir") and args["workdir"] != cwd:
    cmd = f"cd {shlex.quote(args['workdir'])} && {args['cmd']}"
```

`shell` (argv) → CC Bash：argv 数组 join 成单字符串。如果首项是 `bash -lc`，直接取后面的字符串。

---

## 3.3 Read ↔ exec_command cat

CC 的 `Read` 是高级 tool（line-based、PDF、image、notebook 都有 union output）。Codex 没有等价 builtin。

### 3.3.1 转换 — CC → Codex

```python
def cc_read_to_codex(input):
    path = input["file_path"]
    offset = input.get("offset")
    limit = input.get("limit")
    if offset and limit:
        cmd = f"sed -n '{offset},{offset+limit-1}p' {shlex.quote(path)}"
    elif offset:
        cmd = f"tail -n +{offset} {shlex.quote(path)}"
    elif limit:
        cmd = f"head -n {limit} {shlex.quote(path)}"
    else:
        cmd = f"cat -n {shlex.quote(path)}"
    return {"name":"exec_command", "arguments": json.dumps({
        "cmd": cmd, "workdir": cwd, "yield_time_ms": 1000
    })}
```

**带行号 (`cat -n`)**：CC Read 输出格式是 `1\t<line>` 风格（带行号的 cat）。翻译时刻意用 `cat -n` 保持等价。

### 3.3.2 PDF / image / notebook 特殊情况

CC Read 对图片返回 `{type:"image", source:{base64}}`，这在 Codex 里要特殊处理：

```python
def cc_read_image_to_codex(input):
    # 转用 Codex 的 view_image
    return {"name":"view_image", "arguments": json.dumps({
        "path": input["file_path"], "detail":"original"
    })}
```

PDF：CC 拆页处理，Codex 没等价。退化为 `exec_command pdftotext` 或注入说明。

Notebook：`exec_command jupyter nbconvert --to script` 或者直接 `cat`（ipynb 是 JSON）。

### 3.3.3 Codex → CC 反向

如果 Codex 调了 `exec_command cat <path>` 风格命令，能否反推为 CC Read？**不建议**——因为：
1. 命令模式匹配脆弱（`cat` 可能跟管道、参数）
2. CC Read 的 readFileState 缓存语义不易模拟
3. 模型对两种调用反应一样

**默认策略**：保留 exec_command 调用不变。CC 端模型读到 "I executed `cat foo.txt`" 的历史完全能继续对话。

---

## 3.4 Glob / Grep ↔ exec_command rg

### 3.4.1 Glob → exec_command

```python
def cc_glob_to_codex(input):
    pattern = input["pattern"]; path = input.get("path", ".")
    cmd = f"rg --files -g {shlex.quote(pattern)} {shlex.quote(path)}"
    return {"name":"exec_command", "arguments": json.dumps({
        "cmd": cmd, "workdir": cwd, "yield_time_ms": 5000
    })}
```

CC Glob 上限 100 文件，Codex shell 无上限——可加 `| head -100` 保持等价。

### 3.4.2 Grep → exec_command

CC Grep 入参极复杂（参数 16+），全部映射到 rg 命令行：

| CC Grep input | rg flag | 备注 |
|---|---|---|
| `pattern` | 位置参数 | |
| `path` | 位置参数 | 默认 `.` |
| `glob` | `-g <glob>` | |
| `output_mode: content` | （默认） | |
| `output_mode: files_with_matches` | `-l` | |
| `output_mode: count` | `-c` | |
| `-A`, `-B`, `-C` | 同名 flag | |
| `context` | `-C <n>` | |
| `-n` | `-n`（默认 true） | |
| `-i` | `-i` | |
| `type` | `--type <type>` | |
| `head_limit` | `\| head -n <N>` | shell 管道 |
| `offset` | `\| tail -n +<N>` | |
| `multiline` | `-U --multiline-dotall` | |

```python
def cc_grep_to_codex(input):
    args = ["rg"]
    if input.get("output_mode") == "files_with_matches": args.append("-l")
    elif input.get("output_mode") == "count":             args.append("-c")
    if input.get("-i"): args.append("-i")
    if input.get("multiline"): args.extend(["-U","--multiline-dotall"])
    for f in ("-A","-B","-C","context","type"):
        if f in input: args.extend([f if f != "context" else "-C", str(input[f])])
    if "glob" in input: args.extend(["-g", input["glob"]])
    args.extend([shlex.quote(input["pattern"]), shlex.quote(input.get("path", "."))])
    cmd = " ".join(args)
    if "head_limit" in input: cmd += f" | head -n {input['head_limit']}"
    if "offset" in input: cmd += f" | tail -n +{input['offset']}"
    return {"name":"exec_command", "arguments": json.dumps({
        "cmd": cmd, "workdir": cwd, "yield_time_ms": 10000
    })}
```

### 3.4.3 反向不做

`exec_command rg ...` → CC `Grep` 反推不做。原因同 §3.3.3。

---

## 3.5 WebFetch / WebSearch

### 3.5.1 WebSearch ↔ web_search_call

CC：
```json
{"type":"tool_use","name":"WebSearch","input":{
  "query":"what is MCP protocol",
  "allowed_domains":["anthropic.com"],
  "blocked_domains":["spam.com"]
}}
```

Codex 端 web_search 是 OpenAI Responses 内建工具，wire 不是 function_call 而是 `web_search_call`：

```json
{"type":"response_item","payload":{
  "type":"web_search_call","status":"completed",
  "action":{"type":"search","query":"what is MCP protocol","queries":["what is MCP protocol"]}
}}
```

**字段差异**：
- `allowed_domains` / `blocked_domains` → Codex 没等价；丢弃，仅在 query 文本里加 hint
- `action.type` 还有 `open_page` / `find_in_page`，CC `WebFetch` 才对应

CC `WebFetch` → Codex `web_search_call action=open_page`：

```json
{"action":{"type":"open_page","url":"https://example.com"}}
```

但 CC WebFetch 还有个 `prompt` 字段（让小模型对页面归纳）——Codex web_search 没这语义。**降级**：把 prompt 作为下一条 user/developer message。

### 3.5.2 WebFetch 的另一个降级路径：curl

如果目标 Codex 没启用 web_search（或翻译器不想用 OpenAI builtin），用 `exec_command curl`：

```python
def cc_webfetch_to_codex_curl(input):
    cmd = f"curl -sL --max-time 30 {shlex.quote(input['url'])}"
    return [
        # 第一步：fetch
        {"name":"exec_command", "arguments": json.dumps({
            "cmd": cmd, "workdir":"/", "yield_time_ms": 5000, "max_output_tokens": 50000
        })},
        # 第二步：模型自己用 prompt 解析（无需额外 tool 调用）
        # 把 prompt 作为对话注入，例如 user message："以下网页内容请按以下要求分析：<prompt>"
    ]
```

### 3.5.3 反向 web_search_call → CC WebSearch

```python
def codex_websearch_to_cc(payload):
    action = payload.get("action", {})
    if action.get("type") == "search":
        return {"tool":"WebSearch", "input":{"query": action["query"]}}
    elif action.get("type") == "open_page":
        return {"tool":"WebFetch", "input":{"url": action["url"], "prompt": "summarize"}}
    elif action.get("type") == "find_in_page":
        # CC 无对应；降级为 WebFetch + 后续模型自己 grep
        return {"tool":"WebFetch", "input":{"url": action["url"],
                "prompt": f"find the pattern: {action.get('pattern','')}"}}
```

`web_search_call` **没有显式 output**——OpenAI 服务端结果直接由模型在下一 message 里复述。CC 的 WebSearch tool_result 是结构化的 `{query, results:[...]}`，反向时**伪造一个空结果**：

```json
{"type":"tool_result","tool_use_id":"...","content":"(Web search executed; results inlined in subsequent assistant message.)"}
```

---

## 3.6 TaskCreate / TaskUpdate / TaskList ↔ update_plan

### 3.6.1 字段差异

| CC TaskCreate input | Codex update_plan plan[i] |
|---|---|
| `subject` | `step` |
| `description` | （丢失，或合并到 step） |
| `activeForm` | （丢失） |
| `metadata` | （丢失） |

| CC TaskUpdate input | Codex update_plan |
|---|---|
| `taskId` + `status:"in_progress"` | 整个 plan 数组重发，目标 step 改 status |
| `addBlockedBy` / `addBlocks` | （丢失，Codex 无 DAG） |

### 3.6.2 状态机 — 维护"虚拟 plan"

CC 有 task 持久化（`~/.claude/tasks/<sess>/<id>.json`），每次只更新一条；Codex `update_plan` 一次发整个 plan 数组。**翻译器需要内部维护一个 task list**，每次 CC TaskCreate/TaskUpdate 后从内部状态重新发一次完整 update_plan。

```python
class TaskListState:
    def __init__(self):
        self.tasks = []  # [{id, subject, status, description}]

    def on_cc_task_create(self, input, tool_use_id):
        self.tasks.append({
            "id": tool_use_id,  # CC TaskCreate output 才有 id，但翻译时未知，先用 tool_use_id 占位
            "subject": input["subject"],
            "description": input.get("description", ""),
            "status": "pending",
            "activeForm": input.get("activeForm")
        })
        return self._render_codex_update_plan()

    def on_cc_task_update(self, input):
        for t in self.tasks:
            if t["id"] == input["taskId"]:
                if "status" in input: t["status"] = input["status"]
                if "subject" in input: t["subject"] = input["subject"]
                # blockedBy/Blocks/metadata 丢
        return self._render_codex_update_plan()

    def _render_codex_update_plan(self):
        return {
            "name":"update_plan",
            "arguments": json.dumps({
                "plan": [{"step": t["subject"], "status": t["status"]} for t in self.tasks],
                "explanation": ""
            })
        }
```

每次 CC TaskCreate/TaskUpdate 都触发一次 Codex `update_plan`。这意味着 Codex 端 jsonl 会比 CC 多 N 条调用——这不是问题，Codex 模型常这样调。

### 3.6.3 反向 update_plan → CC

简单：第一次 update_plan 拆成多个 TaskCreate（按 plan 数组每个 step 一次），后续 update_plan 与上次 diff，diff 结果转成 TaskUpdate 调用：

```python
def codex_plan_to_cc_diff(prev_plan, new_plan):
    cc_calls = []
    prev_by_step = {p["step"]: p for p in prev_plan}
    for i, np in enumerate(new_plan):
        if np["step"] not in prev_by_step:
            cc_calls.append({"tool":"TaskCreate", "input":{
                "subject": np["step"], "description":"(translated from Codex update_plan)"
            }})
        elif prev_by_step[np["step"]]["status"] != np["status"]:
            cc_calls.append({"tool":"TaskUpdate", "input":{
                "taskId": f"task-{i}", "status": np["status"]
            }})
    return cc_calls
```

注意 `taskId` 反向时找不到（Codex 没存 id），可以用 step 名作 fallback ID。

### 3.6.4 TaskGet / TaskList / TaskOutput / TaskStop

- `TaskGet` / `TaskList`：Codex `update_plan` 不查询，所以这两个调用无对应。**翻译时丢，把 query 结果以注释方式插入**："翻译者注：用户在 CC 端查询了 task <id>，结果是 <内容>"。模型读到能继续对话。
- `TaskOutput` / `TaskStop`：用于 background bash，已在 §3.2.3 处理。

---

## 3.7 Agent (subagent) ↔ spawn_agent / wait_agent

最复杂的一块。CC 和 Codex 都有 subagent 概念，但 wire 完全不同。

### 3.7.1 CC Agent 实例（同步）

```json
// assistant 行
{"type":"tool_use","name":"Agent","id":"toolu_X","input":{
  "description":"Search for X",
  "prompt":"<long markdown task spec>",
  "subagent_type":"general-purpose",
  "model":"sonnet",
  "run_in_background":false
}}

// user 行 (tool_result)
{"type":"tool_result","tool_use_id":"toolu_X","content":[{
  "type":"text",
  "text":"<最终 markdown 报告 from subagent>"
}]}
```

子 transcript 在 `<sess>/subagents/agent-<agentId>.jsonl`（**不在主 jsonl**）。

### 3.7.2 Codex spawn_agent 实例（v2）

```json
// 主会话 function_call
{"type":"function_call","name":"spawn_agent","call_id":"call_X",
 "arguments":"{\"agent_type\":\"general\",\"task_name\":\"search-x\",\"message\":\"<prompt>\"}"}

// 主会话 function_call_output
{"type":"function_call_output","call_id":"call_X",
 "output":"{\"agent_id\":\"019df...\",\"status\":\"started\"}"}

// 后续 wait_agent
{"type":"function_call","name":"wait_agent","arguments":"{\"agent_id\":\"019df...\"}",...}
{"type":"function_call_output","output":"{\"final_message\":\"<结果>\"}",...}
```

子 transcript 在 `~/.codex/sessions/YYYY/MM/DD/rollout-...-<sub-uuid>.jsonl`，独立文件，`SessionMeta.source = SubAgent(...)` + `agent_role/agent_nickname`。

### 3.7.3 CC → Codex 翻译策略

#### 策略 A：Inline 展开（推荐，默认）

把 CC 的 subagent 调用 inline 展开成主对话里的多条 message：

```python
def inline_subagent(cc_main_event, subagent_jsonl_path):
    # 1. 读子 transcript
    sub_events = read_jsonl(subagent_jsonl_path)
    # 2. 注入主 session 的 developer message：
    inject_msg = f"--- begin subagent transcript ({len(sub_events)} events) ---"
    yield codex_message("developer", inject_msg)
    # 3. 把子 transcript 的每条 message/tool_call/tool_result 都翻译成主链上的 RolloutItem
    for ev in sub_events:
        # 注意：子 transcript 的 user/assistant role 都保留，只是模型读到时知道这是 subagent 内部对话
        for item in cc_event_to_codex(ev):
            yield item
    yield codex_message("developer", "--- end subagent transcript ---")
    # 4. 把 CC 的 final result 作为 developer message 总结
    final = cc_main_event["message"]["content"][0]["text"]
    yield codex_message("developer", f"Subagent final result:\n{final}")
```

成本：主对话 token 膨胀。但好处是"完整可读"。

#### 策略 B：保留 spawn_agent 调用，子文件分离

把 CC 子 transcript 翻译成独立的 Codex jsonl（放在 sessions/YYYY/MM/DD/），主会话用 `spawn_agent` + `wait_agent` 调用对接。

```python
def split_subagent(cc_main_event, subagent_jsonl_path, codex_sessions_dir):
    sub_uuid = uuid7()
    sub_path = codex_path(codex_sessions_dir, datetime.utcnow(), sub_uuid)
    # 翻译子 transcript
    sub_lines = translate_full_session(read_jsonl(subagent_jsonl_path), source="subagent",
                                       parent_id=main_session_id)
    write_jsonl(sub_path, sub_lines)
    # 主会话的 RolloutItem
    yield function_call("spawn_agent", call_id=cc_main_event["uuid"], arguments={
        "agent_type": cc_main_event["input"]["subagent_type"],
        "task_name": cc_main_event["input"]["description"],
        "message": cc_main_event["input"]["prompt"]
    })
    yield function_call_output(call_id=cc_main_event["uuid"], output=json.dumps({
        "agent_id": sub_uuid, "status": "completed"
    }))
    yield function_call("wait_agent", call_id=cc_main_event["uuid"]+"-wait", arguments={
        "agent_id": sub_uuid
    })
    yield function_call_output(call_id=cc_main_event["uuid"]+"-wait", output=json.dumps({
        "final_message": cc_main_event["result_text"]
    }))
```

好处：结构对齐，token 不膨胀。坏处：在 Codex picker 里多出 subagent 会话条目（用 `--include-non-interactive` 才能看到）。

#### 策略 C：纯文本降级

`Agent` tool_use → 直接转成主对话的 developer message：

```
{"type":"response_item","payload":{"type":"message","role":"developer","content":[{
  "type":"input_text",
  "text":"Subagent dispatched (description: 'Search for X', type: general-purpose)\nResult:\n<final markdown>"
}]}}
```

Token 最省，结构最弱。**适用于**：Codex 不打算继续与 subagent 互动，仅作为历史读。

### 3.7.4 异步 Agent 的特殊处理

CC `run_in_background:true` 的 Agent → CC 主会话立即得到 `{status:"async_launched", agentId, outputFile}` 类型的 tool_result，后面通过 `queue-operation:enqueue` 注入 `<task-notification>` 通知模型。

**Codex 翻译**：用 `spawn_agent` + 不立即 `wait_agent`；后面 CC `<task-notification>` 时翻译成 `wait_agent` + `function_call_output`。这天然对应 Codex 的 spawn/wait 语义，比同步还干净。

### 3.7.5 反向 Codex spawn_agent → CC Agent

逆操作：

1. 读 `function_call name=spawn_agent` + 后续 `wait_agent` 的 output
2. 找到对应的子 jsonl 文件路径（通过 `agent_id`）
3. 翻译子 jsonl → CC 子 transcript 格式（独立 jsonl in `<sess>/subagents/`），加 `meta.json`
4. 主会话生成 `Agent` tool_use + tool_result（result 用 `wait_agent` 的 final_message）

```python
def codex_spawn_to_cc_agent(spawn_call, wait_output, codex_sub_jsonl):
    args = json.loads(spawn_call["arguments"])
    final = json.loads(wait_output["output"])["final_message"]
    sub_id = json.loads(spawn_call_output["output"])["agent_id"]
    return {
        "main_assistant": {
            "tool_use": {
                "id": spawn_call["call_id"],
                "name":"Agent",
                "input":{
                    "description": args["task_name"],
                    "prompt": args["message"],
                    "subagent_type": args["agent_type"]
                }
            }
        },
        "main_user": {
            "tool_result":{
                "tool_use_id": spawn_call["call_id"],
                "content":[{"type":"text","text": final}]
            }
        },
        "sub_jsonl_path": f"<sess>/subagents/agent-{sub_id}.jsonl",
        "sub_meta": {"agentType": args["agent_type"], "description": args["task_name"]},
        "sub_content": translate_codex_to_cc(codex_sub_jsonl)
    }
```

---

## 3.8 Skill ↔ Codex skills (developer message)

### 3.8.1 CC 端 Skill 调用

CC `Skill` 工具调用后，harness 做两件事：

1. 写 `attachment / type=invoked_skills`，记录 skill 元信息
2. 注入一条 `user` 文本消息（`{type:"text", text:"# Update Config Skill\n..."}`），是 SKILL.md 全文

`tool_result` 仅在错误时出现。

### 3.8.2 Codex 端 skills

Codex 自己有 `~/.codex/skills/` 目录与 SKILL.md。skills 的 listing 注入到 developer message（base_instructions 或 user_instructions），调用时**没有专门的 wire tool**——模型直接执行 skill 文档里描述的工具序列。

### 3.8.3 翻译策略

CC `Skill` tool_use → Codex 端：
1. 把 SKILL.md 内容（从 `attachment / invoked_skills.skills[].content` 取）作为 developer message 注入
2. 不发 function_call（Codex 没 Skill 工具）
3. CC 在 user-text 里看到的 SKILL.md 内容，在 Codex 里以 developer message 形式存在

```python
def cc_skill_to_codex(cc_skill_use, cc_invoked_skills_attachment):
    skill_md = cc_invoked_skills_attachment["skills"][0]["content"]
    return [
        codex_message("developer", f"Skill `{cc_invoked_skills_attachment['skills'][0]['name']}` was invoked:\n\n{skill_md}")
    ]
```

注意：**不要保留 CC 的 `tool_use Skill` 作为 Codex function_call**——Codex 端没有这个 wire name 的工具，模型 resume 时会困惑。

### 3.8.4 反向 Codex → CC

Codex developer message 里如果带 `Skill ... was invoked:` 这种 marker（翻译器自己注入的），可以反推为 CC `Skill` 调用。但 Codex 原生 skills 注入不带这个 marker，普通 developer message 也能含 SKILL.md 内容——**所以反向不可靠**。

**默认**：Codex → CC 时，所有 developer message 全部转成 CC `attachment / type=skill_listing` 或 `user` text，**不重建 `tool_use Skill`**。

---

## 3.9 EnterPlanMode / ExitPlanMode ↔ collaboration_mode

### 3.9.1 CC 端

`EnterPlanMode` tool_use（无参）→ harness 切到 plan-only 模式（屏蔽 Edit/Write/Bash 写）。同时 `permission-mode` 行变 `plan`。

`ExitPlanMode` 带 `allowedPrompts: [{tool:"Bash", prompt:"run tests"}]`，输出包含 `plan: string|null` `filePath?` 等。

### 3.9.2 Codex 端

`turn_context.collaboration_mode = {mode:"plan", settings:{...}}`。Plan mode 下 `request_user_input` 工具被注册。也有 `update_plan` 用作 plan 表达。**没有"Enter/Exit"显式工具调用**——切换发生在 turn 之间的 `turn_context` 重写上。

### 3.9.3 翻译策略

CC EnterPlanMode → Codex 下一个 `turn_context` 写 `collaboration_mode.mode = "plan"`：

```python
def cc_enter_plan_mode(cc_event):
    # 不发 function_call；改下一个 turn_context
    return [
        # 当前 turn 立即写一条新 turn_context（mode 切换）
        rollout_turn_context(mode="plan", model=current_model, cwd=cwd, ...),
        # CC 的 EnterPlanMode tool_use+result 整体丢弃（或注入 developer 注释）
        codex_message("developer", "(translated) Plan mode started.")
    ]
```

CC ExitPlanMode → Codex turn_context 切回 `default`：

```python
def cc_exit_plan_mode(cc_event):
    plan_text = cc_event["tool_use"]["input"].get("plan", "(empty)")
    return [
        rollout_turn_context(mode="default", ...),
        codex_message("developer", f"(translated) Plan mode ended.\n\nPlan:\n{plan_text}")
    ]
```

### 3.9.4 反向 Codex → CC

Codex `turn_context` 看到 `collaboration_mode.mode` 变化时，注入 CC `EnterPlanMode` / `ExitPlanMode` 的 tool_use+result 配对：

```python
def codex_mode_switch_to_cc(prev_mode, new_mode):
    if prev_mode == "default" and new_mode == "plan":
        return [{
            "assistant_tool_use": {"name":"EnterPlanMode", "input":{}},
            "user_tool_result": {"content":"Entered plan mode."}
        }]
    elif prev_mode == "plan" and new_mode == "default":
        return [{
            "assistant_tool_use": {"name":"ExitPlanMode", "input":{}},
            "user_tool_result": {"content":"Exited plan mode."}
        }]
```

---

## 3.10 EnterWorktree / ExitWorktree ↔ exec_command git worktree

直译为 shell：

```python
def cc_enter_worktree(input):
    if "name" in input:
        cmd = f"git worktree add .claude/worktrees/{input['name']}"
    elif "path" in input:
        cmd = f"git worktree list | grep -F {shlex.quote(input['path'])}"  # 验证而非创建
    return {"name":"exec_command", "arguments": json.dumps({"cmd": cmd, "workdir": cwd})}

def cc_exit_worktree(input):
    if input["action"] == "remove":
        cmd = f"git worktree remove --force {worktree_path}"
        if input.get("discard_changes"): cmd += " && git branch -D <branch>"
    else:  # keep
        cmd = "echo 'Worktree kept.'"
    return {"name":"exec_command", "arguments": json.dumps({"cmd": cmd, "workdir": cwd})}
```

**问题**：CC EnterWorktree 内部还会切 cwd（session 视角的 cwd 会变成 worktree 路径）。Codex 这边只是发 shell，**cwd 不会随 git worktree add 自动改变**。翻译时记得后续所有 `exec_command` 的 `workdir` 改为新 worktree 路径。

---

## 3.11 AskUserQuestion ↔ request_user_input

### 3.11.1 字段映射

| CC AskUserQuestion | Codex request_user_input | 备注 |
|---|---|---|
| `questions[i].question` | `questions[i].question` | 直译 |
| `questions[i].header` | （丢） | Codex 无 chip |
| `questions[i].multiSelect` | `questions[i].kind: "choice"` + 加 `multi: true` 字段（推断） | unverified Codex 实际 schema |
| `questions[i].options[].label` | `questions[i].choices[]` | 字符串数组 |
| `questions[i].options[].description` | （合并到 question 文本，加 `(<description>)` 后缀） | Codex 无 description |
| `questions[i].options[].preview` | （丢） | Codex 不支持 markdown preview |

### 3.11.2 Plan mode 才注册

Codex 的 `request_user_input` **仅在 collaboration_mode != default** 时注册（codex §4.8）。这意味着如果 CC 在 default mode 下用了 AskUserQuestion，**Codex 端没法 1:1 翻译**——必须先切 plan mode 或降级。

**降级策略**：转为普通 assistant message 提问 + 等待 user message 回答：

```python
def cc_ask_user_to_codex(cc_input):
    # 在 default mode 下不发 function_call，改成 assistant 文本
    questions = cc_input["questions"]
    text = "\n\n".join([
        f"**{q['question']}**\n" + "\n".join([f"- {o['label']}: {o.get('description','')}" for o in q["options"]])
        for q in questions
    ])
    return codex_message("assistant", text, phase="final_answer")
```

### 3.11.3 反向

Codex `request_user_input` → CC `AskUserQuestion`：直译 questions + choices → options。`header` 用 question 前 12 字符截断生成。

---

## 3.12 ScheduleWakeup / Cron* ↔ (no equivalent)

Codex 没有等价工具。

CC → Codex：丢调用，但**插入一条 developer message** 让模型知道：

```
{"type":"response_item","payload":{"type":"message","role":"developer",
 "content":[{"type":"input_text","text":"(translated) The user attempted to schedule a recurring task: <cron expression> running prompt <prompt>. This functionality is not available in Codex. Please remind the user to set up an external cron."}]}}
```

反向不会出现（Codex 没产生 cron 调用）。

---

## 3.13 MCP 工具

CC wire name `mcp__<server>__<tool>` ↔ Codex `function_call name=<tool> namespace=<server>`：

```python
def cc_mcp_to_codex(cc_tool_use):
    parts = cc_tool_use["name"].split("__")
    assert parts[0] == "mcp"
    server = parts[1]; tool = "__".join(parts[2:])
    return {
        "name": tool, "namespace": server,
        "arguments": json.dumps(cc_tool_use["input"]),
        "call_id": cc_tool_use["id"]
    }

def codex_mcp_to_cc(codex_fc):
    if "namespace" in codex_fc:
        return {
            "name": f"mcp__{codex_fc['namespace']}__{codex_fc['name']}",
            "input": json.loads(codex_fc["arguments"]),
            "id": codex_fc["call_id"]
        }
```

**前提**：两边都连同一 MCP server。否则 Codex resume 时该工具不存在，模型只看到历史调用文字而无法继续 invoke。

`SessionMeta.dynamic_tools` 列出当前 session 注册的 MCP 工具，翻译时复制过去：

```python
session_meta["dynamic_tools"] = [
    {"name": tool_name, "namespace": server_name, "description": "...", "schema": {...}}
    for (server, tool) in mcp_tools_used_in_cc_session
]
```

---

## 4. 特殊场景

### 4.1 Subagent 完整翻译 — 选择策略

§3.7 给了三种策略。在翻译器实现里：

| 触发条件 | 推荐策略 |
|---|---|
| 子 transcript 短（<50 events） | 策略 A：inline |
| 子 transcript 长（>50 events）且与主线无强耦合 | 策略 B：分文件 |
| 异步 Agent 仅返回 status_launched，主对话不依赖结果 | 策略 C：纯文本降级 |
| 嵌套 subagent（subagent 又调 subagent） | 一律策略 B（递归） |

**默认**：A（inline），可配置切换。

### 4.2 Skills 元信息保留

CC 的 `attachment / type=skill_listing`（启动时整列 skill 一览）→ Codex `developer` message 单独一条：

```
{"role":"developer","content":[{"type":"input_text","text":
 "Available skills (translated from Claude Code):\n\n- skill_a: ...\n- skill_b: ...\n"}]}
```

**不要**把 skill 内容作为 Codex 的 `[skills]` 配置注入——Codex 自己有 `~/.codex/skills/`，会冲突。

### 4.3 Plan mode 文件痕迹

CC ExitPlanMode 输出含 `filePath` 指向 `~/.claude/plans/<id>.md`。这是 plan 内容的持久化。

翻译 → Codex：
- `filePath` 转 base64 内联到 developer message（Codex 没有 plans/ 目录概念）
- 或者把 plan 内容直接写为 Codex 端 `update_plan` 调用

### 4.4 Hooks

CC `system / subtype=stop_hook_summary` 行只是 hook 执行结果记录，**不进对话上下文**。Codex 没有等价。**翻译时直接丢**。

如果用户在 CC 配置了 hook 把内容注入到对话（典型 `SessionStart` hook stdout），那部分内容会通过 attachment 出现在 jsonl 里，按 attachment 处理（§4.6）。

### 4.5 Compaction 双向映射

#### CC → Codex

CC 有 `system / subtype=compact_boundary` + 紧跟一行 `isCompactSummary:true` 的 user 行。

→ Codex 的等价是 `compacted` 顶层 RolloutItem：

```json
{"timestamp":"...","type":"compacted","payload":{
  "message":"<CC 的 isCompactSummary user.message.content>",
  "replacement_history":[<boundary 之后的对话作为新 history>]
}}
```

`replacement_history` 是 `Vec<ResponseItem>`，按 §1.2 转换 boundary 之后的所有 CC 行。

**注意**：`replacement_history` 里 Codex 默认会包含 `<permissions instructions>` `<collaboration_mode>` `<skills_instructions>` 等 XML 包裹的 developer messages（codex §3.6.7）。**翻译器应当也注入等价的 envelope**（虽然 CC 端没有这些，但 Codex 模型期待看到）。

#### Codex → CC

Codex `compacted` 顶层 → CC `system / subtype=compact_boundary` + isCompactSummary user：

```json
// 第一行
{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":"manual","preTokens":<unknown>,"postTokens":<from replacement_history token estimate>,"durationMs":0},"uuid":"<new>","logicalParentUuid":"<prev>","timestamp":"...","content":"Conversation compacted","level":"info","sessionId":"...",...}
// 第二行
{"type":"user","isCompactSummary":true,"isVisibleInTranscriptOnly":true,"parentUuid":"<boundary uuid>","uuid":"<new>","message":{"role":"user","content":"<replacement_history 摘要文字>"},"sessionId":"...",...}
```

`replacement_history` 里的 ResponseItem 序列**继续**翻译为 CC 行（user / assistant / tool_use / tool_result），从 boundary 之后开始追加。

#### `Compaction` ResponseItem（不同于顶层 `compacted`）

`response_item.compaction` 是模型作为对话内容插入的"摘要"，由 OpenAI 远端生成，`encrypted_content` 不可解码。

→ CC 翻译：直接转成 `assistant.message.content[].type=text` 的占位文本（"Previous context was compacted; details unavailable."），并丢 encrypted_content。**或者**完全跳过——Codex 后续 turn 不会再依赖它（除非又 resume 同一个 session 给 Codex）。

### 4.6 Attachments 详细映射

CC 10+ 种 attachment 子类型 → Codex：

| CC attachment.type | Codex 端处理 | role | 是否保留 |
|---|---|---|---|
| `skill_listing` | developer message，标 "Available skills:" | developer | 是 |
| `nested_memory` | developer message，"Project memory:\n" + content.content | developer | 是 |
| `task_reminder` | developer message，"Active tasks:\n- ..." | developer | 是 |
| `file` | user message，附 `<file path="...">` 标签 | user | 是 |
| `compact_file_reference` | （丢，仅 UI） | — | 否 |
| `edited_text_file` | user message，"User edited file: <path>\n```\n<snippet>\n```" | user | 是 |
| `command_permissions` | turn_context 字段（影响 sandbox/approval） | (turn_context) | 是 |
| `queued_command` | 下一 user message 前缀 | user | 是 |
| `date_change` | turn_context.current_date 更新 | (turn_context) | 是 |
| `invoked_skills` | developer message + (主 user-text 已包含 SKILL.md) | developer | 是（仅 metadata） |

### 4.7 `<system-reminder>` 与其他 XML 标签

CC 在系统提示里用 `<system-reminder>` `<env>` `<task-notification>` `<local-command-stdout>` `<tool_use_error>` 包裹特殊内容。

→ Codex 翻译时：
- `<system-reminder>` → developer message（Codex 端 base_instructions 已经有自己的 system layer，不要再嵌套）
- `<env>` → 写成纯文本注入到 user message 之前
- `<task-notification>` → developer message（翻译者注："async subagent completed"）
- `<local-command-stdout>` → developer message（slash command 历史）
- `<tool_use_error>` → tool_result 的 `output` 字段（Codex 用 `is_error` 概念但 schema 里没有，靠模型解析）

反向 Codex → CC：developer message 在 CC 端要包成 `<system-reminder>` 才符合 CC harness 期待，但**模型本身**不太挑剔——所以可以保守处理：

```python
def codex_developer_to_cc(content):
    return {
        "type":"user",
        "message":{
            "role":"user",
            "content":[{"type":"text","text": f"<system-reminder>\n{content}\n</system-reminder>"}]
        }
    }
```

---

## 5. Resume Protocol — 写到哪里、放什么字段

这一节是落地 resume 的强制规范。

### 5.1 CC → Codex：制造一份 Codex 能 `resume` 的 jsonl

#### 步骤

1. **生成 session UUIDv7**：`uuid7()`。原 CC sessionId（v4）保留在自定义 metadata 字段（后面提到）。
2. **决定文件路径**：
   ```
   ts = datetime.utcnow()
   path = f"{CODEX_HOME}/sessions/{ts.strftime('%Y/%m/%d')}/rollout-{ts.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid}.jsonl"
   os.makedirs(os.path.dirname(path), exist_ok=True)
   ```
3. **生成 SessionMeta 第一行**（必填）：
   ```json
   {"timestamp":"<ISO>","type":"session_meta","payload":{
     "id":"<UUIDv7>",
     "timestamp":"<ISO>",
     "cwd":"<原 CC cwd>",
     "originator":"agent-bridge",
     "cli_version":"0.1.0",
     "source":{"custom":"agent-bridge"},
     "model_provider":"openai",
     "git": {"commit_hash":"<from CC firstline gitBranch>","branch":"<...>","repository_url":null}
   }}
   ```
   `source: {"custom":"agent-bridge"}` 让 picker 默认隐藏（避免污染用户列表）；`codex exec resume <UUID>` **不需要** `--include-non-interactive` 即可直接 by-UUID 续会话（PoC 验证）。或设 `"cli"` 让它出现在主 picker。
   **`base_instructions` 字段可省**（PoC 验证）：手写 jsonl 不带它，Codex resume 行为正常，自动用配置的默认 base prompt。仅当需要给翻译会话注入特定前言（"以下对话来自 Claude Code"）时再填。
4. **生成第一个 turn_context**：
   ```json
   {"timestamp":"...","type":"turn_context","payload":{
     "turn_id":"<new uuid7>",
     "cwd":"<原 cwd>",
     "current_date":"<YYYY-MM-DD>",
     "timezone":"Asia/Shanghai",
     "approval_policy":"<由 CC permission-mode 映射，见 §6.2>",
     "sandbox_policy":{"type":"<由 CC permission-mode 映射>"},
     "model":"<目标 model 名，按 Codex config>",
     "summary":"none",
     "truncation_policy":{"mode":"tokens","limit":10000}
   }}
   ```
5. **遍历 CC 的 DAG 主链**：选用户钦定的 leaf（默认最近的非 sidechain leaf），沿 parentUuid 反向走到 null（compact_boundary 时走 logicalParentUuid），按时间顺序产出 RolloutItem 序列。
6. **每个 CC event 的转换规则按 §1.2、§3.x、§4.x**。
7. **追加最后一个 user prompt 占位**（可选）：如果用户希望 resume 后立即给一个新指令，可以追加一条 `response_item.message role=user` 作为待处理指令。或者不附加，让用户在 `codex resume <UUID> "<prompt>"` 时传。

#### 必备字段最小集

```jsonc
// 第一行
{"timestamp":"...","type":"session_meta","payload":{
  "id":"<UUIDv7>",
  "timestamp":"...",
  "cwd":"<必填>",
  "originator":"<必填>",
  "cli_version":"<必填>",
  "source":{"custom":"agent-bridge"}  // 或 "cli"
}}
// 第二行
{"timestamp":"...","type":"turn_context","payload":{
  "turn_id":"...",
  "cwd":"<同上>",
  "approval_policy":"<必填>",
  "sandbox_policy":{"type":"<必填>"},
  "model":"<必填>",
  "summary":"none"
}}
// 第三行起
{"timestamp":"...","type":"response_item","payload":{...}}
...
```

#### 不要做的事

- ❌ 不要写 `session_index.jsonl`（codex §2.4），让 Codex 自己回填 SQLite
- ❌ 不要伪造 `encrypted_content`（无法生成有效 token）
- ❌ 不要把 thinking 翻译成 Codex 的 `reasoning.encrypted_content`——置 null 即可
- ❌ 不要假设 SessionMeta 之后必须紧跟 turn_context；Codex 接受任意顺序但默认顺序更稳

#### Resume 命令

```bash
# 直接 by-UUID 续（推荐；不进 TUI、不需要 picker 标志）
codex exec resume <UUIDv7> "可选的新 prompt" -o /tmp/out.md

# 进 TUI（用户场景）
codex resume <UUIDv7>

# 仅当从 picker 选 source=Custom 的会话时，才需要：
codex resume --include-non-interactive
# (--include-non-interactive 只影响 picker 列出范围，不影响 by-UUID resume — PoC 验证)

# 同时给新指令：
codex resume <UUIDv7> "继续做我们之前的事"
```

### 5.2 Codex → CC：制造一份 CC 能 `claude --resume` 的 jsonl

#### 步骤

1. **生成 session UUIDv4**：`uuid4()`。原 Codex UUIDv7 保留在自定义字段。
2. **决定项目目录**：从 Codex `session_meta.cwd` 算 `cc_encode_cwd(cwd)`，路径：
   ```
   ~/.claude/projects/<encoded>/<UUIDv4>.jsonl
   ```
   如果该目录不存在，`mkdir -p`。
3. **遍历 Codex RolloutItem 序列**：
   - SessionMeta → 不单独成行，把 cwd / cli_version / git 写到后续每行的字段
   - turn_context → 仅当 approval_policy/sandbox_policy 变化时写一行 `permission-mode`
   - response_item → 按内容拆成 user/assistant/tool_use/tool_result CC 行
   - event_msg → 大多丢弃（duplicate）；error 转 `system / api_error`
   - compacted → 写 compact_boundary + isCompactSummary user
4. **每行 CC 必填字段**：
   ```jsonc
   {
     "uuid": "<new uuid v4>",
     "parentUuid": "<前一行 uuid 或 null>",
     "isSidechain": false,
     "type": "user|assistant|...",
     "message": {...},
     "sessionId": "<本文件 sessionId>",
     "timestamp": "<ISO 8601 带 Z>",
     "cwd": "<从 Codex session_meta.cwd>",
     "version": "2.1.131",  // 写当前 CC 版本
     "userType": "external",
     "entrypoint": "cli",
     "gitBranch": "<可选，从 git>"
   }
   ```
5. **DAG 链**：每行 `parentUuid` 指向前一行 `uuid`。第一行 parentUuid=null。compact_boundary 行 parentUuid=null + logicalParentUuid 指向前一行。
6. **删除所有 thinking blocks**（含 signature）：
   ```python
   if block["type"] == "thinking":
       continue  # 完全跳过；不要尝试保留 text + 删 signature，下一个 turn API 会拒
   ```
   如果想保留思考摘要，转成 `text` block 加前缀 "(thinking summary)"。

#### 必填字段不全会报错

- 缺 `uuid` → 行被忽略
- 缺 `sessionId` → resume 时该行不进对话
- 缺 `cwd` → CC 列表时跳过
- `parentUuid` 不指向已存在 uuid → DAG 重建产生 orphan，CC 显示成独立 conversation
- `userType` 必须 `external`（"internal" 是 Anthropic 内部）

#### Resume 命令

```bash
claude --resume <UUIDv4>
# 或在原 cwd 下：
cd <original cwd>
claude -c       # 续最近一个
```

### 5.3 双向都要做的事

#### 副文件迁移

CC → Codex：
- CC `<sess>/tool-results/*.txt`（外置大输出）：读取，inline 到对应 `function_call_output.output`（截断到 ~10KB，因为 Codex 也有限）
- CC `<sess>/subagents/*.jsonl`：按 §3.7 处理
- CC `tasks/<sess>/*.json`：按 §3.6 转 `update_plan`
- CC `file-history/`：丢弃（Codex 没 rewind）

Codex → CC：
- Codex 子 agent jsonl（`SessionMeta.source=SubAgent`）：递归翻译为 CC 子 transcript

#### 时间戳

两边都用 ISO 8601 带毫秒带 Z。Codex 用 `T<HH>-<MM>-<SS>` 格式（连字符替代冒号）只在文件名里，jsonl 内字段都是标准格式。`Z`（UTC）必须有。

#### 行排序

写出时严格按时间戳单调递增。`uuid7` 内嵌时间已有序，但翻译时可能给同一原 turn 多个 RolloutItem，确保 timestamp 微秒区分（递增 1ms）。

### 5.4 Validation Gotchas

> **PoC 落地实例**：`data/fixture/cc-input.jsonl`（10 行真实 CC v2.1.107 session）→ `data/fixture/codex-output.jsonl`（8 行手翻 Codex jsonl）→ 复制到 `~/.codex/sessions/.../rollout-...jsonl` → `codex exec resume <UUID>` 一次成功。该 fixture 保留作为回归用例。

**头部/尾部 metadata 行可整段丢弃**（PoC 验证不影响 resume）：
- `permission-mode`、`file-history-snapshot`、`last-prompt`、`ai-title`、`custom-title`、`agent-name`
- 它们仅供 CC UI / rewind 使用，不进入对话上下文，Codex 没等价物
- §1.2 表里都标了，但实操可一次性 filter 掉

| 坑 | 触发条件 | 症状 | 修复 |
|---|---|---|---|
| CC thinking signature 失效 | 翻译保留了 thinking block 的 signature 字段 | 下一次 API 调用 400 | 删除整个 thinking block 或仅保留 text 内容 |
| Codex SessionMeta 缺失 | 翻译时漏写第一行 | `failed to parse thread ID` 错 | 必须写 session_meta 作为第一行 |
| CC encoded_cwd 冲突 | 多个 cwd 在 `[^a-zA-Z0-9]` 之外没区别 | 翻译写入到错的项目目录 | 写之前先扫现有目录的 jsonl 内 cwd 字段确认 |
| Codex UUID 非 v7 | 翻译用了 v4 UUID | SQLite 索引可能拒绝 | 强制 v7 |
| CC parentUuid orphan | 翻译时 DAG 链断开 | resume 时显示成多个独立 conversation | 严格生成线性链，第一行 null，其余指向前一行 |
| 时间戳不 sorted | 翻译多源合并时未按时间排 | 视觉上没问题，但部分 CC UI 会乱序显示 | 写出前 `sort by timestamp` |
| Codex `cwd` 不一致 | session_meta.cwd 与最新 turn_context.cwd 不同 | resume picker 用最新 turn_context.cwd | 翻译时同步两者 |
| CC 第一行 isCompactSummary | head 解析跳过 isCompactSummary 找不到 firstPrompt | session 在 picker 里显示空标题 | 第一行写真实 user prompt |

---

## 6. 身份与 metadata 映射

### 6.1 ID 字段

| 概念 | CC | Codex | 翻译规则 |
|---|---|---|---|
| Session ID | `sessionId` (UUIDv4) | `session_meta.id` (UUIDv7) | 重新铸造，原值存 metadata |
| Parent session（fork） | （CC 无显式 fork；通过 DAG 多 leaf 模拟） | `session_meta.forked_from_id` | CC→Codex 默认 None；Codex→CC 丢 |
| Tool call ID | `toolu_<22>` 或 `tooluse_<22>` | `call_<24>` | **直接复用**，不重铸（前缀不影响） |
| Message ID | `message.id` (`msg_<22>`) | （Codex 无对应 wire 字段） | CC→Codex 丢；反向时不生成 |
| Turn ID | （CC 无） | `turn_context.turn_id` (UUIDv7) | CC→Codex 每个 user turn 新生成；反向丢 |
| Promptid | `promptId` | （Codex 无） | CC→Codex 丢 |
| Agent ID | `agentId` | `session_meta.id` of subagent jsonl | 双向重铸 |

### 6.2 状态映射

#### permission_mode ↔ approval_policy + sandbox_policy

| CC permissionMode | Codex approval_policy | Codex sandbox_policy.type |
|---|---|---|
| `default` | `on-request` | `workspace-write` |
| `acceptEdits` | `on-request` | `workspace-write`（仅 edit auto） |
| `plan` | `on-request`（+ collaboration_mode=plan） | `read-only` |
| `bypassPermissions` / `dangerouslySkipPermissions` | `never` | `danger-full-access` |
| `dontAsk` | `on-request` | `workspace-write` (unverified 是否真等于 acceptEdits) |
| `auto` | `never` | `workspace-write` |

反向（Codex → CC）按上表反查；多对一时取第一项。

#### model

| CC model | Codex model | 备注 |
|---|---|---|
| `claude-opus-4-6` | `gpt-5-codex`（默认） | 跨厂商，无法 1:1 |
| `claude-opus-4-7` | `gpt-5.5` | |
| `claude-sonnet-4-6` | `gpt-5` | |
| `claude-haiku-4-5-20251001` | `o3-mini` | |

CC 模型名翻译到 Codex 时，**通常按用户配置的 default**，不强制对应。

### 6.3 git context

CC `gitBranch` ↔ Codex `session_meta.git.branch`。CC 没有 `commit_hash` `repository_url`，翻译时：
- CC→Codex：写 git ls-remote 抓的 commit hash + repo URL（best-effort，可省）
- Codex→CC：仅取 branch

### 6.4 token usage

| Codex `token_count.info.total_token_usage` | CC `assistant.message.usage` |
|---|---|
| `input_tokens` | `input_tokens` |
| `cached_input_tokens` | `cache_read_input_tokens` |
| `output_tokens` | `output_tokens` |
| `reasoning_output_tokens` | （无对应；累加进 output_tokens） |
| `total_tokens` | （CC 无字段；丢弃） |

CC 的 `cache_creation_input_tokens` 在 Codex 没对应——丢弃。

---

## 7. 硬损失清单（不可逆，必须明确告知用户）

| 损失项 | 影响 | 用户感知 |
|---|---|---|
| **CC thinking signature** | Codex→CC 反向翻译后该 turn 无法触发 extended thinking 续接 | 模型从 resume 后开始 thinking 信号会中断，但对话能继续 |
| **Codex reasoning encrypted_content** | CC→Codex→CC round-trip 后丢失内部因果链 | 模型在 Codex 里 resume 时， reasoning 的连贯性下降；通常感知不到 |
| **CC subagent transcript（多文件 → 单文件）** | 选 inline 策略后 token 膨胀；选 split 策略后子文件成为独立 Codex session | inline 时上下文成本高；split 时 picker 多出条目 |
| **Codex `compaction.encrypted_content`** | 跨 harness 翻译丢，原始历史不可回收 | resume 后模型只看到摘要，不能"展开"被压缩的历史 |
| **CC TaskCreate DAG (blocks/blockedBy)** | Codex update_plan 是平铺数组 | 任务依赖关系丢失，模型重新决策顺序 |
| **CC `attachment / nested_memory` 多层** | 全部塞进 developer message 一团 | 模型读到所有 memory 但失去"哪条来自哪个 CLAUDE.md" |
| **CC plan mode `filePath`** | Codex 没有对应 plans/ 目录 | 计划文件在 Codex 端无法 rewind 编辑 |
| **CC file-history snapshots** | Codex 无 rewind | `/rewind-files` 在 Codex 端不可用，但 git 在 |
| **Codex `image_generation_call` 输出** | CC 无内建 image generation | Codex→CC 后仅保留 prompt 文字，生成的图丢 |
| **Codex `code_mode` 工具组合** | CC 没等价"多工具单次执行" | 反向翻译时拆成多次独立 tool_use，时间戳分裂 |
| **CC `Skill` 的动态 listing** | Codex skills 是文件级 | Codex 端模型只看到当时翻译时的 skills 快照 |
| **Codex `personality` / `collaboration_mode.settings.developer_instructions`** | CC 没等价深度配置 | CC→Codex→CC 反向时这些细节丢 |
| **CC `caller` field** | Codex 无对应 metadata | 标记主 agent vs subagent 调用的 hint 丢失（不影响行为） |
| **CC `cache_control` 标记** | Codex 用不同 cache 机制 | prompt cache 命中率短期下降 |

**用户感知排序**（最痛 → 最不痛）：
1. Subagent 翻译（token 成本 / 文件分裂）— **明显**
2. thinking 链断裂 — **中等**
3. update_plan 简化 — **中等**
4. file-history 丢失 — **轻微**
5. 其他 metadata — **几乎无感**

---

## 8. 三档 fidelity 模式

实现时不要试图做"完美翻译"。提供三档模式让用户选：

### Mode A — Faithful Replay（高保真）

目标：把整个 session 历史完整回放到目标 harness。

策略：
- 所有 user/assistant/tool_use/tool_result 全保留
- subagent 用策略 B（分文件）
- attachments 全部翻译成 developer message
- thinking 全部丢弃但保留 text

成本：token 很高（CC subagent 多时尤甚）。

适用：用户想"完整接着 session 干"，关心历史细节。

### Mode B — Summary Handoff（轻量交接）

目标：把过去对话压成一个紧凑 handoff 文档 + 最近 N 轮原文。

策略：
- 历史所有 turn 走一次 LLM summary（外部模型，比如 Haiku/gpt-5-mini）
- 最近 N 轮（默认 5）原文保留
- subagent 仅保留主结果，不 inline 子 transcript
- 输出一段 markdown：`<handoff>\n## 已完成 ...\n## 未完成 ...\n## 当前焦点 ...\n</handoff>`

成本：低 token，需多调一次 LLM 做摘要。

适用：用户想换个 agent 用全新视角，但要带上做过的事。

### Mode C — Hybrid（混合）

目标：上下文窗口 80% 给最近 N 轮原文，20% 给摘要。

策略：
- 历史中超过 N 轮的部分按 Mode B summary
- 最近 N 轮按 Mode A faithful
- subagent 按"重要性"打分（subagent transcript 长度 + 是否被主线引用），重要的 inline，其他 summary

成本：中等。

适用：默认推荐。N 可配置（默认 10）。

### 各模式触发字段

CLI:
```
agent-resume export --from claude-code --session <id> --mode A|B|C \
                    --keep-last <N> --to codex
```

Mode 选择影响：
- 要不要外部 LLM 调用（Mode B/C 需要）
- 输出文件大小
- 是否需要 fixture 数据（Mode A 高保真依赖 file content snapshot 重建 patch hunks）

---

## 9. Open Questions（需要实现期解决）

按优先级排序：

### P0 — 阻塞实现

1. **Codex `EventPersistenceMode::Extended` 的默认值**：本机 0.128.0 TUI 写出了 `exec_command_end`（Limited 模式不写），说明实际默认是 Extended。**翻译器写 Codex 文件时该写多少 event_msg？** 需要看 `codex-rs/core/src/codex_thread.rs` 决定。**临时方案**：写 Limited 集合 + `exec_command_end`，与本机观察一致。
2. **CC 手写 jsonl 在 v2.1.131 后是否仍可被 resume**：研究文档说"理论上可以"，本机未测试。**P0 必须有 round-trip 测试**：手造一份最小 CC jsonl → `claude --resume <UUID>` → 看是否进入对话。
3. **Codex resume 对历史中"该 session 注册过但当前未注册的工具"的容忍度**：codex §8.5 说不校验，但实测过没？**P0 测试**：翻译一份 CC session（含 Skill / WebSearch）到 Codex → resume → 模型行为是否合理。

### P1 — 影响损耗清单完整性

4. **CC `compactMetadata.preservedSegment` 实际形态**：trigger=manual + preserve text 时这个字段什么样。
5. **CC `attribution-snapshot` 行用途**：v2.1.131 未观察到，但 [bin] 检测它。
6. **Codex `apply_patch Add File` 的 mkdir 行为**：是否递归。本机 实测可，但源码未读到 `create_dir_all` 调用点。
7. **Codex `compaction_summary` 老别名**：源码 `#[serde(alias = "compaction_summary")]`，老版会话需要识别。
8. **Codex MCP function_call 的 `namespace` 字段实际 wire 格式**：源码声称有，但本机会话没启用 MCP，无样本。

### P2 — 优化质量

9. **CC `attachment / file` 大文件截断阈值**：什么时候 attachment 会切到外置？
10. **Codex `disable_response_storage = true` 与本地 jsonl 的关系**：本机配置开了它，jsonl 仍带 `encrypted_content`，说明 flag 仅影响 OpenAI server 端。
11. **跨 file CC session（v2.0 时代的多 file 拷贝行为）**：v2.1 是否完全废弃？v2.1.131 实测无。
12. **Codex `local_shell_call` 的触发模型**：本机未观察，需测 o3 / gpt-4.1。

---

## 附录 A — 字段级速查

为实现期 copy-paste 准备的最小映射表（非完整）：

| 概念 | CC 字段路径 | Codex 字段路径 |
|---|---|---|
| Session ID | `<line>.sessionId` | `payload.id` (在 session_meta 行) |
| Working dir | `<line>.cwd` | `payload.cwd` (在 session_meta + turn_context) |
| Timestamp | `<line>.timestamp` | `<line>.timestamp` |
| Model | `assistant.message.model` | `payload.model` (turn_context) |
| User text | `user.message.content` (str) 或 `user.message.content[].text` (text block) | `payload.content[].text` (input_text) |
| Assistant text | `assistant.message.content[].text` (text block) | `payload.content[].text` (output_text) |
| Tool name | `assistant.message.content[].name` | `payload.name` |
| Tool input | `assistant.message.content[].input` | `payload.arguments` (stringified JSON) 或 `payload.input` (custom_tool_call raw) |
| Tool result | `user.message.content[].content` (在 tool_result block) | `payload.output` (function_call_output 或 custom_tool_call_output) |
| Tool call ID | `assistant.message.content[].id` (`toolu_X`) / `user.message.content[].tool_use_id` | `payload.call_id` |
| Token in | `assistant.message.usage.input_tokens` | `payload.info.total_token_usage.input_tokens` (token_count event) |
| Token out | `assistant.message.usage.output_tokens` | `payload.info.total_token_usage.output_tokens` |
| Cache read | `assistant.message.usage.cache_read_input_tokens` | `payload.info.total_token_usage.cached_input_tokens` |

---

## 附录 B — 翻译器最小输出实例

把 CC 一段 3 turn 简单对话翻译到 Codex 的最小 jsonl（仅展示关键字段）：

CC 输入：
```jsonl
{"uuid":"u1","parentUuid":null,"type":"user","message":{"role":"user","content":"What is 2+2?"},"sessionId":"cc-sess","timestamp":"2026-05-06T09:00:00.000Z","cwd":"/Users/alice","version":"2.1.131","userType":"external","entrypoint":"cli","gitBranch":"HEAD"}
{"uuid":"u2","parentUuid":"u1","type":"assistant","message":{"id":"msg_a","model":"claude-opus-4-6","role":"assistant","content":[{"type":"text","text":"4"}],"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"sessionId":"cc-sess","timestamp":"2026-05-06T09:00:01.000Z",...}
{"uuid":"u3","parentUuid":"u2","type":"user","message":{"role":"user","content":"And 3+3?"},"sessionId":"cc-sess","timestamp":"2026-05-06T09:00:02.000Z",...}
```

Codex 输出（保存为 `~/.codex/sessions/2026/05/06/rollout-2026-05-06T09-00-00-019df900-...jsonl`）：
```jsonl
{"timestamp":"2026-05-06T09:00:00.000Z","type":"session_meta","payload":{"id":"019df900-0000-7000-9000-000000000001","timestamp":"2026-05-06T09:00:00.000Z","cwd":"/Users/alice","originator":"agent-bridge","cli_version":"0.1.0","source":{"custom":"agent-bridge"}}}
{"timestamp":"2026-05-06T09:00:00.001Z","type":"turn_context","payload":{"turn_id":"019df900-0000-7000-9000-000000000002","cwd":"/Users/alice","current_date":"2026-05-06","timezone":"Asia/Shanghai","approval_policy":"on-request","sandbox_policy":{"type":"workspace-write"},"model":"gpt-5","summary":"none"}}
{"timestamp":"2026-05-06T09:00:00.002Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"What is 2+2?"}]}}
{"timestamp":"2026-05-06T09:00:01.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"4"}],"phase":"final_answer"}}
{"timestamp":"2026-05-06T09:00:02.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"And 3+3?"}]}}
```

resume：
```bash
codex resume --include-non-interactive 019df900-0000-7000-9000-000000000001
```

---

> 本文档应当伴随翻译器实现一起演进。每解决一个 P0/P1 open question，更新对应章节并在 changelog 标注。**修改前必须读完两份调研文档（claude-code-harness.md / codex-harness.md）以确保不破坏证据链。**



