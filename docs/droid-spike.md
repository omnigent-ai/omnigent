# Droid harness spike (Factory AI `droid` CLI)

Captured evidence for the in-tree `droid` ACP harness. Everything below was run
live against **Factory CLI v0.164.0** on linux-x64 in this environment.

## Install

```
$ curl -fsSL https://app.factory.ai/cli | sh
Downloading Factory CLI v0.164.0 for linux-x64
Checksum verification passed
Factory CLI v0.164.0 installed successfully to /root/.local/bin/droid
```

`npm i -g droid` was not needed — the official installer works and drops a
self-contained binary at `~/.local/bin/droid`.

## `droid exec` interface (verified)

`droid exec --help` (v0.164.0), relevant flags:

- `-o, --output-format <format>` — **`text` | `json` | `stream-json` | `acp`**
  (`stream-jsonrpc` also exists as a paired in/out format). `acp` is accepted
  silently (a bogus value prints `Invalid --output-format value`).
- `-m, --model <id>` — default `claude-opus-4-8`. Full id list captured below.
- `-r, --reasoning-effort <level>` — per-model levels (see below).
- `--auto <low|medium|high>` — autonomy tier. Default (no flag) is **read-only**
  (Edit/Create/Execute tools are *blocked* — confirmed via `--list-tools`).
- `--skip-permissions-unsafe` — allow everything; **mutually exclusive** with
  `--auto` (`Invalid flags: --auto and --skip-permissions-unsafe cannot be used
  together`).
- `--cwd <path>` — working directory.
- `-s, --session-id <id>` / `--fork <id>` — resume/fork (we don't use these;
  cold-only).

Auth: `FACTORY_API_KEY=fk-...` env var, or interactive `/login` device pairing.
No Omnigent-managed credential — this is an **own-auth** harness.

### Models (from `--list-tools` / `--help`)

`claude-opus-4-8` (default), `claude-opus-4-8-fast`, `claude-opus-4-7`,
`claude-sonnet-5`, `claude-fable-5`, `claude-haiku-4-5-20251001`, `gpt-5.5`,
`gpt-5.5-pro`, `gemini-3.1-pro-preview`, `glm-5.2`, `kimi-k2.7-code`, … (multi-
vendor — hence `ModelFamily.MULTI`). Reasoning levels are per-model, e.g. Opus
4.8 supports `off, low, medium, high, xhigh, max` (default `high`).

## ACP handshake (VERIFIED live, no auth needed)

`droid exec --output-format acp` speaks **ACP (Agent Client Protocol), JSON-RPC
2.0 over newline-delimited stdin/stdout** — the same protocol goose/qwen use.

Client → `initialize`:

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false}}}}
```

Agent → response (captured verbatim):

```json
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"sessionCapabilities":{"list":{},"resume":{}},"promptCapabilities":{"image":true,"embeddedContext":true},"_meta":{"terminal_output":true,"terminal-auth":true}},"agentInfo":{"name":"@factory/cli","title":"Factory Droid","version":"0.164.0"},"authMethods":[{"id":"device-pairing","name":"Login","description":"..."},{"id":"factory-api-key","name":"Factory API Key","description":"...FACTORY_API_KEY..."}]}}
```

So: `protocolVersion: 1`, image prompts supported, `factory-api-key` auth via
`FACTORY_API_KEY`. All CLI flags (`--auto high`, `-m`, `-r`, `--cwd`) are
accepted alongside `--output-format acp` — the `initialize` handshake succeeds
with them present.

## Per-turn stream (VERIFIED live with a working `FACTORY_API_KEY`)

With a valid `FACTORY_API_KEY`, real `session/prompt` turns were driven headlessly
in a throwaway temp dir (`droid exec --output-format acp`) and every per-turn
shape below was captured off the wire (secrets never appear in these payloads;
any `fk-…` token would be redacted). Transport note: ACP is NDJSON over
stdin/stdout — the client must **keep stdin open** for the session's lifetime and
run a continuous stdout reader; `initialize` only responds once a reader is
draining.

### `session/new` result

Returns `sessionId` (as assumed) **plus** large `models` / `modes` /
`configOptions` blocks Omnigent doesn't consume. Trimmed:

```json
{"jsonrpc":"2.0","id":2,"result":{"sessionId":"e4f4d9c4-…","models":{"currentModelId":"claude-sonnet-5","availableModels":[…]},"modes":{"currentModeId":"normal","availableModes":[{"id":"normal","name":"Auto (Off)","description":"Auto-approves only read operations"},{"id":"auto-low",…},{"id":"auto-medium",…},{"id":"auto-high","name":"Auto (High)","description":"Auto-approves all actions"}]},"configOptions":[{"id":"autonomy_level","currentValue":"normal",…},{"id":"model","currentValue":"claude-sonnet-5",…},{"id":"reasoning_effort","currentValue":"max",…}]}}
```

**Key finding — CLI flags are IGNORED in acp mode.** Launched with
`--auto high -m claude-haiku-4-5-20251001 -r low`, `session/new` still reported
`autonomy_level=normal`, `model=claude-sonnet-5`, `reasoning_effort=max`. The
`--auto` / `-m` / `-r` flags do nothing in acp mode. The session default is
`claude-sonnet-5` / `normal` / reasoning `max`, **not** the `claude-opus-4-8`
the non-acp help advertises.

- **Model** *can* be set over the wire: `session/set_model {sessionId, modelId}`
  → `result:{}`, and a following `config_option_update` shows the new
  `model.currentValue`. The executor now sends this after `session/new` to honor
  the configured model.
- **Reasoning effort** has no working acp setter (`session/set_reasoning_effort`
  → method-not-found; a `reasoningEffort` field on `session/set_model` is
  accepted but does not change `reasoning_effort`). Left at the model default.
- `session/set_mode {sessionId, modeId}` is accepted too, but we deliberately do
  **not** raise autonomy: `normal` mode is what makes writes/executes emit
  `session/request_permission` (see below) — auto-high would auto-approve and
  bypass Omnigent's elicitation gate.

### `session/update` variants (VERIFIED)

At turn start droid emits three **metadata** updates we ignore:
`current_mode_update`, `config_option_update`, `available_commands_update`.
Content variants:

```json
// agent_message_chunk — content is a {type,text} dict
{"method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":".\n\nPONG"}}}}

