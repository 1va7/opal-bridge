# Claude Code Harness — Session Format & Tool Reference

> 目标读者：要在 Claude Code（CC） 与 Codex 之间做 session 双向翻译的工程师。
> 本文档是**实证型**参考资料：所有结论都尽量给出磁盘证据 / 二进制证据 / 文档来源。
> 调研环境：macOS 25.3，CC 版本 `2.1.131`（写作时仓库里同时存在 `2.1.29 / 2.1.97 / 2.1.107 / 2.1.112 / 2.1.117 / 2.1.128 / 2.1.131` 留下的会话）。

证据约定：
- **[disk]** = 直接读取 `~/.claude/projects/...jsonl` 得到。
- **[bin]**  = 从 `claude.exe`（Bun 单文件二进制）`strings` 得到。
- **[doc]**  = 来自官方文档 / Anthropic 官方仓库。
- **[web]**  = 来自第三方逆向博客 / GitHub 工具的二级证据，已与磁盘交叉对照。
- **[unverified]** = 尚未取得证据，下结论时已显式标注。

---

## 0. TL;DR — 把 CC session 翻译到别的 harness 时必须先记住的事

1. **物理结构**：每个 session 是 `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`，**append-only**，每行一个独立 JSON。文件名里的 UUID 就是 sessionId。
2. **逻辑结构**：每行有 `uuid` + `parentUuid`，构成一棵 DAG（不是简单数组）。`/resume` 时 CC 会把同一个项目目录里**所有** jsonl 合并成一张图，根据 `parentUuid` 重组对话；这意味着翻译器不能只看一个文件。
3. **不止一种 line type**：除了 `user` / `assistant` 还有 `system`（含子类型）、`attachment`（10+ 子类型）、`file-history-snapshot`、`permission-mode`、`queue-operation`、`last-prompt`、`ai-title`、`custom-title`、`agent-name`、（旧版本）`summary`、`compact_boundary`（system 子类型）。
4. **Compaction 边界**：`type=system / subtype=compact_boundary` 是关键，后面紧跟一条 `isCompactSummary: true` 的伪造 user 消息。翻译器要决定是把 boundary **截断重启**还是**保留摘要**。
5. **Subagent**：现代 CC（2.1.x）用 `Agent` 工具产生子 agent，子 agent 的完整 transcript 放在 `<session-id>/subagents/agent-<id>.jsonl`，**主 session 里只保留 `Agent` 工具的最终 `tool_result`**（一条文本）。同步 vs 异步的 result 形式不同。
6. **`sessions-index.json` 不可信**：本仓库里 `~/.claude/projects/-Users-alice/` 这个最频繁使用的项目里**根本没有这个文件**；只有几个不再使用的旧项目目录里残留 stale index。CC 的官方真相源是「目录扫描 + 解析每个 jsonl 头/尾」（见 `xRH` / `Wb5` / `Gb5` 函数）[bin]。
7. **System prompt 不写入 jsonl**：每次 turn 由 harness 实时拼接（CLAUDE.md 内容 + currentDate + skill listing + 工具描述），通过 `attachment` line 把 `nested_memory` / `skill_listing` / `task_reminder` / `command_permissions` 等动态片段记录在用户回合上下文里。**翻译器必须模拟拼接，否则生成的会话回放时模型会走偏。**
8. **真正的工具集合**比系统提示给我们看到的更窄，但也有几个隐藏成员：jsonl 里实际出现过 `Glob` 和 `Grep`（系统提示没列），而 `MultiEdit` / `TodoWrite` 已经被废弃为 `TaskCreate / TaskUpdate / ...` 体系（见 §3）。
9. **Permission mode 是会话级状态**：单独写在一行 `type=permission-mode`，不在 user/assistant 消息里。翻译时不能丢弃。
10. **大输出会被外置**：超过阈值的 Bash / Web / Read 结果会被写到 `<session-uuid>/tool-results/<random-id>.txt`，jsonl 里只保留 preview，需要时翻译器要回放原始内容必须读这些文件。

---

## 1. Session storage 物理布局

### 1.1 目录树

实际形态（[disk] 来自 `ls -la ~/.claude/projects/-Users-alice/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa*`）：

```
~/.claude/
├── CLAUDE.md                                   # 全局个人记忆 / project memory
├── settings.json                               # 全局 settings（hooks, model, 等）
├── settings.local.json                         # 项目本地（git-ignored）
├── history.jsonl                               # 所有项目的 prompt 全局索引
├── stats-cache.json                            # 用量统计缓存
├── statsig/                                    # Anthropic statsig（功能旗）
├── plugins/                                    # 安装的 plugin/skill marketplace
├── skills/                                     # 全局 skill
├── projects/
│   ├── -Users-alice/                             # 编码后的 cwd
│   │   ├── aaaaaaaa-...........jsonl           # 一个 session = 一个文件
│   │   ├── aaaaaaaa-............/              # 同名子目录（仅 v2.1.x 起出现）
│   │   │   ├── subagents/                      # 该 session 启动的子 agent transcript
│   │   │   │   ├── agent-a01f7a96030eca02a.jsonl
│   │   │   │   └── agent-a01f7a96030eca02a.meta.json   # {"agentType":"general-purpose","description":"..."}
│   │   │   └── tool-results/                   # 大尺寸 tool 输出外置文件
│   │   │       └── b8b0vtpu8.txt
│   │   ├── b9feb527-...........jsonl
│   │   ├── memory/                             # 当前项目的二级 memory（CLAUDE.md 引用）
│   │   │   └── *.md
│   │   └── （历史项目里还可能有）sessions-index.json   # 已废弃，不要依赖
│   └── -Users-alice-Desktop-0205-----/
│       ├── sessions-index.json                 # 老版本残留
│       └── 1877e4d3-3a8c-4e99-9e8a-714b288228e5/
│           └── subagents/
│               ├── agent-aprompt_suggestion-0ddb75.jsonl  # 老式：suggestion subagent
│               └── ...
├── todos/                                      # 早期 todo 持久化（v2.0 时代）
│   └── *-agent-*.json                          # 大多内容是 "[]"
├── tasks/                                      # TaskCreate/Update 持久化
│   └── <session-id>/
│       ├── 1.json                              # 单个任务（id 对应 jsonl 里 toolu_xxx 引用）
│       ├── 2.json
│       └── .lock
├── file-history/                               # /rewind-files 的快照
│   └── <session-id>/
│       └── <hash>@v<n>                         # 文件版本号
├── sessions/                                   # 当前活跃进程指针
│   └── <pid>.json                              # {"pid":41672,"sessionId":"...","status":"busy"}
├── session-env/                                # 预分配空目录，目前未使用
├── plans/                                      # Plan mode 留痕（实测当前为空）
├── shell-snapshots/                            # 启动时快照 PATH/alias
├── telemetry/                                  # 上传缓冲
├── backups/                                    # 升级前自动备份
├── debug/                                      # 调试日志
└── cache/, paste-cache/                        # 各种缓存
```

`/private/tmp/claude-501/-Users-alice/<session-id>/tasks/*.output` 还存着 background bash command 的实时 stdout，由 `TaskOutput` 工具读取。

### 1.2 项目目录名编码（cwd → encoded path）

[bin] 真实算法（最小化代码反推）：

```js
function PY(cwd) {
  let s = cwd.replace(/[^a-zA-Z0-9]/g, "-");
  if (s.length <= 200 /* LhH */) return s;
  return s.slice(0, 200) + "-" + Math.abs(hash32(cwd)).toString(36);
}
```

要点：
- 把 **所有非 ASCII 字母数字字符**（包括 `/`、空格、Unicode）替换为 `-`，**不做转义**，因此 `/Users/alice/Desktop/小红书图文裂变` → `-Users-alice-Desktop-----`（4 个汉字塌成 4 个 `-`，再加上前面的 3 个 `-`）。这就是为什么本机能看到 `-Users-alice-Desktop-----` 这种诡异目录名。
- **冲突可能性**：两个名字不同但在 `[^a-zA-Z0-9]` 之外没有差别的目录会撞名（如 `Desktop/小红书图文裂变` 和 `Desktop/影视台风` 都成 `-Users-alice-Desktop-----`），但在长度 ≤ 200 的情况下 CC **不会附加 hash**，因此生产环境真的存在 stale collision；查资料时要拿 jsonl 内每条 `cwd` 字段为准。
- 长度 > 200 时才追加 base36 hash；hash 函数是私有 32-bit hash（看上去不是 djb2，但算法等价于一个 stable hash）。
- 解码一定不可逆：从目录名 `-Users-alice-Desktop-----` 无法还原 `小红书图文裂变`，必须打开 jsonl 看 `cwd` 字段。
- 路径会先 `realpath` + `String.normalize("NFC")`，所以 macOS 上的 NFD 形式不会单独出现。

### 1.3 sessionId 与文件名

[disk]：jsonl 文件名格式 `<UUIDv4>.jsonl`。`UUIDv4` 校验正则（[bin]）：

```
/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
```

- 文件名 = 该 session 的 `sessionId`（jsonl 内每行的 `sessionId` 字段也是这个）。
- 但**不是绝对的**：CC 内部对 compaction 有特殊处理，存在 jsonl 文件里**前几行的 `sessionId` 是上一段会话的 ID**，第一次出现 `compact_boundary` 之后才切换成本文件名对应的 ID。这是 [web]（fsck.com 2026-02 那篇博文）说的，但本仓库的当前所有 session 都没看到这种「prefix 抄录前文 + 切换 sessionId」的现象，**只在 v2.1.131 以前的版本出现**[unverified-on-this-machine]。
- `--session-id <uuid>` CLI 标志在交互模式下只设 API 上报 ID，**不**控制本地文件名（仍然由 CLI 自己生成 UUID）；只有 `claude -p --session-id ...` 非交互模式下，磁盘文件名才会真的等于该 UUID（[web] kentgigger.com）。

### 1.4 Session 生命周期

| 时点 | 行为 |
|------|------|
| 启动 | 生成 UUIDv4，**立刻** 创建 `<id>.jsonl`（空）。`~/.claude/sessions/<pid>.json` 写一条 `{pid, sessionId, cwd, status:'busy'}` |
| 第一条 prompt | jsonl 追加：① 一行 `permission-mode`（如果跟默认不同）② 一行 `file-history-snapshot`（初始为空 trackedFileBackups）③ 一行 `user` |
| 模型推理 | jsonl 每收到一段流式响应都追加 `assistant` 行；中间可能附 `system / subtype=turn_duration` |
| 工具调用 | tool_use 内联在 assistant 的 `message.content` 数组里；tool_result 在下一行作为 `user.message.content` 数组的一项 |
| compact 触发 | 写一行 `system / subtype=compact_boundary`，紧跟一行带 `isCompactSummary:true` 的 user 行（伪造） |
| 长时间无操作 | `system / subtype=away_summary` 写入回顾摘要 |
| 退出 | 不会写「END」标记。`~/.claude/sessions/<pid>.json` 被删 |
| `/resume` 同一文件 | **可以继续 append**。新的 user/assistant 行会接在最后一行后面，`parentUuid` 指向旧的最后一条（不是 null） |

