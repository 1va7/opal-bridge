# agent-bridge

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
```

## 当前能力（截至 spec 003 完成）

✅ **双向**翻译 CC ↔ Codex（live resume 都验证通过）：
- 6 核心工具：Bash / Read / Glob / Grep / WebSearch / 大部分 metadata
- **apply_patch 双向**：CC Edit/Write/MultiEdit ↔ Codex apply_patch grammar，多 op envelope 自动拆为多个 canonical ToolCall
- **subagent inline**：自动扫 `<sess>/subagents/`，按 description 匹配后拼进主线
- **compact_boundary**：双向（CC `compact_boundary` + isCompactSummary user ↔ canonical SummaryCompaction ↔ Codex `compacted` / `context_compaction`）
- **shell 命令模式识别**：Codex 端的 `cat -n / sed / head / tail / rg --files` 反向回 canonical Read/Glob，避免 round-trip 退化
- **realpath + NFC**：CC encoded-cwd 与 `claude --resume` 行为一致
- attachment / skill_listing / nested_memory / file → developer message
- thinking blocks 自动剥离（signature/encrypted_content 跨 harness 不兼容）
- phase（commentary / final_answer）跨 CC line 推断
- 18 个 pytest（含 round-trip）+ live `codex exec resume` + live `claude --resume` 验证

❌ 推迟（见 `specs/`）：
- DAG 多 leaf 选择
- TaskCreate 完整 stateful diff（目前 1:1 映射）
- Plan mode / AskUserQuestion 完整翻译
- Mode B / C fidelity（LLM 摘要）
- MCP server
- Hermes adapter
- 与像素级蒸馏整合

## 文档

- `docs/ARCHITECTURE.md` — canonical IR 与 src/ 设计
- `docs/tool-mapping.md` — 字段级映射规范（翻译器实现的金本位）
- `docs/claude-code-harness.md` — CC 实证调研
- `docs/codex-harness.md` — Codex 实证调研
- `specs/001-translator-mvp.md` — MVP 范围与验收
- `data/fixture/PoC_REPORT.md` — 端到端 PoC 验证结果

## 与「像素级蒸馏」的关系

长期方向：canonical store 复用像素级蒸馏的 `events.sqlite` + `thread`，把 tool_calls 作为新的 event 子类型落进去。详见 `docs/ARCHITECTURE.md` §9。MVP 阶段保持独立。

## 阶段路线图

1. **MVP** ✅：CC → Codex 单向，6 核心工具，本仓 PoC fixture 自动化
2. **002**：反向 Codex → CC、apply_patch 完整支持、compact_boundary、TaskCreate/Update 流转
3. **003**：subagent inline/split 策略、Plan mode、Hermes adapter
4. **MCP 化**：暴露 `resume(session_id, target)` 给所有 agent
