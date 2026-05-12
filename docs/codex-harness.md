# Codex CLI Harness：会话格式与翻译参考

> 目标：把 Claude Code 与 OpenAI Codex CLI 之间的会话 jsonl 互相翻译。本文档基于 `openai/codex` 仓库 main 分支（撰写时最新提交 `06e5dfa`）以及本机 `~/.codex/sessions/` 真实磁盘数据双向印证。
>
> 调研环境：
> - 本机已安装 `codex-cli 0.128.0`（`/opt/homebrew/bin/codex`），目录 `~/.codex/` 完整可用，包含 9 份真实 session jsonl（2026-04-29 ~ 2026-05-05）。
> - 源码无法 `git clone`（沙盒到 github.com 的直连受限），改用 CDP 浏览器拉取 `raw.githubusercontent.com` 文件。本文中的源码引用形如 `codex-rs/<crate>/<path>:<line>` 指向 `github.com/openai/codex/blob/main/...`。
> - 凡未经源码或磁盘印证的结论会显式标注 `unverified`。

文档目录与任务大纲一一对应：

1. Codex CLI 概览
2. 会话存储（路径、命名、SQLite、index）
3. Conversation/message schema（RolloutLine / RolloutItem）
4. 内置工具清单（schema 摘录）
5. apply_patch 详解
6. shell 系工具详解
7. 系统 prompt、AGENTS.md、环境
8. resume 与 fork 机制
9. MCP 与扩展性
10. Reasoning 内容的存储
11. CC → Codex 的有损映射
12. Codex → CC 的有损映射
13. 未确认问题

附录 A 给出**字段级翻译表**，附录 B 给出**最小可 resume 的 jsonl 模板**。

---

## 1. Codex CLI 概览

### 1.1 是什么、与 Claude Code 的差异

Codex CLI（仓库 `openai/codex`）是 OpenAI 官方维护的开源终端编码 agent。注意：**本文档所说的 Codex 仅指这个 CLI/TUI 工具**，与 2021 年的 Codex API 模型已无任何代码关系。它发布频率非常高（npm `@openai/codex`、Homebrew `--cask codex`），每日多个 commit；本文锚定到撰写时的 `main` 分支提交。

| 维度 | Codex CLI | Claude Code |
| --- | --- | --- |
| 实现语言 | Rust 为主（`codex-rs/` workspace，~80 个 crate），少量 TypeScript（`codex-cli/`、`sdk/`） | Node.js / TypeScript |
| 后端模型 | OpenAI Responses API（`wire_api = "responses"`）。也支持 Chat Completions、本地 Ollama / LM Studio | Anthropic Messages API |
| 默认模型 | `gpt-5-codex`（codex 专用变体），其他常见值 `gpt-5`, `gpt-5.5`, `o3`，参考 `codex-rs/protocol/src/openai_models.rs` 的 `ReasoningEffort` 枚举 | `claude-*` 系列 |
| 界面层 | TUI（`codex-rs/tui/`）、`exec`（非交互）、`mcp-server`（把自身暴露为 MCP）、`app-server`（GUI 后端）、Cloud Tasks 等 | TUI、SDK |
| 工具集风格 | 极简、**shell 中心**：`shell` / `exec_command` 为主，文件编辑统一走 `apply_patch` 自由格式 | 多工具：Read/Write/Edit/Bash/Grep/Glob/AskUserQuestion/Subagent/Skill/Plan… |
| 编辑模型 | `apply_patch` 自由格式 grammar（Lark 语法或 JSON 字符串） | Edit/Write/MultiEdit + 内部 string-replace |
| 子 agent | 一等公民：`spawn_agent`、`send_input`、`wait_agent`、`close_agent`、`list_agents`，会话间通过 `agent_role` / `agent_nickname` 关联 | Task tool（Agent） |
| 计划工具 | `update_plan`（轻量 todo 列表，`StepStatus = pending\|in_progress\|completed`） | Plan mode + ExitPlanMode + TaskCreate/Update |
| 思考链 | Responses API `reasoning` 项，加密内容 `encrypted_content` 持久化在会话里；可见摘要为 `summary[]` | thinking（明文） |
| 沙箱 / 审批 | 三档 `sandbox_policy`（`read-only` / `workspace-write` / `danger-full-access`） + 五档 `approval_policy`（`untrusted` / `on-failure`(deprecated) / `on-request` / `granular` / `never`） | `permissions` allow/deny + `permissionMode` |
| Skills | 一等公民：`$CODEX_HOME/skills/`，`SKILL.md` 列表注入 developer message | 一等公民：Skill 工具 + `~/.claude/skills/` |

**关键差异（影响翻译）**：

- Codex 走 **Responses API** 协议，工具调用是 `function_call` + `function_call_output`，外加 `custom_tool_call` / `web_search_call` / `image_generation_call`。CC 走 **Messages API**，工具调用嵌在 `tool_use`/`tool_result` content blocks 里。
- Codex 把**编辑器抽象只暴露给模型一份**：`apply_patch`。CC 的 Edit/Write/MultiEdit 都必须翻成一份 `*** Begin Patch ... *** End Patch`。
- Codex 把会话每个事件作为 jsonl 一行。CC 的 `.jsonl` 有更多 wrapper（`type=tool_use_v1` 等），但本质类似。
- Codex 持久化 reasoning 的 `encrypted_content`（不可解码、来自后端），resume 时这些会被原样喂回 Responses API；在跨 harness 翻译时这是有损点。

### 1.2 模型选择

Codex 支持的模型枚举见：
- `codex-rs/protocol/src/openai_models.rs:43` — `ReasoningEffort` 枚举：`None`, `Minimal`, `Low`, `Medium`(默认), `High`, `XHigh`。
- `~/.codex/config.toml` 可见于本机：`model = "gpt-5.5"`、`model_reasoning_effort = "xhigh"`、`model_provider = "openai"`。
- 模型迁移由 `[notice.model_migrations]` 配置（如 `"gpt-5.3-codex" = "gpt-5.4"`，强制升级提示）。
- 命令行覆写：`-m, --model <MODEL>` 或 `-c model=...`。

### 1.3 配置文件

主配置位于 `$CODEX_HOME/config.toml`，`CODEX_HOME` 默认 `~/.codex`。可覆写：

```toml
model_provider = "openai"
model = "gpt-5.5"
model_reasoning_effort = "xhigh"
disable_response_storage = true
preferred_auth_method = "apikey"
notify = ["sh", "-c", "afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1"]

[model_providers.openai]
name = "openai"
base_url = "https://api.openai.com/v1"
wire_api = "responses"

[notice.model_migrations]
"gpt-5.3-codex" = "gpt-5.4"

[projects."/Users/alice"]
trust_level = "trusted"
```

CLI flag `-c <key=value>` 允许 dotted-path 覆写任意 TOML 键（见 `codex --help`）。所有配置都向命令的 `RolloutConfigView`/`Config` 暴露，但**仅有限子集会写入会话**（参见 §3.4 SessionMeta）。

---

## 2. 会话存储

### 2.1 路径与命名

源码 `codex-rs/rollout/src/lib.rs:18`：

```rust
pub const SESSIONS_SUBDIR: &str = "sessions";
pub const ARCHIVED_SESSIONS_SUBDIR: &str = "archived_sessions";
```

文件名生成在 `codex-rs/rollout/src/recorder.rs:1380-1394`：

```
$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<YYYY>-<MM>-<DD>T<hh>-<mm>-<ss>-<uuid>.jsonl
```

字段全部使用 UTC、`-` 替代 `:`（兼容旧文件系统），UUID 是 v7（see `ThreadId::new` in `codex-rs/protocol/src/thread_id.rs:18`，`Uuid::now_v7()`）。本机示例：

```
~/.codex/sessions/2026/05/05/rollout-2026-05-05T18-28-34-019df7ae-b036-76c2-a499-fe10385d955a.jsonl
```

* 存档目录：`$CODEX_HOME/archived_sessions/`（同样的 `YYYY/MM/DD/<file>` 结构，由 `--archive`/UI 触发）。
* **没有按 cwd 分目录**——所有项目共享同一个时间型目录树。Resume picker 通过读取每个 jsonl 的 `session_meta.cwd` 与 `turn_context.cwd`（最新优先）来过滤：源码 `codex-rs/tui/src/session_resume.rs:144-189`、recorder `cwd_filters` 参数。这一点与 Claude Code 的 `~/.claude/projects/<encoded-cwd>/` 不同。

### 2.2 文件格式

**JSONL，UTF-8，LF 结尾**。每行是一个 `RolloutLine`（`codex-rs/protocol/src/protocol.rs:2945-2950`）：

```rust
#[derive(Serialize, Deserialize, Clone, JsonSchema)]
pub struct RolloutLine {
    pub timestamp: String,            // ISO-8601 UTC，毫秒，带 Z 后缀
    #[serde(flatten)]
    pub item: RolloutItem,
}
```

`#[serde(flatten)]` 意味着 `type` / `payload` 两个字段是 `RolloutItem` 自己 emit 的（见 §3.1）。最终一行 JSON 形如：

```json
{
  "timestamp": "2026-05-05T10:32:18.680Z",
  "type": "event_msg",
  "payload": { "type": "task_started", "turn_id": "...", ... }
}
```

写入路径：`codex-rs/rollout/src/recorder.rs:1782-1814` 的 `JsonlWriter::write_rollout_item` —— 单行 JSON、追加换行、`flush().await`。

写入由后台 Tokio task 串行化（`RolloutCmd::AddItems / Persist / Flush / Shutdown`），因此**单进程内不会有并发覆写**。

**文件可追加**：resume 时打开 `OpenOptions::append(true)` 继续向同一个文件写新行（`codex-rs/rollout/src/recorder.rs:1770-1779` 的 `append_rollout_item_to_path`）。这与 CC 行为一致。

### 2.3 同时存在的伴生文件

`~/.codex/` 实际目录（本机 ground truth）：

```
~/.codex/
├── auth.json               # Login token / api key 元数据
├── config.toml             # 用户配置
├── installation_id         # 安装 ID
├── version.json            # 自更新检查器最新版本
├── history.jsonl           # ===> 用户输入历史，用于 ↑ 上箭头召回
├── session_index.jsonl     # ===> 见 §2.4
├── logs_2.sqlite (+ -shm/-wal) # 会话索引/缓存的 SQLite，由 codex_state crate 管理
├── state_5.sqlite          # 其他状态（plugin, marketplace 等）
├── sessions/2026/...       # ===> 真正的 rollout jsonl
├── archived_sessions/...
├── shell_snapshots/        # 启动时的 shell env/PATH 快照（exec 工具用）
├── memories/               # 跨 session 持久化的记忆
├── skills/                 # 用户自定义 skill（SKILL.md + scripts/）
├── tmp/, log/, .tmp/       # 运行时缓存
└── .personality_migration  # 一次性升级标志
```

### 2.4 会话索引：`session_index.jsonl`

源码 `codex-rs/rollout/src/session_index.rs:20-26`：

```rust
pub struct SessionIndexEntry {
    pub id: ThreadId,
    pub thread_name: String,
    pub updated_at: String,  // RFC3339
}
```