**结论**：jsonl 是 monotonic append-only，但同一 session 文件可以被多次会话续写——这意味着翻译器把整文件理解成 DAG 是更稳的，不要把它当成「单次会话顺序数组」。

### 1.5 关于 `sessions-index.json` ——为什么不可信

观察到的事实：
- 频繁活跃的 `-Users-alice/` 目录里 **完全没有 sessions-index.json**[disk]。
- 几个早期项目（`-Users-alice-Desktop-clawdbot`, `-Users-alice-Desktop-0205-----`）里有 `sessions-index.json`，但内容只反映 2026-01 ~ 02 的 session，再之后写入的 jsonl 完全没被索引[disk]。

[bin] 里的真相：CC 当前的 session 列表枚举走的是 `Gb5() / xRH()`：直接 `readdir(<project-dir>)`，把 `*.jsonl` 全列出来；用 `head/tail KX=64KB` 读每个文件的头尾来恢复 metadata（`cwd / gitBranch / firstPrompt / customTitle / summary`）。索引文件根本不在调用链里。

→ **翻译器永远要扫目录，绝不要读 sessions-index.json**。

### 1.6 全局 `~/.claude/history.jsonl`

[disk] 字段：

```json
{"display":"prompt text","pastedContents":{},"timestamp":1768538017564,"project":"/Users/alice/Desktop/影视台风","sessionId":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}
```

- 一行一条 prompt，记录所有项目的输入历史。
- `project` 是**未编码的真实 cwd**，可作为 encoded 目录名 → 真实路径的反查表。
- `pastedContents` 通常 `{}`，有粘贴大文本时是 `{ "<id>": "<truncated>" }`。
- 翻译器可拿来做 "用户在哪个 cwd 输入了哪些 prompt" 的全局检索，但**不要用它做对话重建**——它没有 assistant 端。

### 1.7 `~/.claude/sessions/<pid>.json`

[disk] 当前活跃进程注册表，每个 CC 进程一个文件，进程退出时删除：

```json
{
  "pid": 41672,
  "sessionId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "cwd": "/Users/alice",
  "startedAt": 1778055179185,
  "procStart": "Wed May  6 08:12:27 2026",
  "version": "2.1.131",
  "peerProtocol": 1,
  "kind": "interactive",
  "entrypoint": "cli",
  "status": "busy",
  "updatedAt": 1778061021592
}
```

`status` 取值 `busy | idle`。这是 CC 用来防止两个进程并发写同一个 jsonl 的锁机制（结合 `peerProtocol`）。翻译器**不需要**关心，但要知道：CC 进程仍在运行时去 `--resume` 该 session 会被拒。

---

## 2. JSONL line schema

### 2.1 全部出现过的 `type` 值

[disk] 在本机 55 个 session 全量扫描得到的 type 直方图：

| type | 含义 | 是否有 `parentUuid` | 是否进入对话上下文 |
|------|------|-------------------|------------------|
| `user` | 用户消息（含 tool_result） | 有 | 是 |
| `assistant` | 模型消息（含 text/thinking/tool_use） | 有 | 是 |
| `system` | harness 元事件（多种 subtype） | 有 | 部分（仅某些 subtype） |
| `attachment` | 附加上下文（skill 列表 / 文件 / nested memory 等） | 有 | 是（拼到下一回合 user 前） |
| `file-history-snapshot` | 文件版本快照（rewind 用） | 否（只引用 messageId） | 否 |
| `permission-mode` | session 级权限模式切换 | 否 | 否（但会影响后续 tool_use） |
| `queue-operation` | 后台 task 通知队列变更 | 否 | 是（enqueue 内容会注入下一回合） |
| `last-prompt` | 最后一条 user prompt 的快捷指针 | 否 | 否（UI 用） |
| `ai-title` | LLM 生成的会话标题 | 否 | 否 |
| `custom-title` | 用户手设标题（`/title`） | 否 | 否 |
| `agent-name` | 当前 agent 别名 | 否 | 否 |
| `summary`（旧） | leaf 摘要 ([web] piebald.ai)。本机 v2.1.131 已不出现 | 否（用 `leafUuid`） | 否 |

注意：`compact_boundary` 不是顶层 type，而是 `type=system, subtype=compact_boundary`。同样还有几个 system subtype。

### 2.2 公共字段（绝大多数行都有）

```jsonc
{
  "uuid": "f2c14050-db96-4f3b-be08-46de61e29e8d",     // 本行的 UUID
  "parentUuid": "3eb754ac-cf0c-4a08-89dc-e0d91ab57b32", // 上一行；DAG 边
  "isSidechain": false,                                  // true 表示这是 subagent 的内部对话
  "type": "...",
  "timestamp": "2026-05-06T08:14:53.390Z",              // ISO 8601
  "sessionId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "cwd": "/Users/alice",
  "gitBranch": "HEAD",
  "version": "2.1.131",                                  // 写这条记录的 CC 版本
  "userType": "external",                                // external | internal
  "entrypoint": "cli"                                     // cli | sdk | web | ide
}
```

### 2.3 `user` 行

#### 2.3.1 真实用户输入（content 是字符串）

[disk] 例：

```json
{
  "parentUuid": null,
  "isSidechain": false,
  "promptId": "5da17b06-6beb-4197-8d30-21b11b76c5dc",
  "type": "user",
  "message": {
    "role": "user",
    "content": "找一下有没有靠谱的可以让不同的 agent..."
  },
  "uuid": "3eb754ac-cf0c-4a08-89dc-e0d91ab57b32",
  "timestamp": "2026-05-06T08:14:47.238Z",
  "permissionMode": "bypassPermissions",
  "userType": "external",
  "entrypoint": "cli",
  "cwd": "/Users/alice",
  "sessionId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "version": "2.1.131",
  "gitBranch": "HEAD"
}
```

- `promptId` 唯一标记一次 user-initiated turn；同一个 promptId 可对应多条 message（比如带附件时 user 行会拆出 attachment 行也带 promptId）。
- `permissionMode` 一旦改过会一直跟随。
- 第一条 user 行的 `parentUuid` 是 `null`，但**也可能不是**——再次 `/resume` 同一文件追加时，新 user 行会指回旧的 leaf。

#### 2.3.2 工具结果回执（content 是数组，含 `tool_result`）

```json
{
  "parentUuid": "1da537a1-1d3a-48ed-8c17-f70a869129e3",
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {
        "type": "tool_result",
        "tool_use_id": "toolu_01N2Rhyq9FYMbKWGaNMvyD4Q",
        "content": "No skills directory found",
        "is_error": false
      }
    ]
  },
  "uuid": "...",
  ...
}
```

- `content` 字段允许 **string** 或 **数组**。
- 数组项 `tool_result` 的 `content` 自身又是 string 或 `[{type:"text"|"image", ...}]`。
- 错误时 `is_error: true`，且 `content` 通常被 `<tool_use_error>...</tool_use_error>` 包裹。

#### 2.3.3 系统注入的 user-text（skill 调用 / `[Request interrupted by user]`）

```json
{
  "type":"user",
  "message":{"role":"user","content":[{"type":"text","text":"# Update Config Skill\n..."}]}
}
```

这是 Skill 工具被 invoke 后由 harness 插入的：把 skill 的 markdown 全文当成一条用户消息塞进对话。Codex 端没有内置 skill 概念，翻译时这类内容**不能丢**（否则模型不知道为什么自己突然有这段语境），可以转换为 `developer` role 或合并到下一条 user prompt。

#### 2.3.4 Compact summary 伪造 user

[web] + [bin]：

```json
{
  "type":"user",
  "isCompactSummary":true,
  "isVisibleInTranscriptOnly":true,
  "parentUuid":"<compact-boundary-uuid>",
  "message":{"role":"user","content":"This session is being continued from a previous conversation that ran out of context.\n... <长摘要> ..."}
}
```

翻译器要决定：作为「真实 user prompt」一并送出（重启对话语义），还是用 Codex 的 sentinel 作为压缩点。

### 2.4 `assistant` 行

[disk] 标准例：

```json
{
  "parentUuid":"...",
  "isSidechain":false,
  "message":{
    "model":"claude-opus-4-6",
    "id":"msg_01DKmxN65iaSyiTzShiKP7kV",
    "type":"message",
    "role":"assistant",
    "content":[
      {"type":"thinking","thinking":"...","signature":"<base64-blob>"},
      {"type":"text","text":"..."},
      {"type":"tool_use","id":"toolu_01...", "name":"Bash","input":{...},"caller":{"type":"direct"}}
    ],
    "stop_reason":"tool_use",
    "stop_sequence":null,
    "stop_details":null,
    "usage":{
      "input_tokens":3,
      "cache_creation_input_tokens":27535,
      "cache_read_input_tokens":0,
      "output_tokens":575,
      "server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},
      "service_tier":"standard",
      "cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":27535},
      "inference_geo":"",
      "iterations":[],
      "speed":"standard"
    }
  },
  "type":"assistant",
  "uuid":"f2c14050-db96-4f3b-be08-46de61e29e8d",
  "timestamp":"...",
  "userType":"external",
  "entrypoint":"cli",
  "cwd":"/Users/alice",
  "sessionId":"...",
  "version":"2.1.131",
  "gitBranch":"HEAD",
  // 子 agent 时还有：
  // "agentId":"a01f7a96030eca02a",
  // "attributionAgent":"general-purpose"
}
```

字段要点：

- `message.id` 是 Anthropic API 返回的 `msg_xxx`；同一个流式响应多个分块写到不同 jsonl 行时**会复用同一个 `id`**（[disk] 已观察）。
- `message.content` 是数组，每个 block 必须有 `type`。允许的 block type：
  - `text`：`{type:"text", text:"..."}`
  - `thinking`：`{type:"thinking", thinking:"...", signature:"..."}`（Claude extended thinking；signature 由 Anthropic 签名以防伪造，Codex 没有等价物）
  - `tool_use`：`{type:"tool_use", id:"toolu_...", name:"Bash", input:{...}, caller:{type:"direct"|"agent", ...}}`
  - `redacted_thinking`：`{type:"redacted_thinking", data:"<bytes>"}`（[doc]，本机未观察到）
  - `image`：assistant 端罕见，但允许
  - `server_tool_use` / `web_search_tool_result`：当模型用了内置 server tool（不必走 tool_use 流程，比如某些 web search）时出现 [unverified-on-disk]
