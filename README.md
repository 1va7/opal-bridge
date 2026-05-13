# opal-bridge

> Part of [**OPAL**](https://github.com/1va7/opal) (**O**pen **P**ortable **A**ctivity **L**ayer) — the cross-agent CLI session translator subsystem.
>
> **main after v0.6.0** — title sync across CC↔Codex twins, Codex picker/search DB title fixes, no duplicate files on rename. See [CHANGELOG.md](CHANGELOG.md) for full version history.

跨 agent 的 session 翻译与 resume 桥。

## 要解决的问题

不同 agent harness（Claude Code / Codex / Hermes / 公司内部 agent）各自把 session 存成自己的 jsonl 格式，且 tool schema 互不兼容。导致一个项目在某一个 agent 里做到一半，无法在另一个 agent 里 `--resume` 接着干，只能把人工摘要重新粘进去。

## 方案

中间走 canonical session format，每个 agent 配一对 adapter：

- `ingest`: 该 agent 的 session jsonl → canonical
- `render`: canonical → 该 agent 可被 `--resume` 加载的 session 文件

## Quick start

```bash
# 安装
python3 -m venv .venv && .venv/bin/pip install -e .

# 列出最近的 CC session
.venv/bin/python -m agent_bridge.cli list -n 10

# 双向翻译 + 一键 smoke 验证
# CC → Codex：
.venv/bin/python -m agent_bridge.cli smoke \
    ~/.claude/projects/<encoded-cwd>/<UUIDv4>.jsonl \
    --prompt "Reply with: WORKS"
# Codex → CC：
.venv/bin/python -m agent_bridge.cli smoke --from codex \
    ~/.codex/sessions/YYYY/MM/DD/rollout-...UUIDv7.jsonl \
    --prompt "Reply with: WORKS"

# 仅翻译（不 smoke），双向都支持：
.venv/bin/python -m agent_bridge.cli translate \
    --from claude-code --to codex \
    --subagent-strategy inline \
    ~/.claude/projects/<encoded-cwd>/<UUIDv4>.jsonl

.venv/bin/python -m agent_bridge.cli translate \
    --from codex --to claude-code \
    ~/.codex/sessions/YYYY/MM/DD/rollout-...UUIDv7.jsonl

# 拿到结果后直接复制粘贴对应 resume 命令：
codex exec resume <UUIDv7> "你的新指令"          # 翻成 Codex
claude --resume <UUIDv4> -p "你的新指令"          # 翻成 CC（在原 cwd 下）

# 历史补同步 / 修复旧的短镜像：
.venv/bin/python -m agent_bridge.cli sync \
    --direction both --days 365 --include-active --force
```

## 当前能力（main after v0.6.0）

✅ **双向翻译 + 自动镜像 + 共享标题** — 完整版本历史见 [CHANGELOG.md](CHANGELOG.md)

- **双向 CC ↔ Codex**：live `claude --resume` / `codex resume` 都验证通过
- **共享标题**：在 CC 或 Codex 任一边重命名 session，对面 picker 自动跟进；CC `custom-title` / `agent-name` 会成为 Codex picker 的可读名称；不再产生重复文件
- **Codex 搜索标题修复**：强制重渲染时会把 `session_index.jsonl` 里的用户标题写回 `state_5.sqlite`，避免 session 仍在但搜索不到
- **自动镜像**：CC `Stop` hook + Codex `notify` hook，每段对话结束自动同步到对面；或用 `agent-resume watch` 守护进程
- **历史修复同步**：`sync --force --include-active --days 365` 可重渲染旧镜像，修复 hook 未运行期间留下的短 context
- **空会话降噪**：没有 replayable context 的 Codex/CC 源不会生成空镜像；已生成的空镜像会被移除
- **MCP server**：`agent-resume mcp serve` 暴露 6 个工具给任意 MCP host（Claude Desktop / Cursor / Cline / …）
- **6 核心工具映射**：Bash / Read / Glob / Grep / WebSearch / 大部分 metadata
- **apply_patch 双向**：CC Edit/Write/MultiEdit ↔ Codex apply_patch grammar，多 op envelope 自动拆为多个 canonical ToolCall
- **subagent inline**：自动扫 `<sess>/subagents/`，按 description 匹配后拼进主线
- **compact_boundary 双向**：CC `compact_boundary + isCompactSummary user` ↔ canonical SummaryCompaction ↔ Codex `compacted / context_compaction`
- **shell 命令模式识别**：Codex 端的 `cat -n / sed / head / tail / rg --files` 反向回 canonical Read/Glob，避免 round-trip 退化
- **realpath + NFC**：CC encoded-cwd 与 `claude --resume` 行为一致
- **正确的 picker 显示**：title / mtime / state DB `updated_at` / 助手回复都对齐原始 session 活动时间
- **覆盖保护**：反向渲染到 Claude Code 时，只允许覆盖 agent-bridge 生成的 `[from ...]` 文件，拒绝覆盖真实 CC session
- attachment / skill_listing / nested_memory / file → developer message
- thinking blocks 自动剥离（signature/encrypted_content 跨 harness 不兼容）
- **31 pytest** + live `codex exec resume` + live `claude --resume` 验证

❌ 推迟（见 `specs/`）：
- DAG 多 leaf 选择
- TaskCreate 完整 stateful diff（目前 1:1 映射）
- Plan mode / AskUserQuestion 完整翻译
- Mode B / C fidelity（LLM 摘要）
- Hermes adapter
- 与像素级蒸馏整合

## 文档

- [CHANGELOG.md](CHANGELOG.md) — 版本历史与变更记录
- `docs/ARCHITECTURE.md` — canonical IR 与 src/ 设计
- `docs/tool-mapping.md` — 字段级映射规范（翻译器实现的金本位）
- `docs/claude-code-harness.md` — CC 实证调研
- `docs/codex-harness.md` — Codex 实证调研
- `docs/codex-notify-research.md` — Codex notify hook 调研
- `specs/001-translator-mvp.md` — MVP 范围与验收
- `specs/002-completion.md` — apply_patch / compact_boundary / subagent
- `specs/003-bidirectional.md` — Codex → CC 反向
- `specs/004-mcp-and-hooks.md` — MCP server + 双向 hook
- `data/fixture/PoC_REPORT.md` — 端到端 PoC 验证结果

## 与「像素级蒸馏」的关系

长期方向：canonical store 复用像素级蒸馏的 `events.sqlite` + `thread`，把 tool_calls 作为新的 event 子类型落进去。详见 `docs/ARCHITECTURE.md` §9。MVP 阶段保持独立。

## 阶段路线图

1. **v0.1.0 MVP** ✅：CC → Codex 单向，6 核心工具
2. **v0.2.0 spec 002** ✅：apply_patch、compact_boundary、subagent inline、CLI list/smoke
3. **v0.3.0 spec 003** ✅：反向 Codex → CC
4. **v0.4.0 spec 004** ✅：sync/watch/双向 hook + MCP server
5. **v0.5.0** ✅：picker 可见性、mtime、event_msg 镜像
6. **v0.6.0** ✅：title sync、pair_map、dedupe，无重复
7. **下一步**：Hermes adapter / 与像素级蒸馏整合 / Plan mode 完整支持
