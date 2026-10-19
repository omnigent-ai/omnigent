# Run Code Blocks, PR 4: Web UI

Spec: `docs/superpowers/specs/2026-10-16-code-block-run-design.md` (issue #10519).
PRs 1-3 provide the `code_run` item, the start/stop routes, the four
`session.code_run.*` events, the `code_block_run` flag in `/v1/info`, and agent
delivery. This PR is web only, plus a Python parity test for the harness list
and browser e2e tests.

Every task starts with a failing test.

## Spec points and where they land

| Spec point                                                                                                                          | Task    |
| ----------------------------------------------------------------------------------------------------------------------------------- | ------- |
| Run button next to Copy, runnable tags and untagged `$ ` blocks                                                                     | 1, 5, 6 |
| Shown only when flag on, owner, session has a runner, message finished                                                              | 4, 5, 6 |
| Disabled with tooltip in copy-on-write sessions                                                                                     | 2, 5    |
| First-run confirmation, exact command and language, saved/visible/sent note                                                         | 1, 5    |
| Confirmed state per session, per device, in browser storage                                                                         | 1       |
| Policy ASK reopens the dialog with the reason, resends `approved: true`                                                             | 5       |
| Bottom sheet on narrow screens                                                                                                      | 5       |
| Live output, stderr apart, ANSI kept, notebook output renderer                                                                      | 4, 5    |
| Status line: running+elapsed, exited code+duration, stopped, timed out, failed, refused+reason, interrupted, waiting for the runner | 2, 5    |
| Output cut note, Stop while running                                                                                                 | 5       |
| Run again replaces the panel, "Show N earlier runs"                                                                                 | 2, 5    |
| Never a timeline card; refusals only on the trying device                                                                           | 2, 3    |
| Narrow screens: full width, horizontal scroll, max height + expand, touch targets                                                   | 5       |
| Viewers and collaborators see the panel without Run/Stop                                                                            | 5, 7    |
| Fallback note + Add output to message (fenced block into composer)                                                                  | 1, 5, 6 |
| Harness list client copy kept in sync by a test, no new endpoint                                                                    | 1       |
| Block identity: message id + SHA-256 of raw block text                                                                              | 1, 6    |
| De-dup live output by offset                                                                                                        | 2       |
| Hydrate from saved items after reload, from `code_run.snapshot` on reconnect                                                        | 2, 3    |
| Started-without-finished: running / interrupted / waiting for the runner                                                            | 2, 3, 5 |
| Testing: web bullets and e2e bullets                                                                                                | all, 7  |

## Task 1: pure helpers, `web/src/lib/codeRuns.ts`

- `sha256Hex(text)`: synchronous SHA-256 of the UTF-8 text. `crypto.subtle`
  only exists in secure contexts, and a LAN `http://` server is not one.
- `normalizeBlockText(text)`: CRLF to LF and trailing newlines dropped, as the
  server's `_normalize_block_text`, so the hash matches the server's check.
- `codeRunLanguage(tag, code)`: `bash`/`sh`/`shell`/`zsh`/`console` to `shell`,
  `python`, `javascript`/`node` to `node`; untagged runs as shell when every
  non-empty line starts with `$ `; else `null`.
- `NATIVE_HARNESSES` and `CODE_RUN_NOTICE_NATIVE_HARNESSES` client copies and
  `harnessReceivesCodeRuns(harness)`. A Python test checks them against
  `omnigent.harness_aliases.NATIVE_HARNESSES` and the server's
  `_CODE_RUN_NOTICE_NATIVE_HARNESSES`, the way
  `test_client_server_filesystem_extension_parity` does.
- `deriveCodeRunCommand(language, code, console)`: the server's prompt
  stripping, so the dialog shows the exact command.
- `isCodeRunConfirmed(sessionId)` / `confirmCodeRuns(sessionId)` in
  `localStorage`.
- `codeRunComposerText(run)`: command, exit status, and trimmed output as a
  fenced block.
- `startCodeRun` / `stopCodeRun` API calls returning the server's
  `{code, reason}` refusals as values.

Tests: `codeRuns.test.ts` (hash vectors vs Node `crypto`, language matrix,
harness rule, storage, composer text, API refusal parsing) and
`test_web_harness_lists_match_the_server` in
`tests/server/test_code_run_delivery.py`.

## Task 2: run store, `web/src/store/codeRunStore.ts`

Zustand store keyed by session then run id.

- `ingestItems(sessionId, items)`: saved `code_run` items (both phases).
- `started` / `finished` events upsert; `finished` output is authoritative.
- `output(offset, stream, text)`: drop what is already held, append the rest.
- `snapshot(offset, output)`: replace output only when it is ahead.
- `streamOpened(sessionId)` clears per-connection liveness;
  `listingSettled(sessionId)` marks that the connect snapshot is done.
- Local-only refusals per block and the learned copy-on-write flag.
- `runStatus(run, session)`: running / interrupted / waiting for the runner
  for a run without `finished`.
- `blockRuns(sessionId, messageId, blockHash)`: newest first.

Tests: `codeRunStore.test.ts`.

## Task 3: stream and history wiring

- `sse.ts` parses the four events into `session_code_run_*` stream events
  (`events.ts` types).
- `handleSessionEvent` routes them to the store for the delivering stream;
  `session_changed_files_invalidated`, which the server sends right after the
  connect snapshot's code runs, settles the listing.
- `pumpStreamEvents` calls `streamOpened` per connection.
- The item-loading sites feed `ingestItems`; `itemsToBlocks` still drops
  `code_run` items, so they never become timeline cards.

Tests: `sse.test.ts` parse cases; a `chatStore` routing case.

## Task 4: shared pieces

- Export the notebook `OutputView` with a `nowrap` option (horizontal scroll).
- `useIsOwner(conversationId)` next to `useCanEdit`.
- `code_block_run` in `FeatureKey`.

Tests: `NotebookPreview.test.tsx` (nowrap), `usePermissions` owner case.

## Task 5: `web/src/components/chat/CodeRunPanel.tsx`

Kept out of `components/ai-elements`, which the linter skips as vendored code.

- `CodeRunButton` (header, next to Copy) and `CodeRunPanel` (under the block).
- Confirmation dialog; `needs_approval` reopens it with the reason; bottom
  sheet under the `md` breakpoint.
- Panel: status line, notebook output, output cut note, Stop, earlier runs,
  fallback note and Add output to message.

Tests: `CodeRunPanel.test.tsx`.

## Task 6: chat code block and composer wiring

- `BlockRenderer` provides the message id for finished, persisted text.
- `ChatCodeBlockPre` reads the language and code, renders the button and panel.
- `addComposerText` on the chat store, drained into the composer like
  `pendingComposerAttachments`.

Tests: `message.test.tsx`, `chatStore.test.ts` composer queue.

## Task 7: e2e, `tests/e2e_ui/chat/test_code_block_run.py`

A dedicated `server_runner` stack with `OMNIGENT_FEATURES=code_block_run`, a
seeded assistant message with a shell block:

- run, see output, stop a long run, reload and find both saved;
- a viewer (permission level 1 on the session snapshot) sees output, no Run;
- a phone-sized viewport run.

## Decisions made while building

- Copy-on-write is known only to the runner, so the button learns it from the
  server's `409 copy_on_write` refusal and is then disabled with the tooltip
  for the rest of the page's life. Knowing it up front needs a server field.
- Cancelling the first-run confirmation starts nothing and shows nothing.
  Cancelling a policy ASK shows "Refused: <reason> (not approved)" on this
  device only.
- Add output to message is offered to viewers who can edit (they own a
  composer); Run and Stop stay owner-only.
- The viewer e2e rewrites the session snapshot's permission level to 1, since
  the local test server is single-user; the server's owner gate is covered by
  PR 2's route tests.

## Task 8: verification

`pnpm install --frozen-lockfile`, `node_modules/.bin/tsc -b`, oxlint,
prettier `--check`, vitest on changed tests plus code-block, message and
notebook preview tests, pre-commit on changed files, the e2e file.