仅在用户/agent 给会话**起名**（`thread_name_updated` 事件）时追加一行。**append-only**，最新覆盖旧名，反向扫描定位（`scan_index_from_end`）。本机内容示例：

```json
{"id":"019df7ae-b036-76c2-a499-fe10385d955a","thread_name":"26M5 解决方案讨论","updated_at":"2026-05-05T10:29:15.509363Z"}
```

`codex resume` / `codex fork` 接受 `<SESSION_ID>` 参数：**优先 UUID，否则按 thread_name 逆向匹配**。

### 2.5 SQLite 索引（`logs_2.sqlite`、`state_5.sqlite`）

`codex-rs/rollout/src/recorder.rs:235-583` 实现 `list_threads_with_db_fallback`：先扫文件系统得到 candidate，再用 SQLite (`codex_state`) 修复元数据缓存（`reconcile_rollout`）。如果 SQLite 缺失或失败，回退到纯文件扫描（`page_from_filesystem_scan`）。

**对翻译器的含义**：你只需要写好 jsonl，下一次 `codex resume --all`（无 cwd 过滤）会扫到它并写入 SQLite。**不要尝试自己写 SQLite**——schema 没有公开稳定。

### 2.6 生命周期

**创建**：`RolloutRecorder::new(RolloutRecorderParams::Create { ... })` 计算文件路径但不立即创建（`deferred_log_file_info`）。第一次 `record_items`/`flush` 时才 `mkdir -p` + open。

**首行写入**：`SessionMetaLine`（包含 `SessionMeta` + `git`），随后立刻写第一个 `turn_context`。见 `codex-rs/rollout/src/recorder.rs:1700`：

```rust
let rollout_item = RolloutItem::SessionMeta(session_meta_line);
```

**追加**：每个 `RolloutItem` 单独成行，过滤策略由 `EventPersistenceMode` 控制（`codex-rs/rollout/src/policy.rs`，详见 §3.5）。

**结束**：没有显式 "finalize"。`shutdown()` 走完后台队列即可。文件上**没有 EOF 标记**——靠 `RolloutLine` 解析失败 = 文件结尾。

**Resume 时**：`load_rollout_items` 把所有行 deserialize 回 `RolloutItem` Vec，构造 `InitialHistory::Resumed { conversation_id, history, rollout_path }`，然后 recorder 以 `Resume { path }` 模式把同一个文件**继续追加**（不复制、不创建新文件）。源码 `codex-rs/rollout/src/recorder.rs:862-946`。

> 也就是说：如果你要"在 Codex 中 resume 一个翻译过来的 CC 会话"，你必须先把 jsonl 放到 `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-...jsonl`，然后 `codex resume <UUID>`。Codex 自己会附加新的事件到这个文件。

---

## 3. Conversation/message schema

### 3.1 顶层：`RolloutItem` 五种 variant

`codex-rs/protocol/src/protocol.rs:2795-2803`：

```rust
#[derive(Serialize, Deserialize, Debug, Clone, JsonSchema, TS)]
#[serde(tag = "type", content = "payload", rename_all = "snake_case")]
pub enum RolloutItem {
    SessionMeta(SessionMetaLine),        // type:"session_meta"
    ResponseItem(ResponseItem),          // type:"response_item"
    Compacted(CompactedItem),            // type:"compacted"
    TurnContext(TurnContextItem),        // type:"turn_context"
    EventMsg(EventMsg),                  // type:"event_msg"
}
```

由于 `tag = "type"` + `content = "payload"`，每行 JSON 形如 `{"type": "<variant>", "payload": {...}}`，再加 `RolloutLine` 的 `timestamp`，最终就是：

```json
{"timestamp": "...", "type": "...", "payload": {...}}
```

实测本机会话出现的 `type` 集合（来自第二份 1377 行的 session）：

* `session_meta`（恰好 1 条）
* `turn_context`（每个用户 turn 一条）
* `response_item`（绝大多数行）
* `event_msg`（生命周期事件）
* `compacted`（mid-turn 压缩时一条）

### 3.2 `SessionMeta` / `SessionMetaLine`

`codex-rs/protocol/src/protocol.rs:2732-2793`：

```rust
pub struct SessionMeta {
    pub id: ThreadId,                            // UUID v7
    pub forked_from_id: Option<ThreadId>,
    pub timestamp: String,                       // ISO-8601 UTC
    pub cwd: PathBuf,
    pub originator: String,                      // 例: "codex-tui", "codex-exec"
    pub cli_version: String,                     // 例: "0.128.0"
    #[serde(default)] pub source: SessionSource, // §3.3
    pub thread_source: Option<ThreadSource>,
    pub agent_nickname: Option<String>,          // sub-agent 才有
    pub agent_role: Option<String>,
    pub agent_path: Option<String>,
    pub model_provider: Option<String>,
    pub base_instructions: Option<BaseInstructions>,
    pub dynamic_tools: Option<Vec<DynamicToolSpec>>,
    pub memory_mode: Option<String>,
}

pub struct SessionMetaLine {
    #[serde(flatten)] pub meta: SessionMeta,
    pub git: Option<GitInfo>, // commit_hash / branch / repository_url
}
```

**实测样例**（`del(.base_instructions)`）：

```json
{
  "id": "019dd8e4-15dd-7143-95e8-219014341717",
  "timestamp": "2026-04-29T10:58:39.732Z",
  "cwd": "/Users/alice",
  "originator": "codex-tui",
  "cli_version": "0.125.0",
  "source": "cli",
  "model_provider": "openai"
}
```

`base_instructions.text` 在本机会话里高达 ~21KB（见 §7）。`dynamic_tools: 0` 表示当时没有 MCP 注入工具。

### 3.3 `SessionSource` 与 `ThreadSource`

`codex-rs/protocol/src/protocol.rs:2531-2542`：

```rust
#[derive(...)] #[serde(rename_all = "lowercase")]
pub enum SessionSource {
    Cli,
    #[default] VSCode,
    Exec,
    Mcp,
    Custom(String),                  // 例如 "atlas", "chatgpt"
    Internal(InternalSessionSource),
    SubAgent(SubAgentSource),
    #[serde(other)] Unknown,
}
```

```rust
pub enum ThreadSource { User, Subagent, MemoryConsolidation }
```

**翻译器层面要保留**：`source` 标志会决定 resume picker 是否显示该会话（`INTERACTIVE_SESSION_SOURCES` 默认包含 `Cli`, `VSCode`, `Custom("atlas")`, `Custom("chatgpt")`）。CC 会话翻译时建议用 `Custom("claude-code")` 或 `Custom("agent-bridge")`，并配合 `--include-non-interactive` flag 让其在 picker 中可见。

### 3.4 `TurnContextItem`（每个用户 turn 起始时写一条）

`codex-rs/protocol/src/protocol.rs:2836-2872`：

```rust
pub struct TurnContextItem {
    pub turn_id: Option<String>,
    pub trace_id: Option<String>,
    pub cwd: PathBuf,
    pub current_date: Option<String>,
    pub timezone: Option<String>,
    pub approval_policy: AskForApproval,
    pub sandbox_policy: SandboxPolicy,
    pub permission_profile: Option<PermissionProfile>,
    pub network: Option<TurnContextNetworkItem>,
    pub file_system_sandbox_policy: Option<FileSystemSandboxPolicy>,
    pub model: String,
    pub personality: Option<Personality>,
    pub collaboration_mode: Option<CollaborationMode>,
    pub realtime_active: Option<bool>,
    pub effort: Option<ReasoningEffortConfig>,
    pub summary: ReasoningSummaryConfig,
    pub user_instructions: Option<String>,
    pub developer_instructions: Option<String>,
    pub final_output_json_schema: Option<Value>,
    pub truncation_policy: Option<TruncationPolicy>,
}
```

实测样例（节选）：

```json
{
  "turn_id":"019df7b2-1df3-7e01-b2f9-f9283453cd1c",
  "cwd":"/Users/alice",
  "current_date":"2026-05-05",
  "timezone":"Asia/Shanghai",
  "approval_policy":"never",
  "sandbox_policy":{"type":"danger-full-access"},
  "permission_profile":{"type":"disabled"},
  "model":"gpt-5.5",
  "personality":"pragmatic",
  "collaboration_mode":{
    "mode":"default",
    "settings":{
      "model":"gpt-5.5",
      "reasoning_effort":"xhigh",
      "developer_instructions":"# Collaboration Mode: Default\n..."
    }
  },
  "realtime_active":false,
  "effort":"xhigh",
  "summary":"none",
  "truncation_policy":{"mode":"tokens","limit":10000}
}
```

> **resume 时 read_session_cwd 只读 `turn_context.cwd`**，最后一次写入的赢（`codex-rs/tui/src/session_resume.rs:148-189`）。所以翻译时**至少要在文件头部 `session_meta` 之后写一条 turn_context**，否则 picker 拿不到 cwd 会用进程 cwd。

#### 3.4.1 `AskForApproval` 与 `SandboxPolicy`

`codex-rs/protocol/src/protocol.rs:913-944, 1007+`：

```rust
pub enum AskForApproval {
    #[serde(rename = "untrusted")] UnlessTrusted,
    OnFailure,                // DEPRECATED
    #[default] OnRequest,
    #[strum(serialize = "granular")] Granular(GranularApprovalConfig),
    Never,
}

#[serde(tag = "type", rename_all = "kebab-case")]
pub enum SandboxPolicy {
    #[serde(rename = "danger-full-access")] DangerFullAccess,
    #[serde(rename = "read-only")] ReadOnly { network_access: bool },
    #[serde(rename = "external-sandbox")] ExternalSandbox { network_access: NetworkAccess },
    // workspace-write 等更多变体见源码
}
```

序列化形如 `"approval_policy": "never"`，`"sandbox_policy": {"type":"danger-full-access"}`。

### 3.5 `EventMsg` —— 哪些事件被持久化？

`EventMsg` 枚举定义于 `codex-rs/protocol/src/protocol.rs:1286-1530`，约 **70 种事件**。但**并非所有事件都写入 jsonl**。过滤规则定义在 `codex-rs/rollout/src/policy.rs:93-181`：

| 持久化模式 | 写入条件 |
| --- | --- |
| `Limited`（默认） | 列在 `event_msg_persistence_mode` 返回 `Some(Limited)` 的事件 |
| `Extended` | `Limited` ∪ 返回 `Some(Extended)` 的事件 |

**Limited（默认会写入会话）**：

```
UserMessage, AgentMessage, AgentReasoning, AgentReasoningRawContent,
PatchApplyEnd, TokenCount, ContextCompacted,
EnteredReviewMode, ExitedReviewMode,
McpToolCallEnd, ThreadRolledBack, TurnAborted,
TurnStarted (task_started), TurnComplete (task_complete),
WebSearchEnd, ImageGenerationEnd,
ItemCompleted (仅当 item 是 TurnItem::Plan)
```

**Extended（仅在用户启用扩展持久化时写入）**：

```
Error, GuardianAssessment, ExecCommandEnd, ViewImageToolCall,
CollabAgentSpawnEnd, CollabAgentInteractionEnd, CollabWaitingEnd,
CollabCloseEnd, CollabResumeEnd,
DynamicToolCallRequest, DynamicToolCallResponse
```

