# agent-bridge

跨 agent 的 session 翻译与 resume 桥。

## 要解决的问题

不同 agent harness（Claude Code / Codex / Hermes / 公司内部 agent）各自把 session 存成自己的 jsonl 格式，且 tool schema 互不兼容。导致一个项目在某一个 agent 里做到一半，无法在另一个 agent 里 `--resume` 接着干，只能把人工摘要重新粘进去。

## 方案

中间走 canonical session format，每个 agent 配一对 adapter：

- `ingest`: 该 agent 的 session jsonl → canonical
- `render`: canonical → 该 agent 可被 `--resume` 加载的 session 文件

## Quick start (MVP)

```bash
# 安装
python3 -m venv .venv && .venv/bin/pip install -e .

# 翻译一份 CC session 到 Codex
.venv/bin/python -m agent_bridge.cli translate \
    ~/.claude/projects/<encoded-cwd>/<UUIDv4>.jsonl

# 输出会落到 ~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl
# 终端最后一行给出 resume 命令，复制即可：
#   codex exec resume <UUIDv7> "你的新指令"
```

## MVP 范围

✅ 已实现：
- CC → Codex 单向翻译
- 6 个工具：Bash / Read / Glob / Grep / WebSearch / 大部分 metadata
- attachment / skill_listing 与 nested_memory 转 developer message
- thinking blocks 自动剥离
- phase（commentary / final_answer）自动推断
- 3 个 pytest 测试 + live `codex exec resume` 验证通过

❌ 推迟（见 `specs/001-translator-mvp.md` §1.2）：
- Edit / Write / MultiEdit 完整 apply_patch 翻译（目前降级为 echo 占位）
- subagent 翻译（`Agent` 工具）
- compact_boundary 处理（遇到会抛 NotImplementedError）
- Codex → CC 反向翻译
- DAG 多 leaf
- Mode B / C fidelity
- LLM 摘要

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
