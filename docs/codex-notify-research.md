# Codex CLI `notify` hook — research report

Research scope: how Codex CLI's `notify` config hook works, with enough precision to wire `agent-bridge sync` to fire after every Codex turn via `agent-resume install-hook --target codex` writing to `~/.codex/config.toml`.

All upstream source citations refer to `openai/codex` on GitHub, branch `main`, fetched on 2026-05-06 via `raw.githubusercontent.com`.

---

## 1. Config syntax

### Type — always a Vec<String> argv array. Single string is NOT accepted.

The TOML field `notify` is deserialized as `Option<Vec<String>>`. Source:

- `codex-rs/config/src/config_toml.rs:149-151`
  ```rust
  /// Optional external command to spawn for end-user notifications.
  #[serde(default)]
  pub notify: Option<Vec<String>>,
  ```

The same shape is preserved on the runtime `Config` struct (loaded from TOML, then handed to the session):

- `codex-rs/core/src/config/mod.rs:485-505`
  ```rust
  /// Optional external notifier command. When set, Codex will spawn this
  /// program after each completed *turn* (i.e. when the agent finishes
  /// processing a user submission). The value must be the full command
  /// broken into argv tokens **without** the trailing JSON argument - Codex
  /// appends one extra argument containing a JSON payload describing the
  /// event.
  ///
  /// Example `~/.codex/config.toml` snippet:
  ///
  /// ```toml
  /// notify = ["notify-send", "Codex"]
  /// ```
  ///
  /// which will be invoked as:
  ///
  /// ```shell
  /// notify-send Codex '{"type":"agent-turn-complete","turn-id":"12345"}'
  /// ```
  ///
  /// If unset the feature is disabled.
  pub notify: Option<Vec<String>>,
  ```

### TOML format

```toml
notify = ["program", "arg1", "arg2"]
```

- Top-level key (NOT under any `[section]`).
- Always a TOML array of strings. A bare string `notify = "foo"` will fail TOML deserialization — `serde` will reject it because the field is typed `Vec<String>`.
- The first element is the program; subsequent elements are passed as argv.

### Default value

`None`. The `#[serde(default)]` annotation on the TOML struct means an absent key deserializes to `None`. There is no fallback program.

### How "disabled" is determined

Source: `codex-rs/hooks/src/registry.rs:55-62`:

```rust
impl Hooks {
    pub fn new(config: HooksConfig) -> Self {
        let after_agent = config
            .legacy_notify_argv
            .filter(|argv| !argv.is_empty() && !argv[0].is_empty())
            .map(crate::notify_hook)
            .into_iter()
            .collect();
```

So the hook is silently skipped if:
- `notify` is absent (`None`),
- the array is empty (`[]`),
- or the first element is the empty string (`["", ...]`).

Otherwise it is registered as the sole `after_agent` hook.

### How config.notify reaches the runtime

`codex-rs/core/src/session/mod.rs:3322-3330` — when the session is built, `config.notify` is forwarded into the hooks runtime as `legacy_notify_argv`:

```rust
Hooks::new(HooksConfig {
    legacy_notify_argv: config.notify.clone(),
    feature_enabled: config.features.enabled(Feature::CodexHooks),
    ...
})
```

Naming note: upstream calls this the "legacy" notifier because the broader `[hooks]`/Claude-style hook system has superseded it for new use cases. The legacy notifier is still fully supported and is the only documented top-level `notify` config knob.

---

## 2. When does it fire?

### Event: `AfterAgent` (a.k.a. "agent turn complete")

Dispatch site: `codex-rs/core/src/session/turn.rs:565-581`:

```rust
let hook_outcomes = sess
    .hooks()
    .dispatch(HookPayload {
        session_id: sess.conversation_id,
        cwd: turn_context.cwd.clone(),
        client: turn_context.app_server_client_name.clone(),
        triggered_at: chrono::Utc::now(),
        hook_event: HookEvent::AfterAgent {
            event: HookEventAfterAgent {
                thread_id: sess.conversation_id,
                turn_id: turn_context.sub_id.clone(),
                input_messages: sampling_request_input_messages,
                last_assistant_message: last_agent_message.clone(),
            },
        },
    })
    .await;
```