**永不持久化（只是 UI 中转事件，不上磁盘）**：所有 `*Begin` / `*Delta` / `Streaming` 事件、`SessionConfigured`、`StreamError`、`ApplyPatchApprovalRequest`、`PlanUpdate`（结构化 plan 事件），等等。

实测本机会话观察到的 `event_msg.payload.type` 集合：

```
agent_message, exec_command_end, task_complete, task_started,
token_count, turn_aborted, user_message,
context_compacted, error, patch_apply_end, thread_name_updated,
thread_rolled_back, view_image_tool_call, web_search_end
```

**注意**：`exec_command_end` 在 `Limited` 模式里是 `None`，但在我的本机会话里出现了。这意味着我的 codex 用了 `Extended`（很可能因为 `codex-tui` 默认就是 Extended，而 `exec`/MCP 是 Limited）。源码上 `EventPersistenceMode::default()` 返回 `Limited`，但具体调用站点见 `codex-rs/core/src/codex_thread.rs`（`unverified` 这一具体策略选择，但事实上 TUI 写出了它们）。

### 3.6 `ResponseItem` —— 与模型对话的内容

`codex-rs/protocol/src/models.rs:741-891`，`#[serde(tag = "type", rename_all = "snake_case")]`：

| variant | `payload.type` | 关键字段 |
| --- | --- | --- |
| `Message` | `message` | `role` (`"user"`/`"assistant"`/`"developer"`), `content: Vec<ContentItem>`, `phase` (`"commentary"`/`"final_answer"`/null) |
| `Reasoning` | `reasoning` | `summary: Vec<ReasoningItemReasoningSummary>`, `content: Option<Vec<ReasoningItemContent>>`, `encrypted_content: Option<String>` |
| `LocalShellCall` | `local_shell_call` | `call_id`, `status`, `action: LocalShellAction` |
| `FunctionCall` | `function_call` | `name`, `namespace?`, `arguments: String` (JSON-as-text), `call_id` |
| `ToolSearchCall` | `tool_search_call` | `call_id?`, `status?`, `execution: String`, `arguments: Value` |
| `FunctionCallOutput` | `function_call_output` | `call_id`, `output: FunctionCallOutputBody` (text-or-content_items) |
| `CustomToolCall` | `custom_tool_call` | `call_id`, `name`, `input: String` (raw, **不是 JSON**), `status?` |
| `CustomToolCallOutput` | `custom_tool_call_output` | `call_id`, `name?`, `output: FunctionCallOutputBody` |
| `ToolSearchOutput` | `tool_search_output` | `call_id?`, `status`, `execution`, `tools: Vec<Value>` |
| `WebSearchCall` | `web_search_call` | `status?`, `action: Option<WebSearchAction>` |
| `ImageGenerationCall` | `image_generation_call` | `id`, `status`, `revised_prompt?`, `result` |
| `Compaction` | `compaction` (alias `compaction_summary`) | `encrypted_content: String` |
| `ContextCompaction` | `context_compaction` | `encrypted_content?` |
| `Other` | (任何未识别 type) | — |

所有 variant 都共享一个隐藏 `id` 字段（`#[serde(skip_serializing)]`），所以**写入磁盘时不会出现 `id`**——它只用于 in-memory tagging。

#### 3.6.1 `ContentItem`（消息正文 block）

`codex-rs/protocol/src/models.rs:697-712`：

```rust
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ContentItem {
    InputText  { text: String },
    InputImage { image_url: String, detail: Option<ImageDetail> },
    OutputText { text: String },
}
```

- `InputText` 用于 `role: "user"` 或 `"developer"` 的内容。
- `OutputText` 用于 `role: "assistant"` 输出。
- `InputImage` 仅用于用户/developer 的图片 attachment（data URL 或 https URL）。
- `ImageDetail`：`auto`/`low`/`high`/`original`，默认 `high`。

`MessagePhase`（`models.rs:725-739`）只用于 assistant 消息：`commentary`（中途叙述）/ `final_answer`（终态答复）。`role` 是字符串而非枚举，常见值 `"user"`、`"assistant"`、`"developer"`、`"system"`（**Codex 用 `developer` 表达 system prompt 增量；`system` 仅用于 base_instructions**）。

#### 3.6.2 实例 — user/assistant message

第一行用户消息：
```json
{"timestamp":"2026-04-29T10:58:42.786Z","type":"response_item","payload":{
  "type":"message","role":"user",
  "content":[{"type":"input_text","text":"yooo"}]
}}
```

Assistant final answer：
```json
{"timestamp":"2026-04-29T10:58:45.067Z","type":"response_item","payload":{
  "type":"message","role":"assistant",
  "content":[{"type":"output_text","text":"Yooo. What are we working on?"}],
  "phase":"final_answer"
}}
```

#### 3.6.3 实例 — function_call / function_call_output (`exec_command`)

```json
{"timestamp":"...","type":"response_item","payload":{
  "type":"function_call",
  "name":"exec_command",
  "arguments":"{\"cmd\": \"sed -n '1,220p' /Users/alice/.codex/skills/web-access/SKILL.md\", \"workdir\": \"/Users/alice\", \"yield_time_ms\": 1000, \"max_output_tokens\": 12000}",
  "call_id":"call_ahWfyuXHFSa3HIH5LZqHkv5B"
}}
```

注意 `arguments` 是**字符串化的 JSON**——Responses API 规范，必须保持。

```json
{"timestamp":"...","type":"response_item","payload":{
  "type":"function_call_output",
  "call_id":"call_ahWfyuXHFSa3HIH5LZqHkv5B",
  "output":"Chunk ID: ef0014\nWall time: 0.0000 seconds\nProcess exited with code 0\nOriginal token count: 3713\nOutput:\n..."
}}
```

`output` 在 wire 上可以是 string 也可以是 array（`FunctionCallOutputBody::Text` vs `ContentItems`，`models.rs:1384-1389`）。serde 自动 untagged 选择。

#### 3.6.4 实例 — custom_tool_call (`apply_patch`)

`apply_patch` 在 GPT-5 系模型上以**自由格式**注册（`ToolSpec::Freeform` + Lark grammar，见 §5），所以 wire 上落在 `custom_tool_call` 而非 `function_call`：

```json
{"timestamp":"2026-05-05T12:41:27.397Z","type":"response_item","payload":{
  "type":"custom_tool_call",
  "status":"completed",
  "call_id":"call_xC0cwhUrkwAls4ADIPWNywcm",
  "name":"apply_patch",
  "input":"*** Begin Patch\n*** Add File: standard_solution_assets/generate_diagrams.js\n+const fs = require(\"fs\");\n+const path = require(\"path\");\n+...\n*** End Patch\n"
}}
```

对应输出：
```json
{"timestamp":"...","type":"response_item","payload":{
  "type":"custom_tool_call_output",
  "call_id":"call_xC0cwhUrkwAls4ADIPWNywcm",
  "output":"{\"output\":\"Success. Updated the following files:\\nA standard_solution_assets/generate_diagrams.js\\n\",\"metadata\":{\"exit_code\":0,\"duration_seconds\":0.0}}"
}}
```

#### 3.6.5 实例 — reasoning（关键：encrypted_content）

```json
{"timestamp":"2026-05-05T10:32:25.685Z","type":"response_item","payload":{
  "type":"reasoning",
  "summary":[],
  "content":null,
  "encrypted_content":"gAAAAABp-cc5iFLAT0ORTXQiHGE8J3nI_YdRP1Pjbjne5x_h6HpzpghHlTwsQ08Ldcd7fVSL1ACQ9r8T17lagLzMHaJu4swCkFs6...(>2KB)"
}}
```

`summary` 是模型给的可视摘要项（`SummaryText { text }` 列表）；通常**为空**（除非配置 `reasoning_summary != "none"`）。`encrypted_content` 是 OpenAI 服务器返回的不透明 token，本质用于 resume 时把 reasoning 上下文喂回去保持因果链。**翻译器无法解码也无法伪造**——这是 Codex → CC 翻译的最大有损点。

#### 3.6.6 实例 — web_search_call

```json
{"type":"response_item","payload":{
  "type":"web_search_call","status":"completed",
  "action":{"type":"search","query":"Salesforce partner consulting","queries":[...]}
}}
```

或 `action: {"type":"open_page","url":"..."}` 或 `{"type":"find_in_page","url":"...","pattern":"..."}`。详见 `models.rs:1162-1187`。

#### 3.6.7 `Compaction` / `ContextCompaction`

这两个 `ResponseItem` variant 与 `RolloutItem::Compacted` 是不同对象。`Compaction { encrypted_content }` 是模型作为对话历史的一部分插入的"压缩后摘要"（远端压缩）；`RolloutItem::Compacted(CompactedItem)` 是一个会话级元数据 entry，直接 carry 完整的 `replacement_history: Vec<ResponseItem>`，**用于本地 inline compaction**（`codex-rs/core/src/compact.rs`）。

实测样例（截断）：
```json
{"timestamp":"...","type":"compacted","payload":{
  "message":"Another language model started to solve this problem and produced a summary of its thinking process. ...",
  "replacement_history":[
    {"type":"message","role":"user","content":[{"type":"input_text","text":"https://..."}]},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"你可以用飞书CLI..."}]},
    ...,
    {"type":"message","role":"developer","content":[{"type":"input_text","text":"<permissions instructions>..."}]},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>..."}]},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"重试"}]},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"Another language model started..."}]}
  ]
}}
```

`replacement_history` 给出"压缩之后供模型可见的精简对话"。Resume 时，凡 `compacted` 之后的 turn 看到的不是完整的旧 history，而是这份缩略版（`codex-rs/core/src/compact.rs:240-264`）。

### 3.7 关键 EventMsg payload schema

下面只列翻译器最常需要的字段。完整定义在 `codex-rs/protocol/src/protocol.rs`。

#### `task_started` / `task_complete` (`TurnStartedEvent` / `TurnCompleteEvent`)

```json
{"type":"task_started","turn_id":"...","started_at":1777977138,
 "model_context_window":258400,"collaboration_mode_kind":"default"}
```

```json
{"type":"task_complete","turn_id":"...","last_agent_message":"...",
 "completed_at":1777977386,"duration_ms":247714,"time_to_first_token_ms":7007}
```

> v2 wire 接受 `turn_started`/`turn_complete` 别名（`#[serde(alias = "turn_started")]`，`protocol.rs:1322-1328`）。

#### `user_message` / `agent_message`

```json
{"type":"user_message","message":"yooo","images":[],"local_images":[],"text_elements":[]}
{"type":"agent_message","message":"Yooo. What are we working on?",
 "phase":"final_answer","memory_citation":null}
```

`agent_message` 永远在对应的 `response_item.message(role=assistant)` **之后**写出（duplicate；UI 用一份，资料库用另一份）。`memory_citation` 指向 `~/.codex/memories/` 的引用（如有）。

#### `token_count`（每个 turn 末尾，可能多次）

```json
{"type":"token_count",
 "info":{
   "total_token_usage":{"input_tokens":15802,"cached_input_tokens":10624,
                       "output_tokens":282,"reasoning_output_tokens":157,"total_tokens":16084},
   "last_token_usage":{"input_tokens":15802,...},
   "model_context_window":258400},
 "rate_limits":{"limit_id":"codex","limit_name":null,"primary":null,"secondary":null,
                "credits":null,"plan_type":null,"rate_limit_reached_type":null}}
```

