# agent-bridge

跨 agent 的 session 翻译与 resume 桥。

## 要解决的问题

不同 agent harness（Claude Code / Codex / Hermes / 公司内部 agent）各自把 session 存成自己的 jsonl 格式，且 tool schema 互不兼容。导致一个项目在某一个 agent 里做到一半，无法在另一个 agent 里 `--resume` 接着干，只能把人工摘要重新粘进去。

## 方案

中间走 canonical session format，每个 agent 配一对 adapter：

- `ingest`: 该 agent 的 session jsonl → canonical
- `render`: canonical → 该 agent 可被 `--resume` 加载的 session 文件

第一阶段只做 Claude Code ↔ Codex 双向，验证可行性后再扩 Hermes / 公司 agent。

## 与「像素级蒸馏」的关系

长期方向：canonical store 复用像素级蒸馏的 `events.sqlite` + `thread`，把 tool_calls 作为新的 event 子类型落进去。MVP 阶段先独立。

## 阶段

1. **MVP**：本仓库内的离线脚本，CC ↔ Codex 单 session 翻译
2. **集成**：merge 进像素级蒸馏的 ingest，新增 `tool_calls` 表
3. **MCP 化**：起 MCP server，暴露 `resume(session_id, target)` 给所有 agent

## 文档

- `docs/claude-code-harness.md` — CC harness 调研
- `docs/codex-harness.md` — Codex harness 调研
- `docs/tool-mapping.md` — 完整 tool 映射表
- `docs/ARCHITECTURE.md` — canonical 格式与流水线设计
- `specs/` — 各阶段实施 spec
