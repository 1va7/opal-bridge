# PoC Report — agent-bridge translator (CC → Codex)

Date: 2026-05-06
Codex: 0.128.0
CC fixture origin: `~/.claude/projects/-Users-va7/0fdd7092-c42c-4263-a7be-0043fee5d776.jsonl` (CC v2.1.107)

## 1. Fixture chosen

- **Path**: `data/fixture/cc-input.jsonl` (copied from real CC session, 10 lines)
- **Tools**: 1× `Bash` tool call (`which claude`) — no Edit, no Skill invocation, no subagent
- **Turn shape**: 1 user prompt → assistant text + Bash tool_use → tool_result → assistant final text
- **Non-dialog noise**: `permission-mode` (×2), `file-history-snapshot`, `attachment / skill_listing`, `last-prompt`. All but `attachment` were dropped per `tool-mapping.md` §1.2.

Translated to `data/fixture/codex-output.jsonl` — 8 lines:
1. `session_meta` (id `019f0000-1234-7000-9000-000000000001`, source `{"custom":"agent-bridge"}`, originator `agent-bridge`, cli_version `0.1.0`, cwd `/Users/va7`)
2. `turn_context` (`approval_policy: "never"`, `sandbox_policy: {"type":"danger-full-access"}`, `model: "gpt-5.5"`, `summary: "none"`)
3. user `message` (text "claude instal")
4. developer `message` (skill_listing summary)
5. assistant `message` phase=`commentary` (pre-tool narration)
6. `function_call` name=`exec_command`, call_id reused = `tooluse_0aaosP0NyuGRaApO6vaRqW`
7. `function_call_output` same call_id, output `/opt/homebrew/bin/claude`
8. assistant `message` phase=`final_answer`

## 2. Translation challenges

- **CC tool ID prefix**: real fixture used `tooluse_<22>`, not `toolu_<22>` documented in `tool-mapping.md`. Both are valid; mapping doc only mentions `toolu_<22>` (line 133 / 1659). Reusing as-is worked fine — Codex doesn't validate the prefix.
- **`attachment / skill_listing`** content was huge (>2KB of skill descriptions). Truncated to a flat name list because (a) the actual SKILL.md isn't in this attachment, only listing, and (b) Codex has its own `~/.codex/skills/` so injecting full content would conflict. Mapping doc §4.2 / §4.6 supports this approach.
- **Phase inference**: easy here — assistant text in L5 had a tool_use following → `commentary`; L8 was last → `final_answer`. Matches §2.5 exactly.
- **Non-dialog metadata stripped**: `permission-mode`, `file-history-snapshot`, `last-prompt`, raw `attachment` envelope. None of these affect resume.
- **No `base_instructions`**: the §5.1.3 minimal-set example doesn't require it; left it off and Codex was happy. (Appendix B sample includes a stub but it's clearly optional.)
- **Source choice**: used `{"custom":"agent-bridge"}` as recommended; works with `codex exec resume <UUID>` directly without `--include-non-interactive`.

## 3. Verification result

Command (run from `/Users/va7`):

```
codex exec resume 019f0000-1234-7000-9000-000000000001 \
   "Just say 'PoC OK' and nothing else." -o /tmp/poc-output.md --skip-git-repo-check
```

Outcome: **success**. Codex loaded the hand-crafted session_meta, used our session_id, replayed history (treating the prior `function_call` + `function_call_output` as completed), and produced a sensible reply.

Tail of `codex exec resume` stdout:

```
session id: 019f0000-1234-7000-9000-000000000001
--------
user
Just say 'PoC OK' and nothing else.
codex
PoC OK
tokens used
767
```

`/tmp/poc-output.md`:
```
PoC OK
```

After resume, Codex appended 11 events to the same jsonl (turn_started, model output, token_count, task_complete, etc.) — file went from 8 lines to 19 lines. **The append-in-place behavior matches `codex-harness.md` §2.6 / §8.3.**