#### `exec_command_end`（`Extended` 模式持久化）

```rust
// protocol.rs:3099-3139
pub struct ExecCommandEndEvent {
    pub call_id: String,
    pub process_id: Option<String>,
    pub turn_id: String,
    pub completed_at_ms: i64,
    pub command: Vec<String>,
    pub cwd: AbsolutePathBuf,
    pub parsed_cmd: Vec<ParsedCommand>,
    pub source: ExecCommandSource,            // 默认 Agent
    pub interaction_input: Option<String>,
    pub stdout: String,                       // 持久化时被清空（见下）
    pub stderr: String,                       // 持久化时被清空
    pub aggregated_output: String,            // 截取至 10_000 字节，中间省略
    pub exit_code: i32,
    pub duration: Duration,                   // 序列化为字符串
    pub formatted_output: String,             // 持久化时被清空
    pub status: ExecCommandStatus,
}
```

写入磁盘前会被 `sanitize_rollout_item_for_persistence` 处理（`recorder.rs:195-218`）：仅保留 `aggregated_output` 截取到 10KB（`truncate_middle_chars`），`stdout`/`stderr`/`formatted_output` 全部清空。

#### `patch_apply_end`

```rust
pub struct PatchApplyEndEvent {
    pub call_id: String,
    pub turn_id: String,
    pub stdout: String,
    pub stderr: String,
    pub success: bool,
    pub changes: HashMap<PathBuf, FileChange>, // 见下
    pub status: PatchApplyStatus,              // Completed / Failed / Declined
}

pub enum FileChange {
    Add { content: String },
    Delete { content: String },
    Update { unified_diff: String, move_path: Option<PathBuf> },
}
```

实测样例（截断）：

```json
{"type":"patch_apply_end","call_id":"call_xC0...",
 "turn_id":"019df825-eae9-77a2-9130-d2352a24993b",
 "stdout":"Success. Updated the following files:\nA standard_solution_assets/generate_diagrams.js\n",
 "stderr":"","success":true,
 "changes":{"/Users/alice/standard_solution_assets/generate_diagrams.js":
            {"type":"add","content":"const fs = require(\"fs\");\n..."}},
 "status":"completed"}
```

#### `view_image_tool_call`、`web_search_end`、`thread_name_updated`、`error`、`turn_aborted`、`thread_rolled_back`

```json
{"type":"view_image_tool_call","call_id":"...","path":"/abs/path/to/file.png"}
{"type":"web_search_end","call_id":"...","query":"...","action":{...}}
{"type":"thread_name_updated","thread_id":"...","thread_name":"26M5 解决方案讨论"}
{"type":"error","message":"Selected model is at capacity. Please try a different model.","codex_error_info":"server_overloaded"}
{"type":"turn_aborted","turn_id":"...","reason":"...","completed_at":..., "duration_ms":...}
{"type":"thread_rolled_back","num_turns":2}
```

### 3.8 持久化路径汇总（写盘顺序）

一个标准 turn 会按时间顺序写入：

```
session_meta            (仅 1 次，文件首)
event_msg.task_started
response_item.message (role=developer, 注入 permissions/skills/collab_mode)
response_item.message (role=user, 注入 environment_context)
turn_context
response_item.message (role=user, 真正用户输入)
event_msg.user_message  (UI 镜像)
event_msg.token_count
... (tool 循环)
  response_item.reasoning
  response_item.function_call (or custom_tool_call)
  event_msg.exec_command_end (Extended)
  response_item.function_call_output (or custom_tool_call_output)
  event_msg.token_count
... (assistant 最终 message)
response_item.message (role=assistant, phase=final_answer)
event_msg.agent_message (UI 镜像)
event_msg.task_complete
```

如果发生 inline compaction：会插入一条 `compacted` 顶层 RolloutItem，其 `replacement_history` 是接下来对话的"虚拟历史"。

---

## 4. 内置工具清单

工具由 `codex-rs/tools/` crate 提供（`lib.rs:1-156` 是导出清单）。Codex **同一个 session 注册的工具集是动态的**——根据 model、`code_mode`、approval policy、是否启用 web_search 等条件。下表列出所有"出厂"工具及其 wire 名。

| Wire name | 创建函数 | 用途 | 持久化时类型 |
| --- | --- | --- | --- |
| `shell` | `create_shell_tool` (`local_tool.rs:151-212`) | 阻塞式 shell（argv 数组） | `function_call` |
| `shell_command` | `create_shell_command_tool` (`local_tool.rs:214-282`) | 阻塞式 shell（单个字符串） | `function_call` |
| `exec_command` | `create_exec_command_tool` (`local_tool.rs:19-105`) | 长运行 PTY shell；可挂起返回 session_id | `function_call` |
| `write_stdin` | `create_write_stdin_tool` (`local_tool.rs:107-149`) | 给已开 PTY 写入 stdin | `function_call` |
| `apply_patch` (Freeform) | `create_apply_patch_freeform_tool` (`apply_patch_tool.rs:89-99`) | 自由格式编辑（GPT-5 系） | `custom_tool_call` |
| `apply_patch` (JSON) | `create_apply_patch_json_tool` (`apply_patch_tool.rs:102-122`) | JSON 包装（gpt-oss/4.1） | `function_call` |
| `update_plan` | `create_update_plan_tool` (`plan_tool.rs:6-49`) | TODO 列表 | `function_call` |
| `view_image` | `create_view_image_tool` (`view_image.rs:14-37`) | 加载本地图片 | `function_call` |
| `request_permissions` | `create_request_permissions_tool` (`local_tool.rs:284-307`) | 请求扩权（`Granular` 审批模式） | `function_call` |
| `request_user_input` | `create_request_user_input_tool` (`request_user_input_tool.rs`) | Plan 模式或显式询问；非 Default 模式才注册 | `function_call` |
| `request_plugin_install` | `request_plugin_install.rs` | 请求安装 MCP/connector | `function_call` |
| `web_search` | `create_web_search_tool` (`tool_spec.rs:99-128`) | OpenAI Responses 内建工具，无需我们 dispatch | `web_search_call` |
| `image_generation` | `create_image_generation_tool` (`tool_spec.rs:87-91`) | OpenAI 内建图像生成 | `image_generation_call` |
| `local_shell` | `create_local_shell_tool` (`tool_spec.rs:83-85`) | OpenAI 内建 local_shell（仅特定模型） | `local_shell_call` |
| `tool_search` | `create_tool_search_tool` (`tool_discovery.rs`) | "搜索可用工具"元工具（启用 deferred loading 时） | `tool_search_call` |
| `create_goal` / `get_goal` / `update_goal` | `goal_tool.rs:create_*` | 长期目标管理 | `function_call` |
| `spawn_agent`、`send_input`、`wait_agent`、`close_agent`、`list_agents`、`resume_agent`、`send_message`、`followup_task` | `agent_tool.rs:create_*_v1/v2` | 子 agent 管理 | `function_call` |
| `spawn_agents_on_csv`、`report_agent_job_result` | `agent_job_tool.rs:create_*` | 批量子 agent 任务 | `function_call` |
| `list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource` | `mcp_resource_tool.rs:create_*` | 显式访问 MCP resource | `function_call` |
| `code_mode_*` | `code_mode.rs` | 把多个工具捆成一个执行单元（见 §4.10） | `function_call` |
| `test_sync_tool` | `utility_tool.rs` | 内部测试，非生产使用 | `function_call` |

下面对**翻译器最关心的工具**展开 schema。

### 4.1 `shell`（参数为 argv 数组）

`local_tool.rs:151-212`：

```json
{
  "name":"shell","type":"function","strict":false,
  "parameters":{
    "type":"object",
    "properties":{
      "command":{"type":"array","items":{"type":"string"},
                 "description":"The command to execute"},
      "workdir":{"type":"string","description":"The working directory ..."},
      "timeout_ms":{"type":"number","description":"The timeout for the command in milliseconds"},
      "sandbox_permissions":{"type":"string","description":"..."},
      "justification":{"type":"string","description":"..."},
      "prefix_rule":{"type":"array","items":{"type":"string"},...}
    },
    "required":["command"],
    "additionalProperties":false
  }
}
```

**Description**（Unix）：

> Runs a shell command and returns its output.
> - The arguments to `shell` will be passed to execvp(). Most terminal commands should be prefixed with ["bash", "-lc"].
> - Always set the `workdir` param when using the shell function. Do not use `cd` unless absolutely necessary.

Windows 描述更长，强制前缀 `["powershell.exe", "-Command", ...]` 等。

### 4.2 `shell_command`（参数是单字符串）

`local_tool.rs:214-282`：properties 同 `shell` 但 `command: string`、加 `login: boolean`（仅当 `allow_login_shell`）。这个工具更像 "run this exact shell script"，less ceremony。

### 4.3 `exec_command` / `write_stdin`（PTY 长运行）

`local_tool.rs:19-105` & `107-149`。**`exec_command` 是本机会话里出现得最多的 shell 工具**（实测 100% 用它，没用 `shell`）。完整 schema：

```json
{
  "name":"exec_command","type":"function","strict":false,
  "parameters":{
    "type":"object",
    "properties":{
      "cmd":{"type":"string","description":"Shell command to execute."},
      "workdir":{"type":"string","description":"... defaults to the turn cwd."},
      "shell":{"type":"string","description":"Shell binary to launch."},
      "tty":{"type":"boolean","description":"Whether to allocate a TTY ..."},
      "yield_time_ms":{"type":"number","description":"How long to wait (in ms) for output before yielding."},
      "max_output_tokens":{"type":"number","description":"Maximum tokens to return."},
      "login":{"type":"boolean","description":"-l/-i semantics. Defaults to true."}, // 当 allow_login_shell
      "sandbox_permissions":{"type":"string"},
      "justification":{"type":"string"},
      "prefix_rule":{"type":"array",...}
    },
    "required":["cmd"],
    "additionalProperties":false
  },
  "output_schema":{
    "type":"object",
    "properties":{
      "chunk_id":{"type":"string"},
      "wall_time_seconds":{"type":"number"},
      "exit_code":{"type":"number"},
      "session_id":{"type":"number"},        // 如果命令仍在运行
      "original_token_count":{"type":"number"},
      "output":{"type":"string"}
    },
    "required":["wall_time_seconds","output"],
    "additionalProperties":false
  }
}
```

**`exec_command_output_text`** 实际是非结构化 plain text（见 §3.6.3 实例），即模型并不真把 output 解析为 JSON，而是 plain text 包含 `Chunk ID:`/`Wall time:`/`Exit code:`/`Output:` 等行。Codex 把 `output_schema` 给模型看是为了让它知道字段含义，但 wire 上 `function_call_output.output` 是 string。

`write_stdin`：

```json
{"name":"write_stdin","parameters":{
  "properties":{
    "session_id":{"type":"number"},
    "chars":{"type":"string"},
    "yield_time_ms":{"type":"number"},
    "max_output_tokens":{"type":"number"}
  },"required":["session_id"]}}
```

### 4.4 `apply_patch`

完整内容见 §5。

