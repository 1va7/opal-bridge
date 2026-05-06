# Agent Instructions

## Project purpose

Build a translator/bridge that lets a developer move a coding session between
different agent harnesses (Claude Code, Codex, Hermes, ...) by converting their
respective session jsonl formats through a canonical intermediate representation.

## Working principles

- **Empirical over speculative.** When documenting harness behavior, prefer
  evidence from real session files on disk and from upstream source code. Mark
  any unverified claim explicitly.
- **Both directions, always.** Every mapping decision must be reversible.
  Document the lossy cases instead of silently dropping data.
- **Canonical first.** Do not wire CC ↔ Codex as a direct converter. All
  translation goes through the canonical format defined in
  `docs/ARCHITECTURE.md`.

## Layout

```
agent-bridge/
├── README.md
├── AGENTS.md                # this file
├── docs/
│   ├── ARCHITECTURE.md      # canonical format + adapter contract
│   ├── claude-code-harness.md
│   ├── codex-harness.md
│   └── tool-mapping.md      # the master mapping table
├── specs/                   # per-iteration implementation specs
└── src/                     # adapters and translator (later)
```

## Don't

- Don't run `codex` or `claude` against fixture sessions for testing without
  isolating cwd — they will create new entries in the user's real session
  history.
- Don't commit any real session files to the repo. `data/` is gitignored;
  use it for fixtures, redact PII before committing fixtures elsewhere.
- Don't depend on `sessions-index.json` in `~/.claude/projects/` — it is
  known to be incomplete. Scan jsonl files directly.

## Tests

To be defined once first adapter lands. At minimum: round-trip a session
through canonical and back, assert message-count and tool-call-count
preserved.