The `Hooks::dispatch` routing (`codex-rs/hooks/src/registry.rs:84-104`) matches `HookEvent::AfterAgent` → `self.after_agent` hooks, which is exactly where `notify_hook(argv)` is registered. So `notify` fires **only on `AfterAgent`**. It does NOT fire on:
- task started / user prompt submit
- approval requests
- per-tool-call completion (`AfterToolUse` exists separately and would only run plugin hooks; legacy notify rejects it — see `codex-rs/hooks/src/legacy_notify.rs:40-43`)
- errors before turn completion (the `AfterAgent` dispatch is gated on `if !needs_follow_up { ... }` at `session/turn.rs:507`)

### Once per completed turn

The dispatch is inside `run_turn()` at `codex-rs/core/src/session/turn.rs:137`, in the branch `if !needs_follow_up { ... }` (line 507). That branch fires exactly once per turn — after the agent has decided not to call any more tools and produced its final assistant message for the turn. It does not fire after each tool call within a turn. Auto-compaction loops (`continue` at line 504) and stop-hook-driven continuations (`continue` at line 551) do NOT trigger an extra notify because they bypass the terminal block.

### Both `codex exec` and TUI

`run_turn()` in `session/turn.rs` is the single shared turn driver. Both the interactive TUI and `codex exec` (non-interactive) submit user inputs through the same `Session` plumbing, so the notify fires in both modes. There is no TUI-only gate around the dispatch.

### Exit codes & return values

Codex inspects nothing about the spawned process other than whether spawn itself succeeded. See `codex-rs/hooks/src/legacy_notify.rs:60-69`:

```rust
command
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null());

match command.spawn() {
    Ok(_) => HookResult::Success,
    Err(err) => HookResult::FailedContinue(err.into()),
}
```

The returned `Result` of the child is never awaited. Exit code is irrelevant to Codex. A non-zero exit is invisible.

---

## 3. What arguments does the command receive?

### A single extra argv element containing the JSON payload, appended to the configured argv. Nothing on stdin.

Source: `codex-rs/hooks/src/legacy_notify.rs:46-72`:

```rust
pub fn notify_hook(argv: Vec<String>) -> Hook {
    let argv = Arc::new(argv);
    Hook {
        name: "legacy_notify".to_string(),
        func: Arc::new(move |payload: &HookPayload| {
            let argv = Arc::clone(&argv);
            Box::pin(async move {
                let mut command = match command_from_argv(&argv) {
                    Some(command) => command,
                    None => return HookResult::Success,
                };
                if let Ok(notify_payload) = legacy_notify_json(payload) {
                    command.arg(notify_payload);
                }

                command
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());

                match command.spawn() {
                    Ok(_) => HookResult::Success,
                    ...
```

So given `notify = ["sh", "-c", "my-script"]`, the spawned process is:

```
sh -c my-script '<JSON-payload>'
```

i.e. the JSON is the **fourth argv element** (`$0` inside the script), which is a footgun for the `sh -c` pattern (more on this in §5).

For the simpler form `notify = ["my-bin", "--flag"]`, the spawned process is:

```
my-bin --flag '<JSON-payload>'
```

`stdin` is `/dev/null`. `stdout` and `stderr` are both redirected to `/dev/null` by the spawner — the command's output is **discarded**, never logged anywhere.

### JSON payload shape

Source: `codex-rs/hooks/src/legacy_notify.rs:13-44` (struct + serializer) and the upstream test `legacy_notify_json_matches_historical_wire_shape` (lines 121-148) which canonicalizes the wire format.

```rust
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
enum UserNotification {
    #[serde(rename_all = "kebab-case")]
    AgentTurnComplete {
        thread_id: String,
        turn_id: String,
        cwd: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        client: Option<String>,
        input_messages: Vec<String>,
        last_assistant_message: Option<String>,
    },
}
```

Field-name renaming: `serde(rename_all = "kebab-case")` is applied at both the enum level and the variant level, so all keys use kebab-case. Real example payload from the upstream test (`expected_notification_json`, lines 88-99):