### 4.5 `update_plan`

`plan_tool.rs:6-49`：

```json
{"name":"update_plan","type":"function","strict":false,
 "description":"Updates the task plan.\nProvide an optional explanation and a list of plan items, each with a step and status.\nAt most one step can be in_progress at a time.\n",
 "parameters":{"type":"object",
   "properties":{
     "explanation":{"type":"string"},
     "plan":{"type":"array","items":{
       "type":"object",
       "properties":{"step":{"type":"string"},
                     "status":{"type":"string",
                               "description":"One of: pending, in_progress, completed"}},
       "required":["step","status"],"additionalProperties":false}}},
   "required":["plan"],"additionalProperties":false}}
```

实例（实测）：
```json
{"type":"function_call","name":"update_plan",
 "arguments":"{\"plan\":[{\"step\":\"重构产品边界和报价结构\",\"status\":\"in_progress\"},{\"step\":\"生成 v1.1 三份源稿\",\"status\":\"pending\"},...],\"explanation\":\"我会把 Amazon 数字员工从...\"}",
 "call_id":"call_JXf6Gmu0fowsJVTBMgJHsibj"}
```

`update_plan` 不会产生 `function_call_output`——它的输出由 harness 直接 emit 到 UI（`PlanUpdate` event，**不持久化**）；模型其实只用其作为"对外宣告"。Resume 时通过 `event_msg.item_completed`（当 item 是 `TurnItem::Plan`）回放最终 plan 状态。

### 4.6 `view_image`

`view_image.rs:14-37`：

```json
{"name":"view_image","parameters":{
  "properties":{
    "path":{"type":"string","description":"Local filesystem path to an image file"},
    "detail":{"type":"string","description":"Optional detail override. The only supported value is `original`; ..."}
  },"required":["path"]},
 "output_schema":{"properties":{
   "image_url":{"type":"string","description":"Data URL ..."},
   "detail":{"type":["string","null"]}
 },"required":["image_url","detail"]}}
```

实测：
```json
{"type":"function_call","name":"view_image",
 "arguments":"{\"path\":\"/tmp/feishu_meeting_image.png\",\"detail\":\"original\"}",
 "call_id":"call_d5bmLpEQDTEWE7MNt4ofFaAd"}
```

### 4.7 `request_permissions`

`local_tool.rs:284-307` + `permission_profile_schema()`。仅在 `approval_policy = Granular` 时注册：

```json
{"name":"request_permissions","parameters":{
  "properties":{
    "reason":{"type":"string"},
    "permissions":{
      "type":"object","properties":{
        "network":{"properties":{"enabled":{"type":"boolean"}}},
        "file_system":{"properties":{
          "read":{"type":"array","items":{"type":"string"}},
          "write":{"type":"array","items":{"type":"string"}}}}}
    }},
  "required":["permissions"]}}
```

### 4.8 `request_user_input`

`request_user_input_tool.rs`。仅当 `collaboration_mode != Default`（即 Plan 模式或自定义模式）时注册。schema 大致为：

```json
{"name":"request_user_input","parameters":{
  "properties":{
    "questions":[{"id":"...","question":"...","kind":"text|choice","choices":[...]}]
  }}}
```

由于 Default 模式时不可用，实测会话里**没有出现**。开发者文档：仅在用户/agent 主动切到 Plan 模式才用。

### 4.9 子 agent 工具组（`spawn_agent` 等）

`agent_tool.rs` 提供 v1/v2 版本。代表：

```rust
pub fn create_spawn_agent_tool_v2(options) -> ToolSpec {
    // properties: agent_type, message, task_name, model?, ...
    // required: ["task_name", "message"]
}
```

每个 spawned agent 自己有一份 jsonl 文件，`SessionMeta.agent_role`/`agent_nickname`/`agent_path` 用于父子关联，`SessionSource::SubAgent(SubAgentSource)` 标记。Resume picker 默认隐藏 sub-agent 会话（除非 `--all` + `--include-non-interactive`）。

### 4.10 `code_mode`

`code_mode.rs`。把若干工具组装到一个 "exec a script" 入口里（用户写一段 JS/Python，里面调用多个工具）。这类似 Smol Agents 的"工具编程"模式。**不在普通 tui 默认开启**；启用条件为 `features.code_mode = true`（`unverified` 默认值，需要 features 配置查表）。

---

## 5. apply_patch 详解

`apply_patch` 是 Codex 编辑文件的**唯一原语**。其语法在 `codex-rs/tools/src/tool_apply_patch.lark`：

```
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch:   "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk:    "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line:    ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF
```

工具描述（`apply_patch_tool.rs:12-79`，传给模型的 system message）解释了 grammar、上下文行规则（默认上下 3 行，class/function 边界用 `@@`），以及一组实例。

### 5.1 三种文件操作

* **Add**：`*** Add File: <relpath>` 后面所有行以 `+` 开头，组成全文。

  ```
  *** Add File: src/hello.txt
  +Hello world
  +Second line
  ```

* **Delete**：`*** Delete File: <relpath>`，无后续内容。

* **Update**：`*** Update File: <relpath>` 后面可选 `*** Move to: <new relpath>`，再跟一或多个 hunk。每个 hunk 以 `@@` 开头（可选 `@@ <header text>`），随后行的首字符决定操作：` ` 上下文、`-` 删除、`+` 增加。

  ```
  *** Update File: src/app.py
  *** Move to: src/main.py          # 可选 rename
  @@ def greet():
  -    print("Hi")
  +    print("Hello, world!")
  ```

* **EOF marker**：hunk 末尾可加 `*** End of File`，告诉解析器这一段必须出现在文件末尾（容差对待 trailing newline）。

### 5.2 失败模式

`codex-rs/apply-patch/src/parser.rs:46-58, 894-` 与 `lib.rs:46-91`：

* `ParseError::InvalidPatchError(msg)` —— grammar 顶层错误（缺 begin/end，filename 空，等等）。
* `ParseError::InvalidHunkError { line_number, message }` —— hunk 内部错（context 不存在、`+`/`-`/` ` 之外的开头、未配对等）。
* `ApplyPatchError::ComputeReplacements(msg)` —— old_lines 在目标文件中找不到。
* `ApplyPatchError::IoError` —— 读写失败。
* `ApplyPatchError::ImplicitInvocation` —— 在非 `apply_patch` 命令里发现 patch 标记，强制要求显式调用。

对于 `Update` hunk，匹配是 **byte-exact**（包含空白），但带容差：trailing newline 容许差异，`ParseMode::Lenient`（默认）允许 `+`/`-`/` ` 后空行不带前缀字符（gpt-4.1 兼容）。

### 5.3 创建 / 删除 / 重命名能力

* **创建文件**：`Add File`，会自动 `mkdir -p` 父目录（`codex-rs/core/src/apply_patch.rs` + `codex-rs/apply-patch/src/lib.rs:apply_patch`，`unverified` 具体调用，但实测 `Add File: standard_solution_assets/generate_diagrams.js` 在不存在目录的情况下成功创建，说明它会 mkdir）。
* **删除文件**：`Delete File`。
* **重命名**：`Update File: a` + `*** Move to: b`，不带 hunk 即等同纯 rename；带 hunk 则先改内容再 move。
* **多文件**：一个 `*** Begin Patch ... *** End Patch` envelope 内可以有任意多 file ops。所有 op 是**事务性**——`HashMap<PathBuf, ApplyPatchFileChange>` 一次性应用，任一失败会 abort（具体回滚由 executor file system 决定，`unverified` 是否真的 transactional）。

### 5.4 路径相对性

`apply_patch_tool.rs:78`：

> "File references can only be relative, NEVER ABSOLUTE."

`workdir` 由 `function_call.arguments` / 工具调用上下文决定（`exec_command_*` 类工具的 workdir 字段，或 `apply_patch_tool` 的隐式 turn cwd）。这是 Codex 的一种安全设计——避免模型直接写 `/etc/passwd`。

### 5.5 Wire 包装：Freeform 还是 JSON？

`apply_patch_tool.rs:89-99`：默认对 GPT-5 系列模型使用 `ToolSpec::Freeform { format: { type:"grammar", syntax:"lark", definition:<lark grammar> } }`，wire 落在 `custom_tool_call.input` 字段（**整个 patch 是一个 raw string**）。

`apply_patch_tool.rs:102-122`：fallback 为 `ToolSpec::Function`，schema：

```json
{"name":"apply_patch","parameters":{
  "properties":{"input":{"type":"string","description":"The entire contents of the apply_patch command"}},
  "required":["input"]}}
```

此时 wire 上是 `function_call.arguments = "{\"input\": \"*** Begin Patch ... *** End Patch\"}"`。**两种形式持久化字段不同**：custom 是 `payload.input`，json 是 `payload.arguments` 里 JSON 解析后的 `.input`。**翻译器都需要识别**。

### 5.6 实例（来自本机会话）

```json
{"timestamp":"2026-05-05T12:43:51.055Z","type":"response_item","payload":{
  "type":"custom_tool_call","status":"completed",
  "call_id":"call_OMek3BIqp1gAWGPywQHn0aJJ","name":"apply_patch",
  "input":"*** Begin Patch\n*** Update File: standard_solution_assets/generate_diagrams.js\n@@\n   const ls = lines(text, maxLen);\n-  return `<text x=\"${x}\" y=\"${y}\" text-anchor=\"${anchor}\" font-size=\"${size}\" ...>${ls\n+  return `<text x=\"${x}\" y=\"${y}\" text-anchor=\"${anchor}\" font-family=\"Arial Unicode MS, ...\" font-size=\"${size}\" ...>${ls\n*** End Patch\n"
}}
```

输出（带 metadata）：
```json
{"type":"custom_tool_call_output",
 "call_id":"call_OMek3BIqp1gAWGPywQHn0aJJ",
 "output":"{\"output\":\"Success. Updated the following files:\\nM standard_solution_assets/generate_diagrams.js\\n\",\"metadata\":{\"exit_code\":0,\"duration_seconds\":0.0}}"}
