# Spec 001 — MVP Translator (CC → Codex 单向)

> 第一版翻译器的验收规范。范围**有意收窄**：仅 CC → Codex 一个方向、6 个核心工具。验证主路径能跑通后再扩展。
>
> 上游依据：
> - `docs/tool-mapping.md` — 字段级 mapping 规则
> - `docs/ARCHITECTURE.md` — canonical IR 与 src/ 布局
> - `data/fixture/PoC_REPORT.md` — 已验证 codex exec resume 接受手写 jsonl

## 1. 范围

### 1.1 In scope

- ingest：Claude Code session jsonl → canonical IR
  - 单文件 mode（不处理 DAG 多 leaf；选最近一个 leaf）
  - 仅 6 个工具：`Bash` / `Read` / `Edit` / `Write` / `Glob` / `Grep`
  - `attachment / nested_memory` 与 `attachment / skill_listing` 转 developer message
  - 其余 attachment、`compact_boundary`、`Skill`、`Agent`、`TaskCreate*`、`Plan`、`Worktree`、`Cron*`：**降级为 developer message**（带翻译者注释）或丢弃
  - thinking blocks 整段丢弃
- render：canonical IR → Codex sessions jsonl
  - 写到 `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<UUIDv7>.jsonl`
  - SessionMeta + 单 turn_context + 顺序 RolloutItem
  - tool 全部翻成 `function_call name=exec_command` 或 `custom_tool_call name=apply_patch`
- CLI：`agent-resume translate --from claude-code --to codex <session-id>`
- 输出 resume 命令到 stdout，让用户直接复制粘贴

### 1.2 Explicit deferrals

| 推迟项 | 推到哪 | 原因 |
|---|---|---|
| Codex → CC 反向 | 002 | 需要 apply_patch 解析器 |
| 多文件 DAG 合并 | 002 | MVP 单文件够用 |
| Subagent 翻译（任何策略） | 003 | 需要副 jsonl 处理 + 递归 |
| Skill 完整翻译 | 002 | 仅做"降级为 developer notice" |
| `compact_boundary` 翻译 | 002 | 需要测真实 Codex `compacted` 兼容性 |
| `TaskCreate` 流转为 `update_plan` | 002 | 需要 stateful diff |
| `Plan` mode | 003 | 需要 collaboration_mode 切换 |
| Hermes adapter | 010 | 远期 |
| 三档 fidelity（B/C） | 002 | MVP 仅 mode A faithful |
| LLM-based 摘要 | 002+ | 仅在 mode B/C 用 |
| Round-trip / live resume CI | 002 | MVP 手动验证够用 |
| Pixel-distill 集成 | 远期 | 见 ARCHITECTURE §9 |

### 1.3 Out of scope（永远不做或推到很远）

- 翻译成非 jsonl 格式（如 transcript markdown）
- 浏览器 / Web UI
- 实时双向同步（"agent A 在敲，agent B 实时看见"）
- 加密 / 隐私脱敏（用户自己负责 fixture 中不要包含 secret）

## 2. 成功定义（Definition of Done）

MVP 完工的检查清单：

1. **单元测试通过**：
   - `test_canonical_schema.py`：canonical IR 类型校验
   - `test_cc_ingest.py`：fixture 4 个 cc 输入 → IR 正确
   - `test_codex_render.py`：IR → Codex jsonl 字段正确
   - `test_translator_e2e.py`：CC fixture → Codex 输出文件 byte-equiv 期望
2. **可执行 CLI**：`agent-resume translate --from claude-code --to codex <CC_session_id>` 跑通
3. **PoC fixture 自动化**：`data/fixture/cc-input.jsonl` 走 CLI 翻译后能 byte-match `data/fixture/codex-output.jsonl`（已经手翻好的对照样本）
4. **Live resume 手动验证**：跑一次 `codex exec resume <UUID> "say OK"`，能得到 OK
5. **6 个工具 fixture 各一个**：`fixtures/cc-bash.jsonl`、`cc-read.jsonl`、`cc-edit.jsonl`、`cc-write.jsonl`、`cc-glob.jsonl`、`cc-grep.jsonl`，每个翻译都经过单元测试
6. **README 给一段"Quick start"**：3 行 shell 把人引导到第一次 resume