- `stop_reason`：`end_turn | tool_use | max_tokens | stop_sequence | pause_turn | refusal`。
- `usage` 与 Anthropic API 完全一致；CC 在 jsonl 里**全量保留**，可以反推每回合 cost。
- `caller` 字段是 CC 专属，告诉 harness 这次工具调用来自 main agent (`{type:"direct"}`) 还是某个 sub-agent。
- 当 isSidechain=true（子 agent transcript）时，会多 `agentId` 和 `attributionAgent` 字段。

### 2.5 `system` 行 ——核心子类型

#### 2.5.1 `subtype=turn_duration`

```json
{"type":"system","subtype":"turn_duration","durationMs":42742,"messageCount":24,...}
```
仅供 UI / telemetry，可丢弃。

#### 2.5.2 `subtype=compact_boundary`（最重要）

```json
{
  "parentUuid": null,
  "logicalParentUuid": "51d94f79-9686-4462-9a1c-b86b02276783",
  "isSidechain": false,
  "type": "system",
  "subtype": "compact_boundary",
  "content": "Conversation compacted",
  "isMeta": false,
  "timestamp": "2026-04-14T13:51:44.913Z",
  "uuid": "0b7a4ca4-e073-4e72-b6f6-b76c5bf7db14",
  "level": "info",
  "compactMetadata": {
    "trigger": "auto",         // 或 "manual"
    "preTokens": 171705,
    "postTokens": 8473,
    "durationMs": 133190
  },
  ...
}
```

要点：
- `parentUuid: null`——重置链。`logicalParentUuid` 才是真正的上一条，CC 重建对话时用它做「桥」。
- 之后立即跟一条 `user` 行带 `isCompactSummary:true` + `isVisibleInTranscriptOnly:true`。
- `compactMetadata.trigger`：`auto`（接近上限自动触发）或 `manual`（用户输入 `/compact`）。
- 还有 `compactMetadata.preservedSegment: boolean`（[bin]，`hasPreservedSegment` 字段）——`/compact <内容>` 时用户可以自定义保留段，这个字段就是标志位 [unverified-on-disk]。
- 一个文件里**可以有多个** boundary（[web] 一例最长 21 小时 5 个 boundary）。

#### 2.5.3 `subtype=api_error`

```json
{
  "type":"system","subtype":"api_error","level":"error",
  "error":{"status":503,"headers":{},"requestID":null,"error":{...},"type":"new_api_error"},
  "retryInMs":558.94,"retryAttempt":1,"maxRetries":10,
  ...
}
```
重试日志，丢弃即可。

#### 2.5.4 `subtype=away_summary`

```json
{"type":"system","subtype":"away_summary","content":"Looking for the transcript ..."}
```
长时间无操作时 harness 让模型生成的「我刚刚在做什么」摘要，UI 上显示为 idle 提示。**不进入下一回合 prompt**[unverified]。

#### 2.5.5 `subtype=local_command`

```json
{"type":"system","subtype":"local_command","content":"<local-command-stdout>...</local-command-stdout>"}
```
slash command 执行结果（如 `/clear` / `/title`）。`<local-command-stdout>` 标签包裹的内容会作为 system 提示注入下一回合。

#### 2.5.6 `subtype=stop_hook_summary`

```json
{
  "type":"system","subtype":"stop_hook_summary",
  "hookCount":1,
  "hookInfos":[{"command":"afplay /System/Library/Sounds/Glass.aiff","durationMs":2422}],
  "hookErrors":[],
  "preventedContinuation":false,
  "stopReason":"",
  "hasOutput":false,
  "level":"suggestion",
  "toolUseID":"547ac5f1-...",
  ...
}
```
Stop hook 执行结果（来自 `~/.claude/settings.json` 的 `Stop` 事件）。`preventedContinuation` 为 true 时表明 hook 阻止了模型继续。

### 2.6 `attachment` 行

`attachment` 是 CC 把动态上下文贴入对话的方式。`message` 字段没有，只有 `attachment.type` + `attachment.<payload>`。出现位置永远是某个 user prompt 之后、模型回复之前——它们和 user prompt 共同构成模型这一回合的「输入」。

实测全部 10 种 attachment 子类型：

| `attachment.type` | 触发时机 | 关键字段 |
|---|---|---|
| `skill_listing` | 每次新对话 / 启动时 | `content`：所有可用 skill 的 markdown 列表（即 `<system-reminder>` 里你看到的那段） |
| `nested_memory` | 当前 cwd 命中 CLAUDE.md / project memory | `path`、`content.path`、`content.type`（`User`\|`Project`）、`content.content`（md 全文） |
| `task_reminder` | 每个 turn 前 | `content`（数组：未完成 TaskCreate 任务）、`itemCount` |
| `file` | 用户在 prompt 里 `@file.md` 引用 | `filename`、`content`：完整文件子结构（text/image/notebook/pdf 都用同一个 schema） |
| `compact_file_reference` | compact 后保留对文件的简短指针 | `filename`、`displayPath` |
| `edited_text_file` | 用户在 IDE 端编辑过的文件提示 | `filename`、`snippet`（带行号） |
| `command_permissions` | 用户用 `/permissions` 改了允许工具集 | `allowedTools`：`["Bash","Read",...]` |
| `queued_command` | 用户在模型推理时已在输入框排了下一条 prompt | `prompt`、`commandMode`：`prompt`\|`command` |
| `date_change` | 跨过了 0 点 | `newDate`：`"2026-04-15"` |
| `invoked_skills` | Skill tool 被调用后 | `skills:[{name, path:"bundled:claude-api", content:"<完整 SKILL.md>"}]` |

翻译要点：
- **不能直接丢弃**。把它们 flatten 成等价的 `developer` / `system` / `user` 文本是最低成本的方案；其中 `nested_memory`、`skill_listing`、`task_reminder`、`invoked_skills` 是模型决策的关键依据。
- `content` 字段经常很大（MB 级），翻译时考虑截断或外置。

### 2.7 `file-history-snapshot`

```json
{
  "type":"file-history-snapshot",
  "messageId":"3eb754ac-cf0c-4a08-89dc-e0d91ab57b32",
  "snapshot":{
    "messageId":"3eb754ac-...",
    "trackedFileBackups":{
      "/Users/alice/foo.py":{"hash":"0ed4d0613fe19259","version":"v3"},
      ...
    },
    "timestamp":"2026-05-06T08:14:47.239Z"
  },
  "isSnapshotUpdate":false
}
```

- 不带 `parentUuid`，**不参与对话 DAG**。
- `messageId` 关联到某条 user/assistant uuid。
- `trackedFileBackups` 的实际版本字节内容存于 `~/.claude/file-history/<sessionId>/<hash>@v<n>`。
- **用途**：`/rewind-files` 命令可以让 CC 恢复到任意快照对应的文件状态。
- **翻译时**：可以丢弃；Codex 没有等价 rewind，最多在 git 层模拟。

### 2.8 `permission-mode`

```json
{"type":"permission-mode","permissionMode":"bypassPermissions","sessionId":"..."}
```

可选值：
- `default`：每次工具调用都问
- `acceptEdits`：自动同意 Edit/Write
- `plan` / `plan-mode`：在 Plan mode 中只读
- `bypassPermissions` / `dangerouslySkipPermissions`：全自动，**不再询问**
- `dontAsk`：等价 acceptEdits？[unverified]

[bin] 中实际枚举：`"acceptEdits" | "auto" | "bypassPermissions" | "default" | "dontAsk" | "plan"`。

翻译时这是会话级状态，需要映射到 Codex 的 `--full-auto` / `--auto-edit` / 默认人工 confirm。

### 2.9 `queue-operation`

```json
{"type":"queue-operation","operation":"enqueue","timestamp":"...","sessionId":"...","content":"<task-notification>...</task-notification>"}
{"type":"queue-operation","operation":"remove","timestamp":"..."}
```

- `operation:"enqueue"` 时 `content` 会作为下一回合 user 消息前的 system-reminder 注入（典型用途：后台 Bash 任务完成通知）。
- `operation:"remove"` 是消费完毕。
- 设计上对应一个 FIFO 队列；翻译 → Codex 时把 enqueue 的 content **合并到下一个 user turn 前**即可。

### 2.10 `last-prompt`

```json
{"type":"last-prompt","lastPrompt":"找一下...","leafUuid":"d33e6280-...","sessionId":"..."}
```

- 这是一个**指针快照**：方便 UI 快速定位「最近一次 prompt 是哪一条」、「DAG 的 leaf 是哪个 uuid」。
- 不进入对话上下文，可丢弃。
- `leafUuid` 与老式 `summary` 行里的 `leafUuid` 同义。

### 2.11 `ai-title` / `custom-title` / `agent-name`

```json
{"type":"ai-title","aiTitle":"Find shared task session memory solution for multiple agents","sessionId":"..."}
{"type":"custom-title","customTitle":"start ohing","sessionId":"..."}
{"type":"agent-name","agentName":"start ohing","sessionId":"..."}
```

- LLM 自动生成（`ai-title`）或用户 `/title` 设置（`custom-title`）。`agent-name` 是 multi-agent 模式下当前 agent 的可读名。
- 同一个文件可以多次出现 `ai-title`（每隔一段时间重新打标题）。

### 2.12 旧式 `summary` 行（v2.0 时代）

```json
{"type":"summary","summary":"LSP Tool Demo: Getting Document Symbols","leafUuid":"f3b3be9e-..."}
```

[web] piebald.ai。本机扫描 55 个 session 没看到，但若需要兼容老 jsonl 必须支持读。

### 2.13 DAG 重建算法（翻译器必须实现）

```python
def build_conversation(project_dir):
    by_uuid = {}
    for jsonl in glob(f"{project_dir}/*.jsonl"):
        for line in open(jsonl):
            ev = json.loads(line)
            if "uuid" in ev:
                by_uuid[ev["uuid"]] = ev

    # 找出所有 leaf
    parents = {ev.get("parentUuid") for ev in by_uuid.values() if ev.get("parentUuid")}
    leaves  = [ev for u, ev in by_uuid.items() if u not in parents]

    # 用户选择某条 leaf 后，沿 parentUuid 反向走到 null
    def chain_to(leaf):
        seq, cur = [], leaf
        while cur:
            seq.append(cur)
            p_uuid = cur.get("parentUuid")
            # compact_boundary 会把链断开，要走 logicalParentUuid
            if p_uuid is None and cur.get("subtype") == "compact_boundary":
                p_uuid = cur.get("logicalParentUuid")
            cur = by_uuid.get(p_uuid) if p_uuid else None
        return seq[::-1]
    return [chain_to(l) for l in leaves]
```