// agent_thought_chunk — same content shape (reasoning)
{"method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"I'll just create the file directly"}}}}

// tool_call (edit) — NO _meta.toolName; name falls back to `title`; args in rawInput
{"method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call","toolCallId":"toolu_bdrk_01QAw…","title":"Create /work/hello.txt","kind":"edit","status":"pending","rawInput":{"file_path":"/work/hello.txt","content":"hi from droid"},"content":[{"type":"diff","path":"/work/hello.txt","oldText":null,"newText":"hi from droid"}],"locations":[{"path":"/work/hello.txt"}]}}}

// tool_call (execute) — kind:"execute"; rawInput carries command + risk fields
{"…":{"sessionUpdate":"tool_call","toolCallId":"toolu_…","title":"`echo hi` (low)","kind":"execute","status":"pending","rawInput":{"command":"echo hi","summary":"…","riskLevel":"low","riskLevelReason":"…"}}}

// tool_call_update — edit: status only (no rawOutput)
{"…":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","status":"completed"}}

// tool_call_update — read: rawOutput present as a {"text":…} dict (+ content)
{"…":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","status":"completed","rawOutput":{"text":"line one\nline two\nline three"},"content":[{"type":"content","content":{"type":"text","text":"line one\n…"}}]}}

// tool_call_update — execute: output goes to a terminal, not inline
{"…":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","status":"in_progress","content":[{"type":"terminal","terminalId":"181ac1fe-…"}]}}
```

No `plan` update was emitted even when prompting for a plan (the model answered
inline). No `usage` / `usage_update` / token / `size` field appeared in ANY
turn.

### `session/request_permission` (VERIFIED) + reply

Fires for write/execute tools because the session runs in `normal` mode (reads
are auto-approved and do **not** prompt). This is exactly the hook Omnigent's
policy/elicitation gate needs.

```json
// request (agent → client)
{"jsonrpc":"2.0","id":0,"method":"session/request_permission","params":{"sessionId":"…","options":[{"optionId":"proceed_once","name":"Allow","kind":"allow_once"},{"optionId":"proceed_always","name":"Allow & auto-run low risk commands","kind":"allow_always"},{"optionId":"cancel","name":"No, cancel","kind":"reject_once"}],"toolCall":{"toolCallId":"toolu_…","title":"Create /work/hello.txt","rawInput":{"file_path":"…","content":"…"},"kind":"edit"}}}

// reply (client → agent) — the DOUBLE-nested outcome is required and works
{"jsonrpc":"2.0","id":0,"result":{"outcome":{"outcome":"selected","optionId":"proceed_once"}}}
```

Note `optionId` (`proceed_once` / `proceed_always` / `cancel`) differs from
`kind` (`allow_once` / `allow_always` / `reject_once`); the executor selects by
`kind`, which is correct. After `proceed_once` the file was written to disk.

### Final result + interrupt (VERIFIED)

Every `session/prompt` result is **stopReason-only** — no usage:

```json
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

`session/cancel` (a no-id notification with `{sessionId}`) is honored: the
in-flight `session/prompt` resolves mid-turn with:

```json
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"cancelled"}}
```

### fs delegation — droid does NOT use it

Even with `clientCapabilities.fs.readTextFile/writeTextFile: true` advertised,
droid 0.164.0 **never** sent `fs/read_text_file` / `fs/write_text_file`; it read
the file with its own internal `read` tool (a normal `tool_call kind:"read"`).
So the `fs/*` delegation handlers stay unexercised with droid, and file
confinement relies on the process-level sandbox, not fs delegation. (`droid exec
--output-format json` non-acp usage is snake_case:
`{input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens}`.)

## Reconciliation summary

All previously-`# UNVERIFIED` per-turn sites in
`omnigent/inner/droid_executor.py` were reconciled against the captures above:
`session/new sessionId`, the four chunk/tool `sessionUpdate` variants,
`request_permission` options + double-nested outcome reply, `tool_call` /
`tool_call_update` `toolCallId` / `rawInput` / `status` / `rawOutput`, and
`session/cancel` are **CONFIRMED**. Usage extraction was **CORRECTED** (acp emits
no usage; the mapper now also accepts snake_case defensively). Model selection
was **CORRECTED** (`-m` ignored → `session/set_model` wired). Two markers remain,
both justified: the `fs/*` ENOENT mapping and the `usage_update` variant are
**unexercised** because droid never delegates fs and never streams usage.