One unrelated stderr line: `failed to load skill /Users/va7/.codex/skills/amazon-rufus/SKILL.md: invalid YAML at line 2 column 224` — a pre-existing user-side skill issue, not caused by our fixture.

## 4. Mapping doc errors / gaps discovered

1. **Tool call ID prefix**: doc claims CC uses `toolu_<22>` (lines 133, 1659). Real CC v2.1.107 emitted `tooluse_<22>` (length 22). Same length, different prefix. Recommend updating §1.3 / §6.1 to note both forms exist (probably version-dependent).

2. **No mention of `permission-mode` / `file-history-snapshot` / `last-prompt` lines being safe to drop wholesale**. They appear at the head/tail of every CC session (lines 1, 2, 9, 10 in our fixture), but §1.2 lists them as their own type rows. Worth adding a one-liner: "These rarely affect resume context and can be skipped when the goal is dialog continuity."

3. **`base_instructions` requirement ambiguity**: Appendix B template (line 1472) includes `"base_instructions":{"text":"You are Codex (translated from Claude Code)."}`. §5.1.3 minimum-set example does not. **Confirmed: omitting `base_instructions` works**. Recommend explicitly stating it's optional for resume.

4. **`source: {"custom":"agent-bridge"}` does NOT actually require `--include-non-interactive`** for `codex exec resume <UUID>` direct-by-id resume. The flag is only needed for picker-based listing. Doc §5.1.6 implies you must use the flag. Update to clarify: by-UUID resume bypasses picker filtering.

5. **`current_date` / `timezone` in turn_context**: appear required but doc §5.1.4 doesn't flag them as such. Worked when present; haven't tested without.

6. **MUST-HAVE field `phase` for assistant messages**: works as expected, but the rule "tool_use after this text → commentary, else final_answer" (§2.5) was unambiguous and easy to apply.

## 5. P0 verdict (mapping doc §9)

| P0 | Status | Evidence |
|---|---|---|
| Codex `EventPersistenceMode::Extended` default — write enough events for resume? | **ANSWERED-YES** (for the no-events case). We wrote zero `event_msg` lines and resume still hydrated correctly. Codex generated its own `task_started`/`token_count`/`task_complete` post-resume. So translation **does not need to fabricate event_msg lines** for basic resume. |
| Hand-crafted CC jsonl resumable? | **SKIPPED** (PoC was reverse direction). |
| Codex resume tolerates historical-only tools? | **ANSWERED-YES**. Our jsonl referenced `exec_command` from translated history; Codex 0.128 didn't validate the call against currently-registered tools and resumed cleanly. Confirms `codex-harness.md` §8.5. |

## 6. Recommendation

**Proceed to MVP coding.** The core translator path (CC dialog blocks → Codex `RolloutItem` sequence → `~/.codex/sessions/YYYY/MM/DD/...jsonl` → `codex exec resume <UUID>`) works end-to-end on the first manually-crafted attempt. None of the documented mapping rules turned out to be wrong — only minor doc tightening needed (items 1, 2, 3, 4 in §4 above).

Suggested doc edits before MVP coding:
- Add `tooluse_*` as a valid CC tool-id prefix in §1.3.
- Annotate the head/tail "metadata-only" CC line types (`permission-mode`, `file-history-snapshot`, `last-prompt`) as drop-on-translate.
- Mark `base_instructions` as optional in the §5.1.3 minimum set; remove it from Appendix B template or note it's illustrative only.
- Clarify `--include-non-interactive` is only needed for the picker, not for by-UUID resume.

Risks not yet validated (not blockers for MVP, but should be tested early):
- Sessions containing `Edit`/`Write` (apply_patch round-trip)
- Sessions with `thinking` blocks (signature stripping)
- Sessions with subagent (`Agent` tool) — strategy A/B/C choice
- Sessions with `compact_boundary`