```

`output` 是字符串化的 JSON：`{"output":"Success. ...","metadata":{"exit_code":0,"duration_seconds":...}}`。前缀字母 `A`（add）/ `M`（modify）/ `D`（delete）。

---

## 6. shell 系工具详解

### 6.1 三种 shell 工具的取舍

| 工具 | 阻塞？ | 参数 | 何时启用 |
| --- | --- | --- | --- |
| `shell` | 是 | argv `Vec<String>` | 默认 |
| `shell_command` | 是 | 单字符串 + login flag | `cfg.shell_command_backend == ToolUserShellType::ShellCommand` |
| `exec_command` + `write_stdin` | 否（PTY 持久 session） | 字符串 + `yield_time_ms` | "unified exec" 模式（默认开启于 0.128.0） |

工具注册器 `tool_config.rs` 决定哪些进入当前会话。**一个 session 通常只暴露其中一个集合**——本机 0.128.0 默认是 unified exec（即 `exec_command + write_stdin`）。

### 6.2 沙箱与审批

- 由 `TurnContextItem.sandbox_policy` + `approval_policy` 共同决定。每条 shell 调用先经 `safety.rs` (`codex-rs/core/src/safety.rs`) 判断是否需要升级。
- 升级方式：模型在 `arguments.sandbox_permissions` 设为 `"require_escalated"` 并写 `justification`，harness 弹出审批；如果 `approval_policy = never`，直接拒绝。
- `Granular` 模式允许更细：`sandbox_approval / rules / skill_approval / request_permissions / mcp_elicitations` 五个布尔门（`protocol.rs:947-961`）。
- `prefix_rule`：成功执行的命令可注册一个执行策略前缀（execpolicy），后续相同前缀 auto-approve。

### 6.3 Timeout

`codex-rs/core/src/exec.rs:51` 默认 `DEFAULT_EXEC_COMMAND_TIMEOUT_MS = 10_000`（10 秒）。模型可以用 `timeout_ms` / `yield_time_ms` 覆写。超时时进程组用 `kill_child_process_group` + 退出码 124。

### 6.4 stdout/stderr 捕获

`exec.rs:64-72`：
- 单条命令最大输出 `EXEC_OUTPUT_MAX_BYTES = DEFAULT_OUTPUT_BYTES_CAP`（约 100KB，需查 `codex_utils_pty`）。
- `MAX_EXEC_OUTPUT_DELTAS_PER_CALL = 10_000`：限制 SSE 事件数。
- 写盘前再次截取至 10KB 中间（`recorder.rs:193`）。
- 模型可见的 `formatted_output` 由 `function_call_output.output` 给出，包含 `Chunk ID`、`Wall time`、`Exit code`、`Original token count`、`Output:` 五段（实测见 §3.6.3）。

### 6.5 是否流式？

- **模型**：始终在 turn 末尾收到一次性整段 output（Responses API `function_call_output`）。不是 streaming。
- **UI**：`exec_command_output_delta` 事件流式推送（不持久化）。
- **PTY**：通过 `exec_command` + `write_stdin` 实现交互，模型可以在多个 turn 内分批读输出（每次给 `session_id`）。

### 6.6 实例

argv 数组（`shell`，未在本机会话出现，举例自 source）：
```json
{"type":"function_call","name":"shell","call_id":"...",
 "arguments":"{\"command\":[\"bash\",\"-lc\",\"rg -n 'TODO' src\"],\"workdir\":\"/repo\",\"timeout_ms\":15000}"}
```

unified `exec_command`（实测）：
```json
{"type":"function_call","name":"exec_command","call_id":"...",
 "arguments":"{\"cmd\":\"find /Users/alice/.codex/skills -maxdepth 3 -name SKILL.md -print\",\"workdir\":\"/Users/alice\",\"yield_time_ms\":1000,\"max_output_tokens\":8000}"}
```

write_stdin（实测）：
```json
{"type":"function_call","name":"write_stdin","call_id":"...",
 "arguments":"{\"session_id\":51190,\"chars\":\"\",\"yield_time_ms\":1000,\"max_output_tokens\":12000}"}
```

注意 `chars` 为 `""` 用作 "polling" 已在跑的 session。

---

## 7. 系统 prompt 与环境

### 7.1 BaseInstructions

默认 base prompt 在 `codex-rs/protocol/src/prompts/base_instructions/default.md`。开头：

> You are a coding agent running in the Codex CLI, a terminal-based coding assistant. Codex CLI is an open source project led by OpenAI. You are expected to be precise, safe, and helpful.

主要章节：

- **How you work**（personality）
- **AGENTS.md spec**（文件作用域规则）
- **Responsiveness**（preamble messages 风格、grouping）
- **Planning**（`update_plan` 用法、何时用）
- **Tool calling**（apply_patch、shell、view_image、…）
- **Sandbox & approval policies**
- **Tone & formatting**
- **Refusals & safety**

session 的 `SessionMeta.base_instructions.text` 字段就直接 carry 整段 markdown（实测 ~21KB）。`BaseInstructions` 可被 `[base_instructions]` 配置或运行时 override 替换；本机我看到的 Codex 用的是一个**自定义 personality**（`pragmatic`，~10KB），因为我配置了 personality_migration（看 `~/.codex/.personality_migration` 文件）。

### 7.2 AGENTS.md

`codex-rs/core/src/agents_md.rs:1-100` 总结：

* 默认文件名 `AGENTS.md`，本地优先 override 名 `AGENTS.override.md`。
* 项目根：从 cwd 向上找直到命中 `default_project_root_markers`（默认 `.git`）。
* **拼接规则**：从项目根 → cwd 一路下来，所有 AGENTS.md 内容串联；`config.user_instructions` 在前，AGENTS.md 在后；分隔符 `"\n\n--- project-doc ---\n\n"`。
* **写入位置**：作为 user instructions 注入到 developer message。Resume 时不会再次读取（实际行为：base_instructions 已 cache 在 SessionMeta，但 cwd 的 AGENTS.md 在每次 turn 重新展开 —— `unverified` 具体语义，看 turn_context 没看到 user_instructions 字段被填，需进一步看 turn_context 构造函数）。
* 全局 AGENTS.md：`$CODEX_HOME/AGENTS.md`（也支持 `AGENTS.override.md`），`load_global_instructions`。

### 7.3 环境变量

主要：

| Env | 作用 |
| --- | --- |
| `CODEX_HOME` | 覆盖 `~/.codex` |
| `OPENAI_API_KEY` | API key 兜底（`auth.json` 优先） |
| `CODEX_DEFAULT_MODEL` | 默认 model |
| `CODEX_*` | 各种 feature toggle |
| `RUST_LOG` | tracing 级别 |
| `CODEX_RS_FORCE_BWRAP` 等 | sandbox backend 选择 |

完整列表见 `codex-rs/core/src/flags.rs`（`unverified` 完整命名表）。

### 7.4 Approval policy 与 Sandbox policy 细节

- `approval_policy = never`：模型自行决定，所有 shell 直接 run。
- `untrusted`：仅 `is_safe_command()` 列表内的命令（`ls`、`cat`、`sed` 等）auto-approve，其它弹审批。
- `on-request`：模型决定何时升级；`sandbox_permissions = "require_escalated"` + justification 触发审批。
- `granular`：见 §6.2。
- `on-failure`：DEPRECATED，仅在命令失败时升级。

`sandbox_policy`：

- `read-only` (with `network_access`)
- `workspace-write` (默认 cwd 可写，可加 `writable_roots`、`network_access`、`exclude_tmpdir_env_var`、`exclude_slash_tmp` 等子字段)
- `danger-full-access` (无沙箱)
- `external-sandbox`：被外部 wrapper 包了，Codex 自己不再添加约束

实际后端：macOS 用 `sandbox-exec` (Seatbelt)；Linux 用 `bwrap` 或 `landlock` + seccomp；Windows 用 RestrictedToken / AppContainer / Job objects。

---

## 8. Resume 机制

### 8.1 命令

```
codex resume                        # 显示 picker，按 cwd 过滤
codex resume --last                 # 直接续最近会话（按 cwd）
codex resume --all                  # picker 不过滤 cwd
codex resume <UUID>                 # 直接续这个 session
codex resume <thread_name>          # 按 thread_name 找
codex resume --include-non-interactive  # 包含 sub-agent / exec / mcp 会话
codex resume <id> "<additional prompt>"  # 续会话同时追加一条 user message

codex fork [--last|<UUID>] [<prompt>]    # 分叉：复制旧会话 history + new id + forked_from_id 字段