```json
{
  "type": "agent-turn-complete",
  "thread-id": "b5f6c1c2-1111-2222-3333-444455556666",
  "turn-id": "12345",
  "cwd": "/Users/example/project",
  "client": "codex-tui",
  "input-messages": ["Rename `foo` to `bar` and update the callsites."],
  "last-assistant-message": "Rename complete and verified `cargo build` succeeds."
}
```

Notes:
- `type` is always `"agent-turn-complete"` for legacy notify (no other variants exist; `AfterToolUse` returns an error and is never serialized to legacy-notify JSON).
- `thread-id` is the conversation/session id — same UUID as the rollout filename `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl`. **This is the key field for our integration.**
- `turn-id` is `turn_context.sub_id` — the per-turn submission id, not stable across turns.
- `client` is omitted when `None`. The TUI sets it to `"codex-tui"` (see test). For `codex exec`, the value depends on `app_server_client_name` (likely absent or `"codex-exec"`).
- `cwd` is the absolute working directory of the session.
- `input-messages` is the array of user-submitted message strings for that turn.
- `last-assistant-message` is the agent's final assistant text (may be `null`).

The payload contains **no rollout filename**, **no rollout path**, and no token counts.

---

## 4. Behavior caveats

### Asynchronous fire-and-forget — does NOT block the turn

Source: `legacy_notify.rs:66` calls `command.spawn()` and immediately returns `HookResult::Success`. There is no `.wait()`, `.status()`, `.output()`, no `await` on the child. The child is detached.

Tokio's `Command` (`use tokio::process::Command;` at `codex-rs/hooks/src/registry.rs:3`) defaults to `kill_on_drop = false`, so the spawned `Child` is dropped immediately and continues running independently. In practice this means:

- The child becomes a process tree owned by the Codex process; it's reaped via the default tokio reaper but Codex never observes its exit.
- Codex returns control to the user immediately after spawn, regardless of how long the script runs.
- Codex's "turn complete" UI/event flow is NOT delayed by the notify command.

### Failure handling

- If `spawn` itself fails (e.g., binary not found, permission denied on the binary): `HookResult::FailedContinue(err)` is returned. The dispatch loop in `session/turn.rs:584-611` logs a `warn!` line ("after_agent hook failed; continuing") and continues. The user does not see an error.
- If `spawn` succeeds but the child later exits non-zero or hangs: Codex sees nothing. No log, no user warning.
- The legacy notify path never returns `HookResult::FailedAbort`, so it cannot abort turn completion.

### Timeout

There is **no timeout**. The legacy notify path has no timeout wrapper around `spawn`. (The newer `[hooks]` system has its own per-hook timeouts, but legacy `notify` does not use them.) Since Codex doesn't wait on the child anyway, a slow or hung script doesn't block Codex — but it will accumulate orphaned processes.

### Output handling

`stdin`, `stdout`, `stderr` are all set to `Stdio::null()`. Anything the script writes is discarded. There is no upstream log file capturing notify command output.

### Environment variables

The `tokio::process::Command::new(program)` constructor inherits the parent process environment by default, and `command_from_argv` (`registry.rs:199-207`) does not call `.env_clear()` or `.env_remove()`, nor does it set any extra vars. So:

- All env vars present in the Codex parent process are passed through (`PATH`, `HOME`, `USER`, etc.).
- No special env vars like `CODEX_NOTIFY_*` or `CODEX_SESSION_ID` are injected. **The JSON argv is the only contextual data.**

### Working directory

`Command::new` uses the inherited `cwd` of the Codex process — which is whatever `cwd` Codex itself was launched with, NOT the `cwd` reported in the JSON payload (those can differ if `--cd` was used or if the session runs in a different sandbox). The script should not rely on its own `cwd`; use the JSON `cwd` field if needed.

### Concurrency