要点：
- `isSidechain=true` 的行**不能**进主链——它们属于子 agent transcript。要么忽略，要么单独建图。
- compact_boundary 之前的所有消息严格上**已不再活跃于上下文**，只作为审计；翻译目标 harness 不一定要回放它们。
- `isCompactSummary:true` 的 user 行是**伪造**的，回放时必须保留——它包含了被舍弃前文的摘要。

### 2.14 隐藏类型（[bin] 提到但 [disk] 未观察）

- `attribution-snapshot`：[bin] 里有 `Buffer.from('{"type":"attribution-snapshot"')` 检测代码，可能是用来标记「上面这段 message 应当归属给哪个 agent」的内部记录。在本机 v2.1.131 jsonl 里没出现。
- `dream` / `auto-dream`：[bin] 里有 `consolidate-lock` 与 `task_dream` 状态，但属于 background consolidation task，没看到落到 jsonl[unverified]。

---

## 3. 内置工具清单

调研三层证据：
1. [bin] 全量列出真正注册到 model 的 tool name 字符串（顶级唯一名称）
2. [disk] 在真实 session 里实际被调用过的 tool name
3. `sdk-tools.d.ts` 文件 ([disk] `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/sdk-tools.d.ts`) — 由 `json-schema-to-typescript` 自动生成，是 Anthropic 官方暴露给 SDK 用户的 schema

### 3.1 工具一览表

| 名称（jsonl 中的 `name`） | userFacingName ([bin]) | 在哪可用 | 我们当前 session 里的可见性 |
|---|---|---|---|
| `Agent` | `Task` | 任意时刻；spawn subagent | ✓ |
| `AskUserQuestion` | `AskUserQuestion` | 总是 | ✓ |
| `Bash` | `Bash` / `PowerShell`（Windows） | 总是 | ✓ |
| `CronCreate` | `CronCreate` | 启用 routines feature flag 时 | ✗（feature gate 未启用） |
| `CronDelete` | `CronDelete` | 同上 | ✗ |
| `CronList` | `CronList` | 同上 | ✗ |
| `Edit` | `Edit` | 总是 | ✓ |
| `EnterPlanMode` | — | 任意时刻 | ✗ |
| `EnterWorktree` | `EnterWorktree` | 任意时刻 | ✓ |
| `ExitPlanMode` | `ExitPlanMode` | Plan mode 中 | ✗ |
| `ExitWorktree` | `ExitWorktree` | 在 worktree 内 | ✓ |
| `Glob` | `Search` | 总是 | ✗（系统提示没列，但模型可调用——见 §3.18） |
| `Grep` | `Search` | 总是 | ✗（同上） |
| `MultiEdit` | — | 历史遗留；现版本 [bin] 仍注册但不在系统提示 | ✗ |
| `NotebookEdit` | `REPL` | 文件是 .ipynb 时 | ✓ |
| `Read` | `Read` | 总是 | ✓ |
| `ScheduleWakeup` | — | autonomous loop 启用时 | ✗ |
| `Skill` | `Skill` | 总是；调用已注册 skill | ✓ |
| `TaskCreate` | `TaskCreate` | 总是 | ✓ |
| `TaskGet` | `TaskGet` | 总是 | ✗ |
| `TaskList` | `TaskList` | 总是 | ✗ |
| `TaskOutput` | `TaskOutput` | 当有 background task 时 | ✓ |
| `TaskStop` | — | 当有 background task 时 | ✓ |
| `TaskUpdate` | `TaskUpdate` | 总是 | ✓ |
| `TodoWrite` | — | 历史遗留；被 Task* 取代 | ✗ |
| `WebFetch` | `Fetch` | 总是 | ✓ |
| `WebSearch` | `WebSearch` | 总是 | ✓ |
| `Write` | `Write` | 总是 | ✓ |

[bin] 里还能见到 `SendMessage` / `Slacked` / `TestingPermission` 等，但这些是内部 API（subagent 间通信），不直接暴露给 model 作为 tool。

下面逐个工具给出 input / output schema、副作用、状态语义、翻译建议。

---

### 3.2 `Bash`

**Input schema**（[disk] `sdk-tools.d.ts` BashInput）

```ts
{
  command: string;                 // 必填
  description?: string;            // 5-10 词的人类可读描述（UI 用）
  timeout?: number;                // ms，最大 600000（10min）
  run_in_background?: boolean;     // 后台执行；返回 backgroundTaskId，需要 TaskOutput 取
  dangerouslyDisableSandbox?: boolean; // 跳过 macOS sandbox-exec
}
```

**Output schema**（BashOutput）

```ts
{
  stdout: string;
  stderr: string;
  interrupted: boolean;
  isImage?: boolean;               // 命令直接产出二进制图像（如 screencapture）
  backgroundTaskId?: string;       // 若 run_in_background 或自动 background
  backgroundedByUser?: boolean;    // 用户 Ctrl+B 主动后台
  assistantAutoBackgrounded?: boolean; // 长时间阻塞，harness 自动后台
  dangerouslyDisableSandbox?: boolean;
  returnCodeInterpretation?: string;
  noOutputExpected?: boolean;
  structuredContent?: unknown[];
  rawOutputPath?: string;          // 巨大输出落到文件
  persistedOutputPath?: string;    // 同上，CC 持久化目录
  persistedOutputSize?: number;
  staleReadFileStateHint?: string; // 命令修改了已 Read 过的文件，提示模型重读
  ghRateLimitHint?: string;
}
```

但 jsonl 里的 `tool_result.content` **不是这个对象**——CC 在写入 transcript 时会把 BashOutput 转成扁平的字符串（保留 stdout，stderr 用 `<stderr>` 标记）。例：

```json
{"type":"tool_result","tool_use_id":"...","content":"No skills directory found","is_error":false}
```

或带状态：

```json
{"type":"tool_result","content":"<persisted-output>\nOutput too large (567KB). Full output saved to: /Users/alice/.../tool-results/bo3xjf1s8.txt\n\nPreview (first 2KB):\n   <截断>\n...[TRUNC]\n</persisted-output>"}
```

**副作用**：本机 shell 执行；macOS 上默认会被 sandbox-exec 包裹（限制网络/写路径），除非 `dangerouslyDisableSandbox`。

**状态**：本身无状态，但 `run_in_background:true` 启动的进程注册到 `~/.claude/tasks/<sessionId>/` 与 `/private/tmp/claude-501/.../tasks/<id>.output`，存活于整 session 期间。

**Codex 等价**：`shell` tool（apply_patch 不算）；schema 大体一致但没有 `description` 字段语义、没有 `dangerouslyDisableSandbox`、`run_in_background` 行为不同。翻译时把 description 注释掉即可。

---

### 3.3 `Read`

**Input**（FileReadInput）

```ts
{
  file_path: string;        // 必填，绝对路径
  offset?: number;          // 起始行
  limit?: number;           // 行数
  pages?: string;           // 仅 PDF：如 "1-5"
}
```

**Output**（FileReadOutput）—— 这是 union type：

```ts
| { type:"text", file:{ filePath, content, numLines, startLine, totalLines } }
| { type:"image", file:{ base64, type:"image/jpeg"|"png"|"gif"|"webp", originalSize, dimensions:{ originalWidth, originalHeight, displayWidth, displayHeight } } }
| { type:"notebook", file:{ filePath, cells:[...] } }
| { type:"pdf", file:{ filePath, base64, originalSize } }
| { type:"parts", file:{ filePath, originalSize, count, outputDir } }   // PDF > 一定页数时按页拆出
| { type:"file_unchanged", file:{ filePath } }
```

jsonl 中的 `tool_result.content` 是数组形式，文本时第一项是 `{type:"text", text:"1\t<file content with line numbers>"}`，前面带 `cat -n` 风格行号。图片时 `[{type:"image", source:{type:"base64", data:"..."}}]`。

**副作用**：纯读，但 harness 会把 (file_path → content_hash + mtime) 缓存到 readFileState；后续 Edit/Write 会校验「你 Read 过的版本是否过期」（"You must read the file first" 错误就来自这里）。

**Codex 等价**：没有原生 Read，需用 `cat`/`sed`/Bash；翻译时 unfold 为 Bash 调用更可靠。

---

### 3.4 `Write`

**Input**（FileWriteInput）

```ts
{ file_path: string; content: string; }
```

**Output**（FileWriteOutput）

```ts
{
  type: "create"|"update";
  filePath: string;
  content: string;
  structuredPatch: [{oldStart,oldLines,newStart,newLines,lines:string[]}];
  originalFile: string|null;     // null 时为新建
  gitDiff?: { filename, status:"modified"|"added", additions, deletions, changes, patch, repository? };
  userModified?: boolean;
}
```

jsonl `tool_result.content` 是简短字符串：`"File created successfully at: /path"` 或 `"The file ... has been updated successfully."`。结构化字段不会进 jsonl，仅供 UI 展示。

**Codex 等价**：`apply_patch` 工具（Codex 用 `*** Begin Patch / *** Add File / *** End Patch` DSL）。翻译需要把 Write 转成 apply_patch 的 add-file 段。

---

### 3.5 `Edit`

**Input**（FileEditInput）

```ts
{
  file_path: string;
  old_string: string;
  new_string: string;
  replace_all?: boolean;     // 默认 false
}
```

**Output**（FileEditOutput）

```ts
{
  filePath, oldString, newString, originalFile,
  structuredPatch:[...],
  userModified, replaceAll,
  gitDiff?
}
```

jsonl tool_result 简短：`"The file /path has been updated successfully."`

**校验语义**：
- `old_string` 必须**精确匹配**（含空白、tab、换行）。
- 默认情况下 `old_string` 必须**唯一**（否则报错）。
- 必须先 `Read` 过该文件。

**Codex 等价**：`apply_patch` 的 `*** Update File` + `@@`/`+`/`-` hunk。翻译时拼出最小 unified diff。

---

### 3.6 `Glob`

**Input**（GlobInput）

```ts
{ pattern: string; path?: string; }
```

**Output**（GlobOutput）

```ts
{ durationMs, numFiles, filenames: string[], truncated: boolean }   // 默认上限 100
```

jsonl `tool_result.content` 是换行分隔的文件路径。

**底层**：`fast-glob`。

**Codex 等价**：`shell` 调 `find` 或 `rg --files -g`。

---

### 3.7 `Grep`

**Input**（GrepInput，巨复杂）

```ts
{
  pattern: string;
  path?: string;
  glob?: string;                                       // 文件过滤
  output_mode?: "content"|"files_with_matches"|"count"; // 默认 files_with_matches
  "-A"?: number; "-B"?: number; "-C"?: number;
  context?: number;
  "-n"?: boolean;     // 默认 true
  "-i"?: boolean;
  type?: string;       // rg --type
  head_limit?: number; // 默认 250
  offset?: number;
  multiline?: boolean;
}
```