## 3. 实现步骤（建议顺序）

### Step 0 — Project 骨架

- `pyproject.toml`（用 uv 或 hatch）
- `src/agent_bridge/__init__.py`
- `src/agent_bridge/canonical/schema.py`：pydantic v2 dataclass for `Session` + `Moment`
- 跑通 `pip install -e .` + `python -c "import agent_bridge"`

### Step 1 — Canonical IR schema

实现 `agent_bridge.canonical.schema`：
- `Session` dataclass（按 ARCHITECTURE §3.2 字段）
- `Moment` 是 discriminated union，用 pydantic `Field(discriminator='kind')`
- `tool_names.py` 注册 21 个 canonical tool 名（见 ARCHITECTURE §3.4）
- `validate.py`：检查 §11 不变量

校验：写一段最简 canonical session（手敲），用 `Session.model_validate_json` 通过。

### Step 2 — CC ingest

`adapters/claude_code/ingest.py`：

```python
def ingest(jsonl_path: Path, *, follow_subagents=False, fidelity="A") -> Session:
    raw_lines = read_jsonl(jsonl_path)
    raw_lines = filter_drop_types(raw_lines, drop=[
        "permission-mode", "file-history-snapshot", "last-prompt",
        "ai-title", "custom-title", "agent-name",
        "queue-operation",  # MVP 阶段先丢
    ])
    raw_lines = filter_system_subtypes(raw_lines, drop=[
        "turn_duration", "stop_hook_summary", "away_summary"
    ])
    moments = []
    for line in raw_lines:
        moments.extend(translate_line(line))
    return Session(
        id=ulid_new(),
        source_harness="claude-code",
        source_session_id=raw_lines[0]["sessionId"],
        ...,
        moments=sorted(moments, key=lambda m: m["ts"])
    )
```

子模块：
- `tool_map.py`：`Bash` → canonical `shell`，`Edit` → `edit_file`，等
- `attachments.py`：处理 `nested_memory` / `skill_listing`
- 不处理：subagent（设 follow_subagents=False MVP 不递归）、compact_boundary（出现时报 NotImplementedError）

### Step 3 — Codex render

`adapters/codex/render.py`：

```python
def render(session: Session, *, target_dir: Path, fidelity="A",
           subagent_strategy="drop") -> RenderResult:
    uuid = uuid7_str()
    ts = datetime.utcnow()
    out_path = compute_codex_path(target_dir, ts, uuid)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append(make_session_meta(uuid, session, ts))
    lines.append(make_turn_context(session, ts))
    for moment in session.moments:
        lines.extend(moment_to_rollout_items(moment, session))
    write_jsonl(out_path, lines)

    return RenderResult(
        session_id=uuid,
        primary_path=out_path,
        sidecar_paths=[],
        resume_command=f'codex exec resume {uuid} "<your prompt>" -o /tmp/out.md',
        warnings=collect_warnings(session)
    )
```

子模块：
- `tool_map.py`：canonical `shell` → `function_call name=exec_command`，`edit_file` → `custom_tool_call name=apply_patch`（拼 unified diff）
- `apply_patch.py`：实现 Edit/Write/MultiEdit/Delete/Move 转 apply_patch grammar 的字符串拼接
- `rollout_item.py`：`RolloutItem` 类型（与 ARCHITECTURE §3 + tool-mapping §1.2 对齐）
- `path.py`：路径生成

### Step 4 — Translator pipeline

`translator.py`：

```python
def translate(source_path, source_harness, target_harness, target_dir, **opts):
    ingest_fn, _ = ADAPTERS[source_harness]
    _, render_fn = ADAPTERS[target_harness]
    canonical = ingest_fn(source_path, **{k:v for k,v in opts.items() if k in INGEST_OPTS})
    return render_fn(canonical, target_dir=target_dir, **{...})
```

### Step 5 — CLI

`cli.py` 用 typer 或 argparse：