Each `AfterAgent` event spawns its own child. There is no debouncing. If a session somehow double-fires (it doesn't, in practice — single dispatch per turn), each fire would spawn an independent child.

---

## 5. Recommended invocation for our use

### Goals recap

1. Run `agent-bridge sync` after every Codex turn so the just-extended rollout jsonl is mirrored to the CC-side store.
2. Don't block Codex (already guaranteed by Codex — fire-and-forget).
3. Don't error-spam any UI (already guaranteed — stdout/stderr are `/dev/null`).
4. Return fast even though Codex won't wait — keeps the spawned-process tree lean.

### Recommended `notify` value

The simplest, most robust form is a direct invocation of the agent-bridge binary with a single trailing `sync` subcommand. The JSON payload becomes the next argv element (`$1` to the binary), which agent-bridge can either ignore or parse for the `thread-id` to scope the sync.

```toml
notify = ["agent-bridge", "sync", "--from-codex-notify"]
```

Resulting spawn:
```
agent-bridge sync --from-codex-notify '<JSON-payload>'
```

The agent-bridge `sync` command should accept and ignore the trailing JSON argv (or parse it for the thread-id to scope the sync to the active session). The flag `--from-codex-notify` lets agent-bridge silence its own logs / behave appropriately when invoked via this path.

### Why NOT use `sh -c`

The shell `sh -c` form has a footgun: with `notify = ["sh", "-c", "agent-bridge sync"]`, Codex appends the JSON payload as the next argv, which `sh -c` interprets as `$0` for the script. The JSON does not get passed to `agent-bridge` — it gets eaten by `sh`. The script would have to use `"$0"` to access it, which is unintuitive and easy to get wrong. Direct binary invocation avoids this entirely.

### If agent-bridge is not on $PATH

Use an absolute path:

```toml
notify = ["/Users/alice/.cargo/bin/agent-bridge", "sync", "--from-codex-notify"]
```

(or wherever the install prefix is — `agent-resume install-hook --target codex` should detect or accept the binary path). Codex inherits its own `PATH`, which on macOS launched from Terminal/iTerm/etc. will include `~/.cargo/bin` etc., but launched from Spotlight or LaunchAgents may not.

### Silence guarantees

- No need to redirect stdout/stderr; Codex already discards both via `Stdio::null()`.
- No need for `&` or `nohup`; Codex already detaches via fire-and-forget spawn.
- No need to add `>/dev/null 2>&1`; would be a no-op.

### What if the user already has a `notify` set?

The user's existing `~/.codex/config.toml` (read 2026-05-06) currently contains:
```toml
notify = ["sh", "-c", "afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1"]
```

This is incompatible with our hook because `notify` is a single-valued field, not additive. The install-hook implementation should:

1. Detect existing `notify` and prompt / warn the user (don't silently overwrite — they may rely on the sound).
2. If user accepts, replace with a wrapper that does both: e.g. `["sh", "-c", "afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1; agent-bridge sync --from-codex-notify \"$1\""]` — but note the `$0` vs `$1` issue: `sh -c '<script>'` puts the next arg into `$0`, so the JSON payload would be `$0` here, and the script needs to be careful. A safer wrapper:
   ```toml
   notify = ["sh", "-c", "afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1 & exec agent-bridge sync --from-codex-notify \"$0\"", "_"]
   ```
   Add a sentinel arg `"_"` to consume `$0`, then `$0` = `"_"` and the JSON is `$1` if needed — actually upstream only appends ONE arg, so the JSON ends up at `$1` when the sentinel is at `$0`. Wait — re-checking: argv after `sh -c <script>` are `[$0, $1, $2, ...]`. With `notify = ["sh", "-c", "<script>", "_"]`, Codex appends the JSON as the 5th element, so inside the script `$0 = "_"`, `$1 = "<JSON>"`. That's the conventional pattern.
3. Realistically, the cleanest path is to **replace** the existing `notify` and document the tradeoff. The hook installer should preserve the original value as a comment so the user can restore it.

---

## 6. Test plan

A 3-step verification an implementation can run:

### Step 1 — write a test config touching a sentinel file

Create or back up `~/.codex/config.toml`, then set:

```toml
notify = ["sh", "-c", "echo notify-fired-$(date +%s) >> /tmp/codex-notify-test.log; echo \"$1\" >> /tmp/codex-notify-test.log", "_"]
```

(The sentinel `"_"` consumes `$0` so the JSON payload lands in `$1`.)

Pre-step: `rm -f /tmp/codex-notify-test.log`

### Step 2 — run `codex exec` with a trivial prompt

```bash
codex exec --skip-git-repo-check 'say hi and stop'
```

Wait for it to exit (should be one turn).

### Step 3 — verify the sentinel was touched and the JSON is well-formed

```bash
cat /tmp/codex-notify-test.log
```

Expected:
- One `notify-fired-<unix-ts>` line.
- One JSON line containing `"type":"agent-turn-complete"`, `"thread-id":"<UUID>"`, `"turn-id":"<sub_id>"`, `"cwd":"<absolute-path>"`, `"input-messages":["say hi and stop"]`, and a `"last-assistant-message"`.

Validate JSON:

```bash
tail -1 /tmp/codex-notify-test.log | jq '.["type"], .["thread-id"], .["turn-id"], .["last-assistant-message"]'
```

If both lines appear and the JSON parses with the expected `type`, the hook works. The implementation can then swap the sentinel script for the real `agent-bridge sync` invocation.

### Optional step 4 — test multi-turn

For a TUI session, run `codex` interactively, send 2 prompts, and verify `/tmp/codex-notify-test.log` has exactly 2 `notify-fired-*` lines. (Confirms once-per-turn semantics.)

---

## 7. Open questions

These were not fully resolved from source/docs — flag for follow-up if they matter:

1. **`client` field value for `codex exec`.** In the TUI test fixture it's `"codex-tui"`. For `codex exec` it's set from `turn_context.app_server_client_name`, which I did not trace to its exec-mode source. Empirically it is likely `None` (omitted) or `"codex-exec"`. Verify via the test plan output. Not blocking — agent-bridge doesn't need this field.

2. **Behavior on `CodexHooks` feature gate.** `Hooks::new` accepts `feature_enabled` but the legacy notify hook is registered unconditionally — only the new Claude-style `[hooks]` system is gated. Confirmed legacy notify works regardless of `experimental_use_*` flags as far as the source shows. If a future Codex version retires `legacy_notify`, this assumption breaks. Consider pinning a Codex version compatibility note in the install-hook command.

3. **Whether `codex resume` / replays fire `notify`.** Resuming a session replays past turns from rollout. `run_turn` is invoked for each new live turn, so resumed-then-continued sessions should fire normally. Pure replay (no new turn) should NOT fire. Not verified empirically — could test by `codex resume <id>` with no new prompt.

4. **Session prefix / session-startup-prewarm interaction.** Codex has a `session_startup_prewarm.rs` and pre-warm threads. These execute before the first user turn; I did not verify they don't fire spurious `AfterAgent` events. Likely fine since the dispatch is gated on `!needs_follow_up` after a real model completion.

5. **TOML serializer round-trip when modifying via toml-edit.** Our install-hook will need to read+modify+write `~/.codex/config.toml` while preserving comments and other sections. The user's existing config has `[model_providers.openai]`, `[notice.model_migrations]`, `[tui.model_availability_nux]`, `[projects."/Users/alice"]` sections — toml-edit should handle all of these but verify before shipping.

6. **Behavior when `notify` array contains non-string TOML values.** Serde will reject (`Vec<String>` is strict). Any malformed array silently disables the feature with no startup warning surfaced to the user — `Hooks::new` just produces an empty `after_agent` vector. Our installer should validate before writing.

---

## Source citations summary

| Concern | File:line |
|---|---|
| TOML deserialize struct field | `codex-rs/config/src/config_toml.rs:149-151` |
| Runtime Config struct field + doc | `codex-rs/core/src/config/mod.rs:485-505` |
| Config -> Hooks wiring | `codex-rs/core/src/session/mod.rs:3322-3330` |
| Hook registration (skip-empty rules) | `codex-rs/hooks/src/registry.rs:55-78` |
| `command_from_argv` builder | `codex-rs/hooks/src/registry.rs:199-207` (uses `tokio::process::Command`) |
| Legacy notify spawn (fire-and-forget, all stdio null) | `codex-rs/hooks/src/legacy_notify.rs:46-73` |
| JSON payload struct + kebab-case | `codex-rs/hooks/src/legacy_notify.rs:13-44` |
| Wire-format canonical test | `codex-rs/hooks/src/legacy_notify.rs:88-148` |
| AfterAgent dispatch site (turn-complete branch) | `codex-rs/core/src/session/turn.rs:507-611` |
| `dispatch` routing by event type | `codex-rs/hooks/src/registry.rs:84-104` |