codex exec resume [--last|<UUID>] [<prompt>] -o <output.md>  # 非交互续
```

### 8.2 路径 vs ID

`codex resume` 接受的是 **session ID 或 thread name**，**不是文件路径**。它内部用 `find_thread_path_by_id_str(codex_home, id)` 在 sessions 目录下查找匹配的 jsonl（`codex-rs/rollout/src/list.rs`）。这个查找：

1. 先走 SQLite (`codex_state::list_threads_db`)；
2. 不命中走文件系统 glob `sessions/**/rollout-*-<UUID>.jsonl`；
3. 如果传入的是非 UUID 字符串，从 `session_index.jsonl` 反向扫描 `thread_name` 匹配，再用 ID 解析。

### 8.3 Resume 时如何 hydrate

`codex-rs/rollout/src/recorder.rs:862-946`：

1. `tokio::fs::read_to_string(path).await?`。
2. 逐行 `serde_json::from_str::<Value>` → 跳过空行 / 非法行 / `ghost_snapshot` 兼容垃圾。
3. 每行尝试 `from_value::<RolloutLine>`：
   - `SessionMeta`：仅第一条作为 `thread_id`，但所有 SessionMeta items 全部保留（fork 时多个）。
   - 其余按原样 push 到 `Vec<RolloutItem>`。
4. 返回 `InitialHistory::Resumed { conversation_id, history, rollout_path }`。

之后 `codex_thread.rs` 会：

- 构建初始 `Session` 实例，复用同一个 `rollout_path` 以追加。
- 用 `replacement_history`（如果有 `Compacted` item）替换 in-memory 对话历史；否则用所有 `ResponseItem` 平铺出 history。
- 注入新一轮的 developer / environment_context messages。
- 如果 CLI 给了 prompt，作为新 user message 加入。

### 8.4 是否重新执行旧 tool calls？

**否**。所有 `function_call_output` / `custom_tool_call_output` 直接喂回模型作为历史；shell 不会重跑、文件不会重 patch。这与 CC 一致。

**例外**：如果会话最后一个 turn 没有完成（abort 时刻刚好在 model thinking 中），resume 时该 turn 会被 truncate 到最后一个 complete `task_complete`。`thread_rolled_back` 事件 / `pending` plan 会显示给用户。

### 8.5 工具集兼容性？

Resume 时**当前注册的工具**与会话历史里调用过的工具**不一定一致**。Codex 不会校验。如果你之前用了某个 MCP 工具，但 resume 时该 MCP 已被卸载，模型只会看到对话历史里的 `function_call`/`function_call_output` 文字，但不会再有这个工具可用（除非它自己调）。

实践中**最安全的翻译策略**是：
* 把 CC 的 `tool_use` 翻译成 Codex 里同名工具的 `function_call`（如果 wire-name 能对得上）。
* 否则降级成一条 user/developer 文本消息（注入"前面我做了 X"语境）。详见 §11。

### 8.6 手工拼装 jsonl 是否会被 `resume` 接受？

**会**。源码里没看到 schema 校验，只要每行能 deserialize 成 `RolloutLine` 就保留。注意：

- 非法行被静默丢弃（`recorder.rs:880-885`）。
- **必须有至少一行 `session_meta`**——否则 `InitialHistory::Resumed.conversation_id` 会缺失，`get_rollout_history` 返回错误（`codex-rs/rollout/src/recorder.rs:933`：`thread_id.ok_or_else(IoError::other("failed to parse thread ID"))?`）。
- **timestamp 必须是 ISO-8601 字符串**，但 Codex 没有强校验格式（只在 metadata.rs `parse_timestamp_to_utc` 失败时丢弃 SQLite 索引；jsonl 仍然可读）。
- **path 字段**（cwd、rollout_path）写绝对路径。

---

## 9. MCP 与扩展性

### 9.1 Codex 是 MCP 服务端 + MCP 客户端

- `codex mcp-server`：Codex 自己暴露成 MCP server（基于 stdio），其他 MCP-aware host 可以远程 spawn agent。
- `codex mcp`：管理外部 MCP server（add/remove/list）。
- 配置 `[mcp_servers.<name>] command = ["..."], args = [...], env = {...}`。

### 9.2 MCP 工具的会话表示

每个 MCP 工具被发现后会生成一份 `LoadableToolSpec`（`responses_api.rs`）。在 turn 期间，MCP 工具调用持久化为：

- `event_msg.mcp_tool_call_begin`（**不持久化**，UI only）。
- `event_msg.mcp_tool_call_end`（**Limited 模式持久化**）：

  ```json
  {"type":"mcp_tool_call_end","call_id":"...",
   "invocation":{"server":"<server>","tool":"<tool>","arguments":{...}},
   "duration":"...","result":{"Ok": <CallToolResult>}}
  ```

- `response_item.function_call`（带命名空间，例如 `name: "lark", namespace: "feishu"` 之类——`unverified` 实测细节）。
- `response_item.function_call_output`：MCP 返回 content 序列化为 `FunctionCallOutputBody::ContentItems` 数组。

`SessionMeta.dynamic_tools` 会列出 turn 启动时注册的 MCP 工具 spec，让 resume picker 知道当时有哪些可用工具。

### 9.3 自定义工具

Codex 有 `dynamic_tools` 机制（`codex-rs/protocol/src/dynamic_tools.rs`）允许 plugin 注入工具。常见场景：marketplace plugin 提供的 connector 工具。

---

## 10. Reasoning content

### 10.1 持久化结构

`ResponseItem::Reasoning { id, summary, content, encrypted_content }`：

- `summary: Vec<ReasoningItemReasoningSummary>` —— 模型可见的"摘要 block"列表。仅当 `reasoning_summary` 配置非 `none` 时才有内容。本机 100% 是 `summary: []`。
- `content: Option<Vec<ReasoningItemContent>>` —— 明文 reasoning（`ReasoningText { text }` 或 `Text { text }`）。仅在 stream `reasoning_raw_content` 启用时持有；磁盘上**几乎总是 null**。
- `encrypted_content: Option<String>` —— Responses API 返回的加密 token，**唯一会真实出现的字段**。Resume 时直接喂回 API。

### 10.2 Replay 行为

- **同一 session 内 resume**：`encrypted_content` 原样发回 OpenAI，模型可以 reuse 因果链，等同于"接着思考"。
- **跨 harness 翻译（CC ↔ Codex）**：
  - CC 没有 `encrypted_content`——其 thinking 是明文。CC → Codex 时，可以构造 `ResponseItem::Reasoning` 但 `encrypted_content: None`、`summary` 来自 CC 的 thinking 文字。这样**resume 不会出错**，但模型不会有上下文连续性的强保障——视为"丢失思考链，但保留摘要"。
  - Codex → CC 时，Codex 的 reasoning 大多 `summary: []` + `encrypted_content: <opaque>`。CC 不需要任何它，所以直接丢弃即可。

### 10.3 UI 展示

`AgentReasoning` / `AgentReasoningRawContent` / `AgentReasoningSectionBreak` 三种 event_msg：

- `AgentReasoning`：摘要级（持久化，Limited）。
- `AgentReasoningRawContent`：明文级（持久化，Limited）。
- `AgentReasoningSectionBreak`：标志（不持久化）。

实测我的会话有 `reasoning` response_item 但没有 `agent_reasoning` event_msg —— 因为 `summary: []` 时 UI 没东西可显示。

---

## 11. CC → Codex 翻译的有损映射

### 11.1 无对等的 CC 概念

| Claude Code 概念 | Codex 对应物 | 推荐降级策略 |
| --- | --- | --- |
| `Skill` 调用（`mcp__skill__...`） | Codex 也有 skills（详见 §7.1 和 `codex-rs/core/src/skills.rs`），但 wire 协议不一样：CC 是 MCP 工具调用，Codex 把 skill 注入到 developer message + 通过普通 shell/apply_patch 执行 | 把 CC 的 skill_call 转成 `developer` message："以下来自 skill `<name>` 的指令：..."；后续模型操作落到普通工具 |
| `Task` (subagent) | Codex 的 `spawn_agent` 系列 | name 不一致；可保留 function_call 文字，但不会真的 spawn。建议降级为 developer 消息："此处子 agent 已完成，结果为：..." |
| `TaskCreate` / `TaskUpdate`（todo） | Codex 的 `update_plan` | 一一映射：CC todo `{content, status}` → Codex `{step, status}`；`status: in_progress / completed / pending` 直译 |
| `EnterPlanMode` / `ExitPlanMode` | Codex 的 collaboration_mode `plan` / `default` | 在 turn_context.collaboration_mode 上切换；本机 turn_context 已经包含完整 mode 描述。Plan mode 还会把 `request_user_input` 工具暴露 |
| `Worktree`（创建/退出） | 无对应 | 降级为 user/developer 消息记录 |
| `ScheduleWakeup` / `Cron` | 无对应 | 同上 |
| `AskUserQuestion`（多选） | Codex 的 `request_user_input`（仅 Plan mode）能近似 | base_instructions 明确禁止 multiple choice 文本，所以降级为：在 Plan mode 下 `function_call request_user_input { questions: [...] }`，否则降级为 plain assistant question |
| `WebFetch` / `WebSearch` | Codex 的 `web_search`（Responses 内建） | 翻译时把 CC `WebSearch` → Codex `web_search_call`；CC `WebFetch` 没有完美对应，可降级为 `exec_command` 跑 `curl` |
| `Read` / `Write` / `Edit` / `MultiEdit` / `NotebookEdit` | Codex 的 `apply_patch` + `exec_command cat` | 见 §11.3 |
| `Glob` / `Grep` | Codex 的 `exec_command` 跑 `rg --files` / `rg -n` | 直接翻译为 shell |
| `Bash` | Codex 的 `shell` / `exec_command` | argv 数组直译 |
| `BashOutput` / `KillBash` | Codex 的 `write_stdin` / 杀 session | unified exec 等价 |

### 11.2 CC → Codex jsonl 翻译总流程

1. **顶层包裹**：
   - 写一行 `session_meta`：`id` 用新生成 UUID v7（与 CC 的 `sessionId` 不一定 UUID 兼容，可重新铸造一次并保留映射），`source: Custom("agent-bridge")`，`originator: "agent-bridge"`，`cli_version` 写翻译器版本，`cwd` = CC 的 `cwd`，`base_instructions.text` 选 Codex 默认（或注入"以下对话来自 Claude Code"前言）。
   - 写一行 `turn_context`，至少 `cwd`、`model`、`approval_policy`、`sandbox_policy`、`collaboration_mode.mode = default`。
2. **逐条 CC event 映射**：
   - `user`/`assistant` 消息：→ `response_item.message`。`role` 直译；CC 的 `tool_use` content block 提到的工具调用拆出来变独立 `function_call`/`custom_tool_call`。
   - CC 的 `system` 消息 → `response_item.message role=developer`（Codex 习惯）。
   - CC 的 thinking content：→ `response_item.reasoning summary=[{text}]` + `encrypted_content: null`。
   - CC 的 `tool_use`（id: toolu_xxx, name: <tool>, input: {...}）→ Codex `function_call` 或 `custom_tool_call`，按工具决定。`call_id = toolu_xxx`（保持 idempotent ref）。
   - CC 的 `tool_result`（tool_use_id: toolu_xxx, content）→ Codex `function_call_output` 或 `custom_tool_call_output`。
   - CC 的 todo update → `function_call name=update_plan`。
3. **细粒度工具 mapping**：见 §11.3。

### 11.3 Edit/Write/MultiEdit → apply_patch

CC 的几种文件操作翻成一段 `apply_patch.input`：

- **Write { path, content }** → `*** Add File: <relpath>` + `+<each line>` 列表（先把绝对路径相对化）。如果文件已存在，CC 的 Write 行为是覆盖整个文件——对应 Codex 的"`Update File` + 一个 `*** End of File` hunk 替换全部内容"或者干脆"`Delete File` 然后 `Add File`"（前者更合 grammar）。
- **Edit { file_path, old_string, new_string, replace_all? }** → `*** Update File: <relpath>` + 一个 hunk，old_string → 一组 `-` 行、new_string → 一组 `+` 行。需要前后各 ≤3 行 context，从原文里读出。`replace_all = true` 意味着多次 hunk（每个 occurrence 一个）。
- **MultiEdit { file_path, edits[] }** → 同一个 `*** Update File: <relpath>` 下的多个 hunk，按出现顺序排列。
- **NotebookEdit** → 没有直接 patch 对应。最粗暴是：把 .ipynb 完整 JSON 写入到 Codex `apply_patch Add/Update File`。但 .ipynb 的 cell-level edit 在 Codex 里只能整文件 round-trip。

`call_id` 沿用 CC 的 `tool_use_id`（前缀差异不会有问题——Codex 也是字符串）。`function_call_output`/`custom_tool_call_output` 的 output 用 CC tool_result 的纯文字结果。

### 11.4 不翻译的字段

- CC 的 `cache_control` 标记：Codex 不识别，丢弃。
- CC 的 `stop_reason`：Codex 用 `task_complete.duration_ms` + `last_agent_message` 表达，没有完美等价。
- CC 的 `permission` 事件：Codex 用 `event_msg.exec_approval_request`（不持久化），所以丢弃即可。
- CC 的 `Mcp` 工具 wrapper：把 wire name `mcp__<server>__<tool>` 拆开，写 `function_call name=<tool> namespace=<server>`，并在 SessionMeta.dynamic_tools 里注册。

---

## 12. Codex → CC 翻译的有损映射

### 12.1 关键损失

| Codex 特性 | CC 是否支持 | 翻译策略 |
| --- | --- | --- |
| `apply_patch` Lark grammar | 否 | 拆成多次 CC Edit/Write/Delete 调用；每个 hunk → 一次 Edit；`Add File` → Write；`Delete File` → 调用 Bash `rm`；`Move to` → Bash `mv` 后 Edit |
| `reasoning.encrypted_content` | 否 | 丢弃；如果有 `summary[].text` 翻译成 `thinking` content block；否则丢弃 |
| `code_mode` | 否 | 拆成多个独立 tool_use（`code_mode` 在源码里是把多个 tool 调用塞进一个 script，CC 这边只能反编译） |
| `spawn_agent` 子会话 | CC 有 Task tool | 把 sub-agent 的整个 jsonl 翻译成 CC 的 Task 调用 + 子结果 |
| `request_user_input`（multi-Q） | CC 有 AskUserQuestion | 字段直译：`questions[]` → CC 的 questions[] |
| `web_search_call` | CC 有 WebSearch | 直译（注意 CC 没有 OpenAI 的 cached/live 概念） |
| `image_generation_call` | CC 没有内建 image_generation | 降级为 assistant text："I would generate an image with prompt: ..." |
| `view_image` | CC 用 Read tool 读图 | function_call `view_image` → CC `Read` tool_use（path）+ tool_result 用图片 base64 |
| `compacted` rollout item | CC 有自己的 compacting（`isCompactSummary`） | 把 `replacement_history` 翻成新的对话 history，丢弃原始历史 |
| `event_msg.thread_name_updated` | CC 的 `summary` event | 直译 |
| `permission_profile` / `granular` 审批 | CC 仅 allow/deny | 降级为字符串注释 |
| `personality` / `collaboration_mode` | CC 没对应概念 | 注入 system prompt 段落（`<personality>pragmatic</personality>` 等等） |

### 12.2 apply_patch → Edit 的反向工程

每个 `*** Update File` hunk：

1. 在原 hunk 文本里找出 `-` 行的起止：那就是 `old_string`。
2. `+` 行连起来就是 `new_string`。
3. 把 `@@` header（如有）合并到 old_string 前缀，避免 `old_string` 不唯一时报错。
4. 如果一个 update 有多个 hunk，可以拆成 MultiEdit 单次调用（`edits: [...]`）。

`*** Add File`：直接 Write。
`*** Delete File`：CC 没有 Delete tool；用 Bash `rm <path>`。
`*** Move to`：用 Bash `mv <old> <new>`，再发一次 Edit/Write 处理后续 hunk。

CC 工具会**真的去文件系统执行 Edit**（除非 dry-run），所以反向翻译时如果只为了"显示历史"，应当生成 tool_use+tool_result（result 为成功消息），不实际跑。

### 12.3 多 turn 边界

Codex 一个 turn = `task_started` 到 `task_complete`。CC 一条 conversation message 也对应一次完整 turn。两边的 turn 边界**等价**，但内部子结构不同：

* Codex 在一个 turn 里可能发起多次 tool_call（响应里多个 function_call 项），CC 则把每个 tool_use/tool_result 当独立 message。
* 翻译时：Codex `response_item` 序列里的 tool 调用按时间顺序拆分成 CC 一条条 message。

### 12.4 token usage

Codex 的 `token_count.info.total_token_usage` (`input/cached_input/output/reasoning_output/total`) → CC 的 `usage`（`input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens`）。`reasoning_output_tokens` 在 CC 里没有对应键——可累加进 `output_tokens` 或丢弃。

---

## 13. 未确认问题（Open Questions）

1. **`exec_command_end` 的实际持久化模式**：源码上 `Limited` 模式不写它，但本机会话有写出。需要确认 0.128.0 TUI 是否默认 `Extended`。源码 `codex-rs/core/src/codex_thread.rs` 应该有 `EventPersistenceMode::Extended` 的初始化点（尚未读完）。
2. **`apply_patch` Add File 的 mkdir 行为**：`apply-patch/src/lib.rs` 是否真的递归创建父目录，未在 `lib.rs` 文本中找到 `create_dir_all` 调用；可能委托给 `ExecutorFileSystem`。需要看 `codex-exec-server::ExecutorFileSystem` 的实现。
3. **Resume 时的工具集校验**：源码 `core/src/codex_thread.rs` 是否对历史中出现的工具名做了存在性校验？目前推测无校验，但未直接定位。
4. **AGENTS.md 在 resume 时是否重新加载**：`turn_context.user_instructions` 字段在我的会话里 `null`，但 base_instructions 已经在 SessionMeta 里。AGENTS.md 似乎在每个 turn 时拼接到 developer message。需要看 `Session::build_initial_context` 实现。
5. **`disable_response_storage = true` 的语义**：本机配置开启了它，会话中确实有 `encrypted_content`——所以这个 flag 似乎不影响本地 jsonl 持久化，只影响 OpenAI 服务端是否保留 raw response。需要 `model_provider/store_responses` 字段确认。
6. **`compaction_summary` 与 `Compaction` 的别名**：`#[serde(alias = "compaction_summary")]` 表示老版本会话用过 `compaction_summary` 类型——翻译器至少需要 ingest 时识别这个旧别名。
7. **`local_shell_call`**：本机 0.128.0 用的是 unified `exec_command`，没看到 `local_shell_call`。需要确认是否仅 `o3` / `gpt-4.1` 等模型才注册 `LocalShell` 工具。
8. **跨 cwd resume**：当 `current_cwd != session.cwd` 时，TUI 会弹 `cwd_prompt`。`exec resume` 子命令是否同样有 prompt？源码 `codex-rs/exec/src/cli.rs` 未读完。
9. **`CompactedItem.replacement_history` 的 schema**：里面又是 `Vec<ResponseItem>`，但 `developer` role 消息里嵌入了完整的 `<permissions instructions>` `<collaboration_mode>` `<skills_instructions>` blocks。这意味着翻译器在跨 harness 重建 history 时也要构造这些 XML 包裹。模板未在 source code 中明显常量化，需要检查 `core_skills::injection`、`core/src/codex_thread.rs::build_initial_context` 等位置。
10. **加密 reasoning 跨 session 是否可用**：`encrypted_content` 是 OpenAI 服务器签发的 token，是否绑到 session_id？官方 docs 暗示绑到 organization、可跨 session 使用，但未实测。