**Output**（GrepOutput）

```ts
{
  mode?, numFiles, filenames: string[], content?, numLines?, numMatches?,
  appliedLimit?, appliedOffset?
}
```

**底层**：`/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/<arch>/rg`（[disk] 路径在错误信息里见过）。Bun 在 macOS 单文件模式下若打包遗漏 ripgrep 子进程会报 ENOENT（本机历史 jsonl 里见过）。

**Codex 等价**：直接用 `rg`，参数基本一一映射。

---

### 3.8 `WebFetch`

**Input**（WebFetchInput）

```ts
{ url: string;  /* http(s)，不允许 user:pass@host */ prompt: string; }
```

**Output**（WebFetchOutput）

```ts
{ bytes, code, codeText, result, durationMs, url }
```

**关键限制**：
- 由 `api.anthropic.com/api/web/domain_info` 决定 host 是否被允许（[bin] 中的 `eE7` 函数）。企业代理屏蔽时直接报「Unable to verify if domain ... is safe to fetch」。
- 重定向只跨同一 host 才允许；登录页一律失败。
- HTML 会先 turndown → markdown，再把 prompt 与内容拼一起送给一个轻量模型做归纳，jsonl 里只保留模型摘要。

**Codex 等价**：Codex 没有 builtin WebFetch；需用 Bash 调 `curl` + 让 model 自行解析。

---

### 3.9 `WebSearch`

**Input**（WebSearchInput）

```ts
{ query: string; allowed_domains?: string[]; blocked_domains?: string[]; }
```

**Output**（WebSearchOutput）

```ts
{
  query: string;
  results: ( {tool_use_id, content:[{title, url}]} | string )[];   // 第一项通常是 model 解释
  durationSeconds: number;
}
```

走 Anthropic server-side `web_search_20250305` server-tool，结果直接由 server 决定。

**Codex 等价**：无；通常翻译为「丢弃，提醒模型自己用 curl + 公开 search API」。

---

### 3.10 `NotebookEdit`

**Input**

```ts
{
  notebook_path: string;
  cell_id?: string;
  new_source: string;
  cell_type?: "code"|"markdown";
  edit_mode?: "replace"|"insert"|"delete";   // 默认 replace
}
```

**Output**

```ts
{ new_source, cell_id?, cell_type, language, edit_mode, error?, notebook_path, original_file, updated_file }
```

**Codex 等价**：手动改 ipynb（ipynb 是 JSON，可以用 apply_patch / Bash）。

---

### 3.11 `Agent`（也叫 Task tool 内部名）

**Input**（AgentInput）

```ts
{
  description: string;             // 3-5 词
  prompt: string;
  subagent_type?: string;          // "general-purpose" 或自定义
  model?: "sonnet"|"opus"|"haiku";
  run_in_background?: boolean;
  name?: string;                   // 可被 SendMessage 寻址
  team_name?: string;
  mode?: "acceptEdits"|"auto"|"bypassPermissions"|"default"|"dontAsk"|"plan";
  isolation?: "worktree";
}
```

**Output**（AgentOutput）—— union：

```ts
// 同步完成时：
{
  agentId, agentType?, content:[{type:"text", text:"<最终 markdown 报告>"}],
  totalToolUseCount, totalDurationMs, totalTokens, usage:{ ... },
  toolStats?:{ readCount, searchCount, bashCount, editFileCount, linesAdded, linesRemoved, otherToolCount },
  status: "completed",
  prompt: string
}
// 异步启动：
{
  status:"async_launched",
  agentId, description, prompt,
  outputFile: "/private/tmp/claude-501/<proj>/<sess>/tasks/<agentId>.output",
  canReadOutputFile?: boolean
}
```

**关键事实**：
- 主 session jsonl **不**内联子 agent transcript。子 transcript 在 `~/.claude/projects/<proj>/<sess>/subagents/agent-<agentId>.jsonl`，和 meta 文件 `<...>.meta.json` 一起。
- 主 session 里只看到 `Agent` tool_use 和它的 `tool_result`——后者要么是同步报告（`content:[{type:"text",text:"..."}]`）要么是「Async agent launched」字符串告示。
- 子 transcript 自己也是完整 jsonl（同样结构），其每行都有 `isSidechain:true` + `agentId`。
- 子 agent 完成后通常会触发主 session 的 `queue-operation:enqueue` + `<task-notification>` block 来通知主线。

**翻译**：Codex 没有原生 subagent。可降级为：把 `Agent` 调用 inline 成一个 markdown block 「请扮演 X 子 agent，做 Y 任务」并直接执行；或者把 subagent transcript 完整 inline 进主对话。

---

### 3.12 `Skill`

**Input**: `{ skill: string }`（仅一个字符串）

**Output 形态**：
- 成功时 harness 会**直接把 skill 的 SKILL.md 全文当作一条 `user` text content** 注入对话（即 §2.3.3 描述的那种 user-text）。**不是** tool_result——它是 user message。同时记录一行 `attachment / type=invoked_skills`，里面 `skills:[{name, path:"bundled:claude-api", content:"<SKILL.md 全文>"}]`。
- 失败（skill 名不存在）：`{type:"tool_result", is_error:true, content:"<tool_use_error>Unknown skill: web-access</tool_use_error>"}`

**含义**：Skill 是 CC 对「上下文加载」的封装，不是真正在副 process 里执行；它就是把一个 markdown 文件粘到对话中、并允许其中声明 `allowed-tools` 等元数据。

**Codex 等价**：把 SKILL.md 作为 system prompt 追加（Codex 有 AGENTS.md 机制类似）。

---

### 3.13 `EnterPlanMode` / `ExitPlanMode`

**EnterPlanMode**: 切换到 plan-only 模式（屏蔽 Edit/Write/Bash 等）。

**ExitPlanMode Input**（ExitPlanModeInput）

```ts
{
  allowedPrompts?: [{ tool:"Bash", prompt:"run tests"|"install dependencies"|... }];
  // strict object false: 允许其它字段（保留扩展性）
}
```

**ExitPlanMode Output**

```ts
{
  plan: string|null;
  isAgent: boolean;
  filePath?: string;            // 计划被存到磁盘的路径
  hasTaskTool?: boolean;
  planWasEdited?: boolean;      // 用户是否在 CCR Web UI 编辑过
  awaitingLeaderApproval?: boolean;
  requestId?: string;
}
```

**实测**：本机 `~/.claude/plans/` 当前为空。Plan 写到磁盘的具体路径是 `<filePath>` 字段提示给模型。Plan mode 进出是会话级状态，对应 `permission-mode:plan`。

**Codex 等价**：Codex 也有 plan 概念但更轻；翻译时一般退化为「连续 read-only Bash 操作 + 生成 markdown 计划」。

---

### 3.14 `EnterWorktree` / `ExitWorktree`

**EnterWorktree Input**

```ts
{ name?: string; path?: string; }   // 只能二选一
```

**EnterWorktree Output**

```ts
{ worktreePath, worktreeBranch?, message }
```

**ExitWorktree Input**

```ts
{ action:"keep"|"remove"; discard_changes?: boolean; }
```

**ExitWorktree Output**

```ts
{ action, originalCwd, worktreePath, worktreeBranch?, tmuxSessionName?, discardedFiles?, ... }
```

**副作用**：在 git 仓库里创建 `.claude/worktrees/<name>/` 这个 worktree 并切到对应 branch；离开时可保留或删除。

**Codex 等价**：`shell` 直接调 `git worktree`。

---

### 3.15 `TaskCreate` / `TaskUpdate` / `TaskList` / `TaskGet` / `TaskOutput` / `TaskStop`

这是新版 todo / job 体系（替代旧 `TodoWrite`）。

**TaskCreate Input** ([bin] reverse-engineered)

```ts
{
  subject: string;                   // 标题（短）
  description: string;               // 详细
  activeForm?: string;               // 进行时副本（spinner 显示）
  metadata?: Record<string, unknown>;
}
```
**TaskCreate Output**: `{ task: { id: string, subject: string } }`，jsonl 里渲染成 `"Task #1 created successfully: ..."`。

**TaskGet Input**: `{ taskId: string }`
**TaskGet Output**: `{ task: { id, subject, description, status, blocks:[ids], blockedBy:[ids] } | null }`

**TaskUpdate Input**: 实际 schema 较灵活，本机 jsonl 见到 `{ taskId, status:"in_progress" }`。也可改 description / blocks。

**TaskList Input**: 无参或带 filter。

**TaskOutput Input**: `{ task_id: string, block: boolean, timeout: number }`
**TaskOutput Output**: 一段 XML 风格文本：

```xml
<retrieval_status>success</retrieval_status>
<task_id>bo4e02sur</task_id>
<task_type>local_bash</task_type>
<status>completed</status>
<exit_code>0</exit_code>
<output>... (实时 stdout) ...</output>
```

**TaskStop Input**: `{ task_id?: string, shell_id?: string }`（shell_id 已废弃）
**TaskStop Output**: `{ message, task_id, task_type, command? }`

**持久化**：每条任务以 `~/.claude/tasks/<sessionId>/<id>.json` 形式落盘；同名 `.lock` 防并发。任务有 DAG（`blocks` / `blockedBy`），CC 不会让你 start 一个 blockedBy 不空的 task。

**与 Bash background task 的关系**：当 `Bash` 用了 `run_in_background:true`，harness 自动为你创建一个对应的 Task（`task_type:"local_bash"`）。`TaskOutput`/`TaskStop` 既能查 model-driven task 也能查 background bash。

**Codex 等价**：Codex 没有结构化 todo / 后台 task 系统；翻译时建议把 TaskCreate/Update 转换成 markdown checklist（自然语言）；TaskOutput/Stop 直接丢弃或改写成「请等待」。

---

### 3.16 `AskUserQuestion`

**Input**（AskUserQuestionInput，schema 巨大）

简化版：

```ts
{
  questions: [{
    question: string;
    header: string;            // ≤12 chars chip
    multiSelect: boolean;
    options: [{ label, description, preview? }];   // 2-4 个
  }];   // 1-4 个 question
}
```

**Output**（AskUserQuestionOutput）

```ts
{
  questions: [...];           // 回放原 input
  answers: { [questionText: string]: "<label>" | "<csv-of-labels>" };
  annotations?: { [questionText: string]: { preview?, notes? } };
}
```

jsonl 里 result 形如：

```json
{"type":"tool_result","content":"User has answered your questions: \"...\"=\"...\". You can now continue with the user's answers in mind."}
```

**副作用**：阻塞模型，等用户在 UI 选；可以「自由文本回答」。

**Codex 等价**：Codex 没有结构化 question；可降级为普通模型问句 + user 回答。

