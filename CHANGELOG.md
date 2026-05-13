# Changelog

All notable changes to **agent-bridge**. Versions follow [SemVer](https://semver.org/) — pre-1.0 minor bumps may include breaking changes.

Each version maps to a git tag (`v0.1.0` … `v0.6.0`).

---

## Unreleased

### Fixed
- Preserve Claude Code `custom-title` / `agent-name` metadata when rendering into Codex so sessions appear with the user's readable title instead of the first prompt.
- Use session `ended_at` for Codex `state_5.sqlite.updated_at`, so recently synced long-running sessions appear in the recent picker instead of being buried by their start time.
- Refuse to overwrite real Claude Code jsonl sessions during reverse rendering; only agent-bridge-generated `[from ...]` files are eligible for idempotent overwrite.
- Re-render missing targets even when source-side sync state says the source is unchanged.
- Install Claude Code Stop hooks with `--include-active`, so the just-finished session is not skipped while Claude Code still lists it in the session registry.
- Skip sources with no replayable moments and remove stale generated empty mirrors instead of leaving tiny picker entries.
- Preserve Codex `session_index.jsonl` user titles when upserting `state_5.sqlite`, so force re-rendering cannot hide renamed sessions from Codex search/picker.

### Added
- `agent-resume sync --force` to repair old short mirrors by bypassing source-side sync state and re-rendering deterministic targets.

### Verified
- 31 pytest pass.

---

## v0.6.0 — Title sync, no duplicates (2026-05-07)

**Headline**: rename a session in either CC or Codex; the new title propagates to its twin without spawning a duplicate file.

### Added
- `src/agent_bridge/pair_map.py` — bidirectional `cc_id ↔ codex_id` registry. Auto-recorded after every `cc→codex` render.
- `src/agent_bridge/title_sync.py::propagate_titles()` — runs at sync start; walks the pair map; appends `custom-title` row to CC and a `SessionIndexEntry` to `~/.codex/session_index.jsonl` whenever sides drift.
- `agent-resume dedupe` — bootstraps the pair map from disk, then removes legacy duplicate CC files (those whose UUID matches `deterministic_uuid4("codex:<id>")` for an agent-bridge codex file with a known CC twin); propagates the title onto the twin first.
- `agent-resume sync --include-active` — opt-in: also translate the currently-busy CC session.

### Changed
- Default `--days` widened from 7 → 365 (full history by default).
- Default `--max-bytes` raised from 25 MB → 100 MB.
- `_user_touched_codex_translation()` now only triggers full re-translation on actual content growth. Rename-only changes are handled exclusively by `title_sync` so no second CC file is created.

### Fixed
- Round-trip echo: previously every real CC source produced both a Codex copy and a CC copy of the Codex copy. CC `[from codex]` count is now exactly 1:1 with real codex sessions (plus user-touched bridge files).
- Stale `pair-map.json` after `clean` — wiped alongside other state files.

### Migration
- Old duplicate CC files won't be removed until you run `agent-resume dedupe` once. After that the pair map is bootstrapped and future syncs stay clean.

### Verified
- `codex /name` → CC `custom-title` updated; `00369f75` legacy duplicate removed; `claude --resume` still loads the original CC source which now shows the new name.
- CC custom-title append → codex `session_index.jsonl` gains the same name.
- 24 pytest pass.

---

## v0.5.0 — Picker visibility, mtime correctness, content fidelity (2026-05-06)

**Headline**: translated sessions actually show up in `codex resume` and `claude --resume` pickers with correct titles, correct timestamps, and full assistant transcripts.

### Fixed
- **Codex picker invisibility** (multiple root causes, fixed in sequence):
  - Hardcoded `model_provider: "openai"` was filtered by `ProviderMatcher` against the user's configured provider (e.g., `openai`). Now omitted; picker falls back to `matches_default_provider`.
  - `source: {"custom":"agent-bridge"}` not in `INTERACTIVE_SESSION_SOURCES` whitelist (`Cli, VsCode, Custom("atlas"), Custom("chatgpt")`). Even `--include-non-interactive` doesn't open this up. Switched to `source: "cli"`; distinction preserved via `originator: "agent-bridge"`.
  - Codex picker reads `~/.codex/state_5.sqlite` `threads` table, not the filesystem. Render now `INSERT OR REPLACE`s a row so picker actually lists our jsonls.
  - `metadata.title` extractor reads `event_msg.user_message` only — `response_item.message` is a no-op for title extraction. Render now emits both response_item AND paired event_msg for every UserText moment.
- **Half-conversation in Codex resume**: TUI transcript reader uses `event_msg.agent_message`, not `response_item.message(role=assistant)`. Render now emits `event_msg.agent_message` mirrors for every AssistantText.
- **mtime = sync time, not session time**: file mtimes were "just now" so pickers showed everything as recent. Render now `os.utime`s target jsonl to the latest moment timestamp (or session `ended_at`).
- **CC title showed `<environment_context>` envelope**: `_first_real_user_text()` skips Codex/CC injection envelopes (`<environment_context`, `<permissions instructions`, `<command-name`, `<command-message`, `<local-command-caveat`, `<system-reminder`, etc.) when picking a title body.
- **User-set thread_name lost on resync**: `_make_thread_name()` reads existing user-set thread_name from `session_index.jsonl` (filtering out our `[from <harness>]` autogenerated ones); sync no longer overwrites it.

### Added
- `_backdate_mtime()` helper in both renderers.
- `_read_codex_thread_name()` reads latest user-set thread_name from `session_index.jsonl`.
- Session-index append in `codex/render.py` so picker has a thread_name to show in the Conversation column.
- `compact_boundary` graceful handling: emits a `SummaryCompaction` moment instead of `NotImplementedError`.
- `apply_patch.py` real translation for `Edit`/`Write`/`MultiEdit`/`Delete`/`Move` (was previously echo-only).
- subagent inline strategy: scans `<sess-id>/subagents/agent-*.jsonl` siblings, splices into main timeline.

### Changed
- `clean` now also wipes `~/.cache/agent-bridge/sync-state.json` and `pair-map.json`.

### Verified
- `codex resume` (no flags) lists translated sessions with correct `[from claude-code] <prompt>` titles.
- `claude --resume` lists translations with `[from codex] <prompt>` titles.
- Live resume in both directions; model recalls full context.

---

## v0.4.0 — Continuous sync (2026-05-06)

**Headline**: agent-bridge stops being a one-shot CLI; now syncs both sides continuously via hooks, watch daemon, or MCP.

### Added
- `agent-resume sync [--direction both|cc-to-codex|codex-to-cc]` — batch translate recent sessions both ways. Idempotent via deterministic UUIDs (`deterministic_uuid4`/`deterministic_uuid7`). Skips currently-active CC sessions and files exceeding `--max-bytes`.
- `agent-resume watch -i <secs>` — polling daemon that runs `sync_once` on every tick.
- `agent-resume install-hook --target claude-code` — writes a Stop hook into `~/.claude/settings.json` Stop chain. Preserves any existing hook (chains via `;`). Idempotent.
- `agent-resume install-hook --target codex` — writes a `notify` chain into `~/.codex/config.toml`. Fires after every Codex `AfterAgent` event in both TUI and `codex exec`. Preserves the user's existing notify (e.g., afplay sound) by chaining via `sh -c "<old>; <our cmd>"`.
- `agent-resume clean [--dry-run]` — removes every jsonl agent-bridge previously generated.
- `agent-resume mcp serve` — stdio MCP server exposing 6 tools: `list_sessions`, `translate_session`, `sync_now`, `find_session`, `prepare_resume`, `resume_with_prompt`.
- `agent-resume mcp config-snippet` — emits ready-to-paste host config JSON for Claude Desktop / Cursor / Cline / Codex.

### Documented
- `docs/codex-notify-research.md` — sub-agent + source-code investigation of `notify` semantics.
- `specs/004-mcp-and-hooks.md` — MCP server design + hook integration spec.

### Verified
- 4 install-hook unit tests + 2 MCP server stdio integration tests; 24 total pytest.
- Live `codex exec` triggers notify → sync runs → CC mirror appears.

---

## v0.3.0 — Bidirectional translation (2026-05-06)

**Headline**: sessions translate both ways. Stop in CC, resume in Codex. Stop in Codex, resume in CC.

### Added
- `adapters/codex/ingest.py` — Codex rollout jsonl → canonical `Session`. Handles `session_meta`, `turn_context`, all `response_item` subtypes (`message`, `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, `web_search_call`), the `compacted` top-level RolloutItem, and `event_msg` deduplication.
- `adapters/codex/apply_patch_parser.py` — Lark-grammar reverse parser for `apply_patch` input strings. Splits multi-op envelopes into per-file canonical ToolCalls (`write_file` / `edit_file` / `multi_edit_file` / `delete_file` / `move_file`).
- `adapters/codex/ingest._classify_shell_cmd` — recognizes idiomatic shell commands we emit during CC→Codex (`cat -n`, `head`, `tail`, `sed -n`, `rg --files`) so round-trips preserve canonical tool semantics.
- `adapters/claude_code/render.py` — canonical → CC jsonl. `realpath` + `NFC` normalize matches CC's `PY()` encoded-cwd function. Linear `parentUuid` chain. Drops `thinking` blocks. Maps canonical tool names back to CC wire names with `wire_native` fallback.

### Documented
- `specs/003-bidirectional.md` — status, end-to-end verification table, known limitations.

### Verified
- Hand-crafted CC jsonl resumed via `claude -p --resume <UUID>` → "CC-RESUME-OK".
- Real ~563 KB Codex session → CC translation → `claude --resume` → "REVERSE-WORKS".
- 18 pytest (3 new round-trip + ingest + parser-split tests).

### Lossy items (documented, not bugs)
- Codex `reasoning.encrypted_content` cannot be reconstructed → CC `thinking` blocks dropped on `Codex→CC`; only summary text (if present) preserved.
- CC `thinking` signatures cannot be portable → also dropped.
- Multi-op `apply_patch` envelopes split into multiple CC `tool_use` moments with synthesized `call_ids` of the form `<orig>__<idx>`.

---

## v0.2.0 — Spec 002 completion (2026-05-06)

**Headline**: real `apply_patch` translation, `compact_boundary` no longer crashes, `subagent` inline strategy works, plus `list` and `smoke` CLI commands.

### Added
- `agent-resume list` — list recent CC sessions on disk with first prompt + tool summary + line count.
- `agent-resume smoke <path>` — translate then immediately run `codex exec resume` (or `claude -p --resume`) end-to-end. Exits non-zero if the model can't be reached. Cleans up the test session by default.
- `apply_patch.py::FileStateCache` + `build_file_state()` — pre-pass that walks moments to track file content via `Read` results and `Write`/`Edit` cache simulation. Lets render produce real ≤3-line context hunks.
- `patch_for_write/edit/multi_edit/delete/move` — Lark-grammar string generators.
- `to_relative()` — absolute → relative path normalization (`apply_patch` requires relative).
- `strip_cat_n()` — removes CC's `Read` line-number prefixes.
- `SummaryCompaction` canonical moment kind.
- subagent inline rendering: matches main-session `subagent_dispatch` to specific transcript via meta description (exact match, chronological fallback). Splices each sub transcript right after its dispatch's `ToolResult`. Wraps with `--- begin/end subagent transcript (<id>) ---` developer messages.

### Changed
- `Write` on existing file now uses `Delete + Add` envelope (atomic).
- `replace_all=true` produces multi-hunk Update File.

### Documented
- `specs/002-completion.md` — what 002a/b/c/d landed; deferrals; hand-off to 003.

### Verified
- 12 new `apply_patch` unit tests + existing 3 E2E. 15 pytest pass.
- Real 1.8 MB CC session with 4 subagents: all 4 spliced inline, output 1117 lines.

---

## v0.1.0 — MVP (2026-05-06)

**Headline**: end-to-end CC → Codex translator. The `claude code` agent puts the conversation down; `codex exec resume` picks it up and the model continues.

### Added
- Canonical IR (pydantic v2 schema with discriminated union of 11 moment kinds: `UserText`, `AssistantText`, `Thinking`, `ToolCall`, `ToolResult`, `Attachment`, `ModeChange`, `PlanUpdate`, `SummaryCompaction`, `Error`, `Notification`, plus `subagent_call`).
- `claude_code/ingest.py` — reads CC jsonl; drops `permission-mode`, `file-history-snapshot`, `last-prompt`, title rows, queue rows; splits multi-block assistant content into `AssistantText` / `ToolCall` / `Thinking` moments; parses attachments.
- `codex/render.py` — writes `session_meta` + `turn_context` + `response_items` to `~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl`.
- 6 core tools: `shell` / `read_file` / `find_files` / `search_text` → `function_call exec_command`; `attachment / skill_listing / nested_memory` → developer message envelope.
- Phase inference (`commentary | final_answer`) via post-pass over moments.
- typer CLI: `agent-resume translate <CC-jsonl>`.

### Verified
- 3 pytest tests pass (structural match against PoC fixture).
- Live: translator output → `codex exec resume <UUID>` → model replied "MVP-OK".
- PoC fixture (`data/fixture/cc-input.jsonl` + `codex-output.jsonl`) committed for regression.

### Deferred
- Edit/Write/MultiEdit `apply_patch` (echo placeholder for MVP).
- subagent inline/split, `compact_boundary`, mode B/C fidelity.
- Codex → CC reverse, DAG multi-leaf, MCP server.