```bash
agent-resume translate --from claude-code --to codex \
  --session-id 0fdd7092-c42c-4263-a7be-0043fee5d776 \
  [--target-dir ~/.codex/sessions]    # 默认就这个
```

输出最后一行：
```
Resume command:
  codex exec resume 019df... "your prompt" -o /tmp/out.md
```

### Step 6 — Tests

每个 step 写完后立刻补对应测试。最低门槛：

- `test_canonical.py`：手敲 IR → 序列化 → 反序列化 → 等
- `test_cc_ingest.py`：6 个 fixture 各 ingest 一遍，断言 moment 数 + 关键字段
- `test_codex_render.py`：IR → Codex jsonl，断言第一行 session_meta、第二行 turn_context、tool_call 字段对齐
- `test_translator_e2e.py`：`data/fixture/cc-input.jsonl` → 翻译输出 byte-equiv `data/fixture/codex-output.jsonl`（顺序与 ts 严格一致）

## 4. 时间预估

总 ~2 工作日（一个有 Python + jsonl 经验的人）：

- Step 0：30 min
- Step 1：3 hr（pydantic schema + 21 个 canonical tools + 不变量）
- Step 2：4 hr（CC ingest + 6 个工具 mapping + attachment）
- Step 3：5 hr（Codex render + apply_patch 拼接最难）
- Step 4：1 hr（pipeline 串起来）
- Step 5：1 hr（CLI）
- Step 6：3 hr（测试）

实际时间预估 1.5x（unknown unknowns）：~3 工作日。

## 5. 接口冻结

**这些字段 / 命名一旦定下来就不要改**（MVP 之后只能加，不能改）：

- canonical schema_version `"1.0.0"`
- `Session` 必填字段：`id`, `source_harness`, `source_session_id`, `cwd`, `started_at`
- `Moment.kind` 枚举（13 种，见 ARCHITECTURE §3.3）
- canonical 工具名 21 项（ARCHITECTURE §3.4）
- 文件后缀：`.canonical.json`
- artifact 子目录：`<id>/artifacts/`

可以变的：
- 字段子集（如 `stats` 内字段）
- 渲染细节（如 turn_context 字段顺序）
- CLI flag 名称
- 测试 fixture 内容

## 6. 风险登记

| 风险 | 缓解 |
|---|---|
| **apply_patch 拼接失败**：Edit 的 old_string 在原文里出现多次或不唯一 | 在 ingest 时把前后 ≤3 行 context 抓出来一起进 IR；render 时 fallback 用 `Delete File + Add File` |
| **Edit 的文件内容不在 fixture 里**：翻译时找不到原文构造 hunk | MVP 强制 `--include-file-snapshots`，要求 fixture 包含 `attachment / file` 行作为内容来源 |
| **CC 同 turn 多 tool_use 时 phase 推断错** | PoC 已验证规则；写测试覆盖 |
| **Codex SQLite 索引污染** | 不写 SQLite，让 Codex 自己 reconcile（per ARCHITECTURE §1.7） |
| **某个 fixture 触发 NotImplementedError**（如含 compact_boundary） | MVP 测试明确说"含此结构的 session 不在 MVP 范围"，给清晰报错 |

## 7. 不做哪些事（reminders）

- ❌ 不写后端（FastAPI / 数据库），纯 CLI + 文件
- ❌ 不调 LLM（mode A faithful 不需要）
- ❌ 不引入 LangChain / langgraph 等高阶框架
- ❌ 不做"超过 5 个 fixture 的覆盖率"（MVP 不是 SDK）
- ❌ 不写 docstring 100%（够用就行）
- ❌ 不做"通用日志 / metrics" 层

## 8. 完成后的下一步

通过验收后立即写 `specs/002-bidirectional.md`：
- Codex → CC 反向
- DAG 多文件合并
- compact_boundary 翻译
- TaskCreate 流转 update_plan
- mode B/C fidelity

---

> 落地节奏建议：Step 0~6 一气呵成，写到哪测到哪。中途若发现 mapping 文档与实际不符（PoC 已暴露 4 处），先修文档再继续——不允许"心里默认"绕过文档。