---

### 3.17 `ScheduleWakeup` / `CronCreate` / `CronList` / `CronDelete`

[bin] 都注册了，但需要 feature flag (`tengu_orchid_mantis` / `tengu_mocha_barista` / `tengu_surreal_dali`) 启用，且需要 `claude.ai` 后端 token（不支持 API key）。

**ScheduleWakeup**: 用于 `/loop` skill：让模型在固定 cron 时间被「叫醒」执行 prompt。Sentinel `<<autonomous-loop-dynamic>>` 可以让 prompt 变成动态指令。

**CronCreate / CronList / CronDelete**: 全功能 cron 调度，存到 claude.ai 远端 routines 服务（即「remote agent」），不全在本地。

**翻译**：Codex 没有等价；丢弃 / 提示用户外部 cron。

---

### 3.18 `MultiEdit`、`TodoWrite` ——废弃但保留

[bin] 仍注册：
- `MultiEdit`：早期版本一次多处编辑工具，现版本的 `Edit` 已支持 `replace_all` + 一次调用，因此 MultiEdit 不再下发到模型 prompt。
- `TodoWrite`：旧 todo schema（`{todos:[{content, status:"pending"|"in_progress"|"completed", activeForm}]}`），整体被 TaskCreate/TaskUpdate 取代，但 `~/.claude/todos/*.json` 目录仍在（多为空 `[]`）。

翻译老 jsonl 时要兼容。

---

### 3.19 MCP 工具

[disk] `sdk-tools.d.ts` 还有 3 个 MCP 相关接口：

```ts
McpInput          = { [k:string]: unknown };
McpOutput         = string;
ListMcpResourcesInput = { server?: string };
ListMcpResourcesOutput = [{ uri, name, mimeType?, description?, server }];
ReadMcpResourceInput = { server, uri };
ReadMcpResourceOutput = { contents:[{ uri, mimeType?, text?, blobSavedTo? }] };
```

实际 jsonl 里 MCP 工具的 `name` 形如 `mcp__<server>__<tool>`，例如 `mcp__workspace__bash`。本机当前没启用 MCP server，故没有真实样本，但 [bin] 中的 alias 表显示：

```js
{ Bash: ["mcp__workspace__bash"], WebFetch: ["mcp__workspace__web_fetch"] }
```

——即 CC 内部允许 MCP server 重写 builtin 工具。翻译器要解析任何 `mcp__*` 前缀的 tool_use，至少把它们透传。

---

### 3.20 在系统提示里**没列**但实际存在的工具

本对话当前的系统提示包含：Agent、AskUserQuestion、Bash、CronCreate/Delete/List、Edit、EnterPlanMode、EnterWorktree、ExitPlanMode、ExitWorktree、NotebookEdit、Read、ScheduleWakeup、Skill、TaskCreate/Get/List/Output/Stop/Update、WebFetch、WebSearch、Write。

但 [disk] 真实 session 还看到：
- **Glob**（55 个 session 中至少 5 个用到）
- **Grep**（同上）

可能性：`Glob` 和 `Grep` 在某些版本 / 某些「team」配置里被自动注入到 system prompt，但不在当前我们看到的版本里；模型从训练数据中知道有这俩工具，调用时 harness 也接受。

→ **翻译器必须为这些「未文档化但客观存在」的工具准备 schema**。

---

## 4. System prompt 与运行时环境

### 4.1 不写入 jsonl 的部分

系统提示**不会**作为单独一行存在 jsonl 中。`type=system, subtype=...` 是事件而非 prompt。这是与 Codex 的根本差异——Codex 把 system prompt 写在 `meta` line 里。

→ 翻译时必须**自己重建** CC 系统提示。

### 4.2 系统提示的拼接来源

[bin] + 当前对话观察，CC 启动时按以下顺序拼接：

1. **静态前缀**（model identity、安全声明）—— 由 CC 二进制硬编码，与 Anthropic API 文档里的 [Claude Code system prompt] 模板一致 [doc]。
2. **环境块**（动态，每次 turn 注入）：
   ```
   Here is useful information about the environment you are running in:
   <env>
   Working directory: <cwd>
   Is directory a git repo: yes/no
   Platform: <os>
   OS Version: <版本>
   Today's date: <YYYY/MM/DD>
   </env>
   ```
3. **Memory 块**（来自 CLAUDE.md / project memory）—— 这块走 `attachment / type=nested_memory`，拼到第一个 user prompt 前。
4. **可用工具描述**：每个工具的 input/output schema + description（也可以是 [doc] 提到的 Anthropic-API tool definitions）。
5. **Skills listing**：`<system-reminder>` 包裹的可用 skill 列表，走 `attachment / type=skill_listing`。
6. **Permission rules**：从 `settings.json` 拼出（`allow`/`deny` 列表会作为系统提示一部分注入）。
7. **MCP 服务器描述**（如果有）：每个 server 提供的工具描述。
8. **Hook 注入**：`SessionStart` hook 的 stdout 会作为 system message 加入。

### 4.3 `<system-reminder>` 块来源

观察这次对话开头：

```
<system-reminder>
The following skills are available for use with the Skill tool:
- feishu-doc-writer: ...
- gstack: ...
...
</system-reminder>
```

[disk] 它对应一行 `attachment / type=skill_listing`，但**渲染**到模型前会被包成 `<system-reminder>...</system-reminder>` 标签——这是 CC 的 prompt 工程：让模型分清「这是 harness 注入的、不是用户说的」。同样标签也用于：

- `<system-reminder>` for queued_command, task_reminder, command_permissions
- `<env>` for environment block
- `<task-notification>` for queue-operation enqueue
- `<local-command-stdout>` for slash command output

[bin] 中明确：当模型自己输出 `<system-reminder>` 这串文字时，harness 会校验它是不是合法插入位置；模型不允许伪造。

### 4.4 系统提示在每个 turn 是否重发

[doc] + Anthropic API 行为：是的，每次 API 调用都重发完整 system prompt。但 CC 利用 prompt caching（`cache_control:"ephemeral"`），把 system 部分标记为 5min/1h 缓存，因此 token 计费上等价于「只发一次」。

[disk] `assistant.usage.cache_creation_input_tokens` / `cache_read_input_tokens` 真实显示：第一次 system prompt 创建缓存（27535 tokens），后续 turn 全部命中（cache_read=27535）。

→ 翻译时不要漏掉系统提示——**Codex 重新启动时如果不复刻它，模型会以为没有任何 tool 可调用，行为完全错乱**。

### 4.5 哪些部分 per-session 变化

| 部分 | 静态/动态 | 源 |
|---|---|---|
| 静态前缀 | 跟随 CC 版本 | 二进制 |
| `<env>.cwd` | 每次 launch 变 | `process.cwd()` |
| `<env>.Today's date` | 每天变 | `Date.now()`，跨 0 点会写一行 `attachment/date_change` |
| `<env>.Platform` / `OS Version` | 平台变 | `os.platform()` |
| Memory（CLAUDE.md） | 用户编辑随时变 | `~/.claude/CLAUDE.md` + project `CLAUDE.md` |
| Skills listing | 安装/卸载时变 | 扫描 `~/.claude/skills/`、`~/.claude/plugins/.../skills/` |
| Tool schemas | CC 升级时变 | 二进制 |
| Permission rules | `/permissions` 时变 | `settings.local.json` |
| MCP servers | 配置变 | `settings.json.mcpServers` |

---

## 5. Resume 机制

### 5.1 触发方式

[doc] CLI flags：
- `claude` 单独执行 → 新建 session
- `claude --resume` → 弹交互菜单选 session
- `claude --resume <session-uuid>` → 直接 resume 指定 session
- `claude -p --resume <id> "继续做..."` → 非交互
- `claude --continue` / `claude -c` → 直接 resume **当前目录最近一个** session

[doc][web]：`--session-id` 是另一回事（telemetry ID，不影响磁盘）。

### 5.2 Resume 时 CC 在做什么

[bin] + [web] 实际流程：

1. `realpath(cwd)` + `normalize("NFC")` 算出 `encoded_dir = PY(cwd)`；候选目录 `~/.claude/projects/<encoded_dir>/`。
2. 如果 cwd 在某 git worktree 里，再去解析所有 worktree 的 encoded path 加进候选。
3. `readdir` 候选目录，列出所有 `.jsonl`，对每个：
   - 校验文件名是否合法 UUIDv4（regex `jj4`）
   - 用 `VN_(filePath)` 读 head/tail 64KB，从 head 解析 `cwd`、`gitBranch`、`firstPrompt`、`createdAt`，从 tail 解析 `customTitle`、`tag`、`summary`
   - 跳过 `tool_result` / `isMeta:true` / `isCompactSummary:true` 行寻找真正的 firstPrompt
   - 这步**完全不解析整个文件**——因此 50MB 大 session 也能秒列。
4. 用户选择或指定后，把整个 jsonl **顺序读入**，构建对话上下文：
   - 跳过纯 UI 的行（ai-title、custom-title、agent-name、last-prompt、queue-operation、permission-mode、file-history-snapshot、turn_duration、stop_hook_summary、api_error）
   - **保留**：user、assistant、attachment、compact_boundary、isCompactSummary user、所有有效的 tool_use/tool_result。
   - compact_boundary 之前的内容**会被截断**——CC 只把 boundary 后段（含 isCompactSummary 摘要）作为活动上下文。
5. 把恢复后的对话作为新一轮的 input，**完整重发**给 Anthropic API（依赖 prompt cache 命中节省钱）。
6. 用户输入新 prompt 后，新行被 append 到**同一个** jsonl。

### 5.3 Resume 需要的 side files

- 必需：`<sessionId>.jsonl` 本身。
- 几乎必需：`~/.claude/projects/<dir>/<sessionId>/subagents/*` 如果对话里有 `Agent` 调用且模型可能继续操作子 agent。
- 可选：
  - `~/.claude/projects/<dir>/<sessionId>/tool-results/*.txt` —— 大 tool 输出。模型在 jsonl 中能看到 preview，但要拿全文需 Read。
  - `~/.claude/tasks/<sessionId>/*.json` —— 当对话引用了仍未完成的任务时，CC 会读取它来 inject 到 task_reminder attachment。
  - `~/.claude/file-history/<sessionId>/*` —— /rewind-files 用，对话本身不读。
  - `~/.claude/projects/<dir>/memory/*.md` —— 如果对话里 attachment 类型 `nested_memory` 引用了它，CC 会重新读最新内容（**变化的会反映**——这是 Memory 比对话更"活"的关键）。
- **不需要**：sessions-index.json、permissions、worktree state（resume 时会重新协商）。

### 5.4 能否手写 jsonl 让 CC resume？

**理论上可以**，约束如下（[bin] 校验）：