---

## 附录 A：字段级翻译表（精简）

| 概念 | Codex 字段 | Claude Code 字段 |
| --- | --- | --- |
| 会话 ID | `session_meta.id` (UUID v7) | `sessionId` (UUID v4) |
| Parent fork | `session_meta.forked_from_id` | `parentUuid` |
| 创建时间 | `session_meta.timestamp`（ISO-8601 UTC） | `timestamp` |
| 项目 cwd | `session_meta.cwd` + `turn_context.cwd`（最新优先） | encoded path 写在 `~/.claude/projects/<encoded>/` 目录上 |
| Source kind | `session_meta.source` (`cli` / `vscode` / `exec` / `mcp` / `custom("...")` / `subagent` / `internal`) | `source` 字段（罕见）+ 文件位置 |
| User message | `response_item.message role=user content=[{type:input_text, text}]` | `type:user` 顶层，`message.role:"user"`, `message.content` 数组 |
| Assistant message | `response_item.message role=assistant content=[{type:output_text}] phase=final_answer\|commentary` | `type:assistant`, `message.content` 数组 |
| Developer/system | `response_item.message role=developer` | `type:system` |
| Thinking | `response_item.reasoning summary=[] content=null encrypted_content="..."` | `content[].type=thinking text=...` |
| Tool call | `response_item.function_call name arguments call_id` 或 `custom_tool_call name input call_id` | `content[].type=tool_use id name input` |
| Tool result | `response_item.function_call_output call_id output` | `content[].type=tool_result tool_use_id content is_error` |
| Web search | `response_item.web_search_call action={search\|open_page\|find_in_page}` | `tool_use name=WebSearch input.query` |
| Plan/todo | `response_item.function_call name=update_plan arguments.plan` | `tool_use name=TaskCreate\|TaskUpdate input.todos` |
| Token usage | `event_msg.token_count.info.total_token_usage` | message.usage |
| Compaction marker | `compacted` 顶层 + 其 `replacement_history` 数组 | `type:summary` 或 `isCompactSummary` |
| Turn start/end | `event_msg.task_started` / `task_complete` | 隐含在 message 序列；`stop_reason` 标记 |
| Errors | `event_msg.error` | `tool_result.is_error=true` 或 errored message |

## 附录 B：最小可 resume 的 jsonl 模板

下面是一份手工拼装、Codex 0.128.0 能 `resume` 接受的最小三行 jsonl。把它放在 `~/.codex/sessions/2026/05/06/rollout-2026-05-06T17-00-00-019df900-0000-7000-9000-000000000001.jsonl`，然后 `codex resume --include-non-interactive 019df900-0000-7000-9000-000000000001`：

```json
{"timestamp":"2026-05-06T09:00:00.000Z","type":"session_meta","payload":{"id":"019df900-0000-7000-9000-000000000001","timestamp":"2026-05-06T09:00:00.000Z","cwd":"/Users/alice","originator":"agent-bridge","cli_version":"0.0.1","source":{"custom":"agent-bridge"},"model_provider":"openai","base_instructions":{"text":"You are Codex (translated from Claude Code)."}}}
{"timestamp":"2026-05-06T09:00:00.001Z","type":"turn_context","payload":{"turn_id":"019df900-0000-7000-9000-000000000002","cwd":"/Users/alice","current_date":"2026-05-06","timezone":"Asia/Shanghai","approval_policy":"never","sandbox_policy":{"type":"danger-full-access"},"model":"gpt-5.5","summary":"none","truncation_policy":{"mode":"tokens","limit":10000}}}
{"timestamp":"2026-05-06T09:00:00.002Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Continue the work from earlier."}]}}
```

注意：

- `session_meta.id` UUID 段位需要满足 v7（首两段 = unix timestamp 毫秒）。低版本 Codex 比较宽松，但 SQLite 索引会要求严格 UUID。
- `turn_context.model` 必须是 codex 配置里"已知"的 model name，否则模型选择 fallback 到默认。
- 缺失 `forked_from_id` 表示是新会话；想模拟"fork 自 X"，就把它设到原 session 的 UUID。
- `source: {"custom":"agent-bridge"}` 会让 picker 默认隐藏（除非 `--include-non-interactive`）。设 `"cli"` 可让它正常出现在主 picker，但容易污染用户列表。

---

## 附录 C：源码定位速查

| 想看什么 | 文件 |
| --- | --- |
| Rollout 定义 | `codex-rs/protocol/src/protocol.rs:2795-2950` |
| ResponseItem 类型 | `codex-rs/protocol/src/models.rs:741-891` |
| EventMsg 枚举 | `codex-rs/protocol/src/protocol.rs:1286-1530` |
| Persistence 策略 | `codex-rs/rollout/src/policy.rs:14-181` |
| Recorder（写 jsonl） | `codex-rs/rollout/src/recorder.rs` |
| Resume hydration | `codex-rs/rollout/src/recorder.rs:862-946` |
| Resume CLI 解析 | `codex-rs/cli/src/main.rs:275-320, 1724-1745` |
| TUI resume picker | `codex-rs/tui/src/resume_picker.rs`, `session_resume.rs` |
| apply_patch grammar | `codex-rs/tools/src/tool_apply_patch.lark` |
| apply_patch 工具 | `codex-rs/tools/src/apply_patch_tool.rs` |
| apply_patch 解析器 | `codex-rs/apply-patch/src/parser.rs` |
| shell 工具集 | `codex-rs/tools/src/local_tool.rs` |
| update_plan | `codex-rs/tools/src/plan_tool.rs` |
| view_image | `codex-rs/tools/src/view_image.rs` |
| MCP 工具桥 | `codex-rs/tools/src/mcp_tool.rs`, `core/src/mcp.rs` |
| AGENTS.md | `codex-rs/core/src/agents_md.rs` |
| Compaction | `codex-rs/core/src/compact.rs` |
| Default base prompt | `codex-rs/protocol/src/prompts/base_instructions/default.md` |
| Skills | `codex-rs/core/src/skills.rs`, `codex-rs/core-skills/` |

---

> 总结一句：**Codex 的会话格式是"每行一个 RolloutLine"的 jsonl，第一行必为 session_meta，其余按时间顺序混合 turn_context / response_item / event_msg / compacted 四类**。`resume` 不校验内容，只解析；`fork` 复制 history、新 ID、`forked_from_id` 反向链。最重要的有损映射点：apply_patch（Codex 端唯一编辑器）⇆ Edit/Write/MultiEdit（CC 端），以及 reasoning 的 `encrypted_content`（不可逆）。