1. 文件名必须是合法 UUIDv4。
2. 至少有一行能被 head/tail 解析为含 `"type":"user"` 或 `"type":"assistant"` 的 JSON——`C1H()` 函数否则视为空文件。
3. 第一条非 meta 行必须能给出 firstPrompt 和 cwd——CC list 时跳过 isCompactSummary、tool_result、isMeta 行去找。
4. 每行必须是合法 JSON（无尾随逗号 / BOM）。
5. UUID 链不能断（否则 DAG 重建出多个 leaf，但 CC 会把每个 leaf 当独立 conversation 显示——这反而是 hack 钩子）。
6. `parentUuid` 必须 in-file 存在或为 null/known。

**已知验证项（必填字段否则报错）**：
- user/assistant 行至少要有 `uuid`、`type`、`message.role`、`message.content`、`sessionId`、`timestamp`、`cwd`。
- `message.id`（assistant）建议有，但 CC 会兼容缺失。
- `version` 是版本兼容字段，建议写最新。

**风险点**：
- 翻译生成的 thinking block 没有合法 `signature`，模型在 resume 时虽然能读，但下一次想继续 thinking 可能会被 Anthropic API 拒（API 会校验 signature）。**对策**：删掉所有 thinking block，只保留 text / tool_use / tool_result。
- assistant 的 `message.id` 一旦伪造，再次 cache hit 时可能导致 cache key 错位（性能下降，但功能 OK）。

→ **结论**：手写 jsonl 让 CC resume 是可行的，**只要避免 thinking 与重复 message.id**。这是 Codex→CC 翻译方向的核心可行性。

---

## 6. 特殊场景

### 6.1 Subagent

如 §3.11 所述：

```
主 session jsonl
└── 一行 assistant {tool_use: Agent, input:{prompt,...}}
└── 一行 user {tool_result: "<最终 markdown>" 或 "Async agent launched..."}

主 session 子目录
└── subagents/agent-<agentId>.jsonl   ← 完整子对话 transcript
└── subagents/agent-<agentId>.meta.json ← {agentType, description}
```

子 jsonl 每行：
- `isSidechain: true`
- `agentId: "<id>"`
- 其余结构与主 session 完全相同（user/assistant/attachment/file-history-snapshot 等都可能出现）
- 子 agent 也可以再 spawn 子子 agent，无限嵌套

异步模式下：
- `Agent.input.run_in_background:true` 启动后立即返回 `status:"async_launched"`，jsonl 主线继续。
- 异步 agent 完成时往主 session **append 一条 queue-operation:enqueue**，content 是 `<task-notification>...<status>completed</status>...</task-notification>`，下一回合 user prompt 前以 `<task-notification>` 形式注入。

**翻译策略**：
- **保留 + 内联**：把子 transcript 的最终输出 inline 到主对话；丢弃中间步骤。
- **保留 + 引用**：在新 harness 里创建对应的子对话文件并保留指针。
- **降级为单线**：把所有 subagent 调用展开成主线 markdown「我先去做了 X，结果是 Y」。

### 6.2 Skills

如 §3.12：
- `tool_use: Skill` + `tool_result` 两行，加上一条 `attachment / type=invoked_skills`。
- 同时还会塞一条 `user` 消息内容是 SKILL.md。
- Skills 列表在每次 session 启动时塞一条 `attachment / type=skill_listing`。

**翻译要点**：
- Skill 内容是 markdown + 可选 frontmatter（YAML：`name`, `description`, `allowed-tools`, `model`, ...）。
- 大多数 skill 的 markdown 主体描述了 Claude Code-specific 工作流（提到 Bash、Skill、TaskCreate 等），跨 harness 时要 rewrite 成中性描述。
- 如果目标 harness（Codex）没 Skill 概念，就把 SKILL.md 的内容并入 system prompt 或 AGENTS.md。

### 6.3 Plan mode

进入：tool_use `EnterPlanMode`（无参） → permission-mode 切到 `plan`。
退出：tool_use `ExitPlanMode` → output `{ plan, isAgent, filePath?, planWasEdited?, awaitingLeaderApproval? }`。

文件痕迹：
- `<filePath>` 通常是 `~/.claude/plans/<id>.md` 但本机当前 plans 目录为空，所以可能要等下次进 plan mode 才能确认 [unverified]。
- 期间所有 tool_use 都被限制为只读（无 Edit/Write/Bash 写操作），这是 harness 行为，不在 schema 里强制。

**翻译**：Codex 没有 plan mode；翻译时降级为「请只读探查 + 输出 plan markdown」，并把权限切换记录在 metadata 里以便回译。

### 6.4 Hooks

[doc] hook payload schema：

```jsonc
{
  "session_id": "...",
  "transcript_path": "/path/to/<sessionId>.jsonl",
  "cwd": "/Users/alice",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse|PostToolUse|PreCompact|PostCompact|Stop|SubagentStop|Notification|SessionStart|UserPromptSubmit",
  // event-specific 字段：
  "tool_name": "Bash",
  "tool_input": {...},
  "tool_result": {...},      // PostToolUse only
  "user_prompt": "...",      // UserPromptSubmit
  "reason": "...",           // Stop / SubagentStop
  "agent_id": "...",          // 子 agent 上下文里
  "agent_type": "..."
}
```

Hook 在 jsonl 里的痕迹：
- `system / subtype=stop_hook_summary` 行（hook 完成后写）
- 如果 hook stdout 不空且 event 是 `UserPromptSubmit` 或 `SessionStart`，stdout 会作为 system content 注入下一回合（但仍不是 jsonl 单独一行）。

**翻译**：hooks 是用户配置层面的，迁移时建议**不翻译**（让用户在新 harness 里自己重配）；jsonl 里的 stop_hook_summary 也可以丢弃。

### 6.5 MCP servers

如 §3.19：
- jsonl 中 `assistant.message.content[].name` 形如 `mcp__<server>__<tool>`。
- `tool_result.content` 通常是 string；若 server 返回二进制资源，会带 `blobSavedTo` 路径。
- list/read MCP resources 走 `ListMcpResourcesInput` / `ReadMcpResourceInput`。

**翻译**：MCP 是开放协议，Codex 也支持 MCP。如果两端都连同一 MCP server，**最干净的策略是直接保留 mcp__ 前缀工具名**（schema 通用）。如果只一端连，就降级为「该工具不可用，请人工介入」。

---

## 7. 翻译损耗清单（CC → 通用 shell agent）

按损耗大小排序：

| CC 特性 | 是否能在 Codex 上还原 | 降级策略 |
|---|---|---|
| `Bash` 简单命令 | ✅ 完全可还原 | 直接映射 `shell` |
| `Read` / `Write` / `Edit` | ✅ 可还原 | 转 apply_patch / shell 组合 |
| `Glob` / `Grep` | ✅ 可还原 | shell 调 `rg` / `find` |
| `WebSearch` / `WebFetch` | ⚠️ 部分还原 | shell 调 curl + 让 model 自己解析；search 没有 builtin |
| `AskUserQuestion` | ⚠️ 部分还原 | 改成自由文本提问 |
| `Skill` | ⚠️ 把 SKILL.md 注入 system prompt | 失去 dynamic skill listing |
| `Agent`（同步 subagent） | ⚠️ 把子 transcript inline | 失去并发隔离 |
| `Agent`（异步 subagent） | ❌ 大部分失败 | Codex 没有后台 subagent；只能改成串行 |
| Extended thinking blocks | ❌ 不可还原 | 删掉，模型继续运转可，但 chain-of-thought 会丢 |
| `EnterPlanMode` / `ExitPlanMode` | ⚠️ 可降级 | 转换为只读探查 + 输出 plan markdown |
| `EnterWorktree` / `ExitWorktree` | ✅ 可还原 | shell 调 `git worktree` |
| `NotebookEdit` | ✅ 可还原 | 直接改 ipynb JSON |
| `TaskCreate/Update/...` | ⚠️ 失结构 | 翻成 markdown checklist |
| `TaskOutput` / `TaskStop` (background bash) | ⚠️ 失语义 | 删除工具调用，串行化 |
| `ScheduleWakeup` / `Cron*` | ❌ 不可还原 | 提示用户 |
| `MCP` 工具 | ✅ 可保留 | 同协议 |
| `attachment / nested_memory` | ✅ 易还原 | 把 CLAUDE.md 内容塞进 system prompt 或 AGENTS.md |
| `attachment / skill_listing` | ⚠️ 半还原 | 改成纯文本 |
| `attachment / task_reminder` | ⚠️ 半还原 | 改成 system message 列出当前 todo |
| `attachment / queued_command` | ✅ 可还原 | 当作下一回合 user input |
| `attachment / date_change` | ✅ 可还原 | 注入 `<env>` 块 |
| `attachment / file` (大文件) | ⚠️ 注意 token | 截断 + 提示 |
| `attachment / image` (FileReadOutput.image) | ⚠️ 看 model 多模态能力 | base64 透传 |
| `compact_boundary` | ⚠️ 截断或保留摘要 | 把 isCompactSummary 转为「之前对话摘要：...」 |
| `permission-mode` | ✅ 可还原 | 映射到 Codex 的 `--full-auto` 等 |
| `last-prompt` / `ai-title` / `custom-title` | ✅ 可丢 | 元数据 |
| `file-history-snapshot` / rewind | ❌ 不可还原 | 没 Codex 等价 |
| Hook traces (stop_hook_summary 等) | ✅ 可丢 | 用户重配 |
| `caller` field on tool_use | ⚠️ 信息丢失 | 但这只是 metadata，模型不会看 |

---

## 8. 仍未解决的问题（Open questions）

1. **`compactMetadata.preservedSegment` 字段的具体形态**——`hasPreservedSegment:boolean` 在 [bin] 里出现，但本机 jsonl 没看到非 false 的样本。需要触发一次 `/compact <preserve text>` 才能验证。
2. **`attribution-snapshot` 行的真正用途**——[bin] 中检测它，但本机 v2.1.131 jsonl 没出现。可能是 `--agent` 模式 / multi-agent team 的特征；需要在 enable team 时复测。
3. **`dream` task 的磁盘表现**——[bin] 提到 auto-consolidation 锁文件 `~/.claude/projects/<dir>/.consolidate-lock` 但本机未出现。
4. **`message.id` 重发时的精确语义**——多行同 id 的现象是 streaming 分块？或一次 turn 中 retry？需要在小 session 上断点验证。
5. **`-p --session-id <new-uuid>` 是否会创建新文件**——[web] 说会，但具体写入路径与是否复用 history.jsonl 的 sessionId 字段未实测。
6. **`permission-mode` 的 `dontAsk` vs `acceptEdits`** 差异——[bin] 中两者都列出，行为差别从代码片段不易反推；需 UI 实测。
7. **跨 file 续写**：[web] 提到 v2.0 时代会出现「同 project 不同 jsonl，前缀拷贝旧 session 内容」的现象，但本机扫描 55 个 v2.1 jsonl 没找到。可能 v2.1.131 已经改成「单文件 append-only + compact_boundary」模型。需要老存档对照确认。
8. **Sub-sub-agent 嵌套深度限制**——[bin] 没看到显式限制，但 Anthropic API 有一层 inference geo / iterations cap，跨 harness 时是否能绕过？
9. **Skill 的 `path:"bundled:claude-api"`** 中的 `bundled:` scheme——这指 CC 二进制内置的 skill。其它 scheme 包括 `~/.claude/skills/<name>/SKILL.md`、plugin 路径等，但确切 prefix 集合未列举完整。
10. **MCP tool 的 `tool_result.content` schema**——本机无样本，schema 仅来自 d.ts 文件，未验证 jsonl 里是否完全一致。

---

## 9. 速查表 ——最常用 jsonl line shape

为了让翻译器代码能直接抄写：

```jsonc
// ---------- USER（普通文本） ----------
{
  "uuid":"<u>", "parentUuid":"<u or null>", "type":"user",
  "sessionId":"<s>", "timestamp":"<iso>", "cwd":"...", "version":"2.1.131",
  "userType":"external", "entrypoint":"cli", "gitBranch":"...",
  "promptId":"<u>",                              // 可选
  "permissionMode":"default",                    // 可选
  "isSidechain":false,
  "message":{ "role":"user", "content":"<text>" }
}

// ---------- USER（携带 tool_result） ----------
{
  "uuid":"<u>", "parentUuid":"<u>", "type":"user", ...,
  "message":{
    "role":"user",
    "content":[
      { "type":"tool_result", "tool_use_id":"toolu_...", "content":"<string or array>", "is_error":false }
    ]
  }
}

// ---------- ASSISTANT ----------
{
  "uuid":"<u>", "parentUuid":"<u>", "type":"assistant", ...,
  "message":{
    "model":"claude-opus-4-6", "id":"msg_...", "role":"assistant", "type":"message",
    "content":[
      { "type":"thinking", "thinking":"...", "signature":"<base64>" },     // 可选
      { "type":"text", "text":"..." },                                       // 可选
      { "type":"tool_use", "id":"toolu_...", "name":"<ToolName>", "input":{...}, "caller":{"type":"direct"} }
    ],
    "stop_reason":"end_turn|tool_use|...",
    "usage":{ "input_tokens":..., "output_tokens":..., "cache_creation_input_tokens":..., "cache_read_input_tokens":..., "service_tier":"standard", ... }
  }
}

// ---------- COMPACT BOUNDARY ----------
{ "uuid":"<u>", "parentUuid":null, "logicalParentUuid":"<u>",
  "type":"system", "subtype":"compact_boundary", "content":"Conversation compacted",
  "compactMetadata":{"trigger":"auto","preTokens":171705,"postTokens":8473,"durationMs":133190},
  "level":"info", ... }

// ---------- COMPACT SUMMARY (synthetic user) ----------
{ "type":"user", "isCompactSummary":true, "isVisibleInTranscriptOnly":true,
  "parentUuid":"<boundary-u>", "uuid":"<u>",
  "message":{"role":"user","content":"This session is being continued..."}, ... }

// ---------- ATTACHMENT (skill_listing) ----------
{ "type":"attachment", "parentUuid":"<u>", "uuid":"<u>", ...,
  "attachment":{ "type":"skill_listing", "content":"- skill1: ...\n- skill2: ..." } }

// ---------- ATTACHMENT (nested_memory) ----------
{ "type":"attachment", ...,
  "attachment":{ "type":"nested_memory", "path":"/Users/alice/.claude/CLAUDE.md",
                  "content":{"path":"...","type":"User","content":"<full md>"} } }

// ---------- ATTACHMENT (file) ----------
{ "type":"attachment", ...,
  "attachment":{ "type":"file", "filename":"...", "content":{"type":"text","file":{"filePath":"...","content":"...","numLines":...,"startLine":1,"totalLines":...}} } }

// ---------- ATTACHMENT (invoked_skills) ----------
{ "type":"attachment", ...,
  "attachment":{ "type":"invoked_skills",
                  "skills":[{"name":"claude-api","path":"bundled:claude-api","content":"<SKILL.md>"}] } }

// ---------- FILE HISTORY SNAPSHOT ----------
{ "type":"file-history-snapshot", "messageId":"<u>",
  "snapshot":{"messageId":"<u>","trackedFileBackups":{...},"timestamp":"..."},
  "isSnapshotUpdate":false }

// ---------- PERMISSION MODE ----------
{ "type":"permission-mode", "permissionMode":"bypassPermissions", "sessionId":"..." }

// ---------- QUEUE OPERATION ----------
{ "type":"queue-operation", "operation":"enqueue", "timestamp":"...",
  "sessionId":"...", "content":"<task-notification>...</task-notification>" }
{ "type":"queue-operation", "operation":"remove", "timestamp":"...", "sessionId":"..." }

// ---------- TITLES ----------
{ "type":"ai-title",      "aiTitle":"...", "sessionId":"..." }
{ "type":"custom-title",  "customTitle":"...", "sessionId":"..." }
{ "type":"agent-name",    "agentName":"...", "sessionId":"..." }

// ---------- LAST PROMPT ----------
{ "type":"last-prompt", "lastPrompt":"...", "leafUuid":"<u>", "sessionId":"..." }

// ---------- SYSTEM SUBTYPES（按需） ----------
{ "type":"system","subtype":"turn_duration","durationMs":..., "messageCount":..., ... }
{ "type":"system","subtype":"away_summary","content":"..." }
{ "type":"system","subtype":"api_error","level":"error","error":{...},"retryInMs":...,"retryAttempt":1,"maxRetries":10 }
{ "type":"system","subtype":"local_command","content":"<local-command-stdout>...</local-command-stdout>" }
{ "type":"system","subtype":"stop_hook_summary","hookCount":1,"hookInfos":[...],"hookErrors":[],"preventedContinuation":false,"toolUseID":"..." }

// ---------- 旧版 SUMMARY ----------
{ "type":"summary", "summary":"...", "leafUuid":"<u>" }
```

---

## 10. 翻译器实现建议（最小实现路径）

1. **Stage 1 — 单文件 → 通用 IR**：忽略 sidechain，截断到最近一个 compact_boundary，保留所有 user / assistant + tool_use + tool_result + 关键 attachment（nested_memory、file、invoked_skills、queued_command）。
2. **Stage 2 — 工具映射**：维护一张 `cc_tool → codex_equivalent` 的表（见 §7）。Bash/Edit/Write/Read/Glob/Grep 用最小损耗映射；Skill/Agent/Task/Plan/Cron 必要时降级。
3. **Stage 3 — 系统提示重建**：在新 harness 里重建一份等价 system prompt（CC 模板 + memory + tool schema + skill listing），不要试图照抄 jsonl。
4. **Stage 4 — 落盘**：写出 Codex 的目标 jsonl 格式（参见 `/Users/alice/Desktop/agent-bridge/docs/codex-harness.md`，由另一个 agent 产出）。
5. **Stage 5 — 反向回译**：保留 metadata（cwd、gitBranch、permissionMode、agentId、原 tool_use_id），方便 Codex → CC 回译时把信息塞回去。

**最关键的两条不变量**：
- 不删除 `tool_use` 与对应 `tool_result` 的配对（删掉一边模型会崩）。
- 不删除 `compact_boundary` 后紧跟的 `isCompactSummary:true` user 行（删掉模型上下文断层）。

---

## Sources

主要来源：

- [Anthropic Claude Code 官方文档 — Sessions](https://code.claude.com/docs/en/sessions)（被本机网络拒绝直接访问，但内容由 [doc] 间接引用）
- [Anthropic Claude Code 官方文档 — Hooks](https://code.claude.com/docs/en/hooks)
- [Anthropic Claude Code 官方文档 — `.claude` 目录](https://code.claude.com/docs/en/claude-directory)
- [How Claude Code Session Continuation Works — fsck.com (2026-02-22)](https://blog.fsck.com/releases/2026/02/22/claude-code-session-continuation/) — compact_boundary、isCompactSummary、跨文件续写
- [Messages as Commits: Claude Code's Git-Like DAG — Piebald Blog](https://piebald.ai/blog/messages-as-commits-claude-codes-git-like-dag-of-conversations) — DAG 模型、leafUuid、summary 行
- [Inside Claude Code: The Session File Format — Yi Huang on Medium](https://databunny.medium.com/inside-claude-code-the-session-file-format-and-how-to-inspect-it-b9998e66d56b)
- [Unlocking Your Claude History, Part 1 — Raymond E Peck III on Medium](https://medium.com/@raymondpeck/unlocking-your-claude-history-part-1-f19000c05655)
- [What Actually Happens When You Run `/compact` — DEV Community](https://dev.to/rigby_/what-actually-happens-when-you-run-compact-in-claude-code-3kl9)
- [Dive into Claude Code — VILA-Lab arXiv paper / GitHub](https://github.com/VILA-Lab/Dive-into-Claude-Code)
- [simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — JSONL → HTML 工具
- [swyxio/claude-compaction-viewer](https://github.com/swyxio/claude-compaction-viewer/)
- [withLinda/claude-JSONL-browser](https://github.com/withLinda/claude-JSONL-browser)
- [daaain/claude-code-log](https://github.com/daaain/claude-code-log)
- [thejud/claude-history](https://github.com/thejud/claude-history)
- [jhlee0409/claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer)
- [kentgigger.com — How to resume, search, manage Claude Code conversations](https://kentgigger.com/posts/claude-code-conversation-history)
- [stevekinney.com — Claude Code Session Management](https://stevekinney.com/courses/ai-development/claude-code-session-management)
- [GitHub anthropics/claude-code Issue #33912 — `claude --resume <id>` returns "No conversation found"](https://github.com/anthropics/claude-code/issues/33912)
- [GitHub anthropics/claude-code Issue #44607 — No way to access session ID from within a running session](https://github.com/anthropics/claude-code/issues/44607)
- [Anthropic / claude-code repo — hook-development SKILL.md](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/hook-development/SKILL.md)
- [Anthropic API Docs — Tool use, Extended thinking, Prompt caching](https://docs.anthropic.com/) (依赖 cache_control / signature 字段)

直接磁盘 / 二进制证据：

- 本机 `~/.claude/` 目录（55 个 v2.1.x jsonl session, 共 ~700MB transcripts）
- `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/sdk-tools.d.ts` (v2.1.131)
- `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` (Mach-O, 217MB Bun 单文件包)
