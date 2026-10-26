# Markdown file editor

Users read and edit Markdown files in the workspace viewer, search their text,
switch between Edit, Source, and Preview, and attach comments to selections.
This map defines the repeatable critical user journey (CUJ) for editor changes
and the recording protocol for comparing revisions.

## Sub-features

- `read`: headings, lists, tasks, tables, links, quotes, and fenced code render.
- `edit-save`: typing preserves the caret and autosave survives reopening.
- `find`: search finds edited content without typing into the document.
- `modes`: Edit, Source, and Preview retain the same saved content.
- `review`: selected text anchors a comment; an unsent draft remains editable.
- `responsive`: desktop and mobile show the file and retain edits and drafts.
- `load`: fixed mixed-content documents expose parsing and rendering costs.
- `offline`: unsaved edits remain guarded when the workspace cannot save.
- `large-files`: files above 1 MiB open as editable source; truncated files
  remain read-only, with full-file download available.

## How to get to it (user POV)

- `files`: open a session's Workspace panel, select Files, then a Markdown file.
- `changes`: select a changed Markdown file, then exit diff to edit it.
- `direct-link`: open a session URL with a selected Markdown file.
- `chat-link`: follow a Markdown file link in a message; a line citation opens
  Source at that line, rather than the rich editor, and a range (`:12-18`,
  `#L12-L18`) highlights every cited line. Right-clicking or long-pressing the
  link offers Copy path, which copies its path (absolute when the workspace root
  is known).
- `tool-row`: click the path on an agent's Read, Write, or Edit row for a
  Markdown file, including one the agent just created; absolute paths open too.
- `agent-api`: ask the agent to use `ui_open_file` with a workspace-relative
  Markdown path, or call the [file-opening endpoint](../docs/agent-ui-api.md).
  Only current session viewers (the selected pane in grids) open the file;
  background requests are not replayed.
- `document-link`: follow a relative file link inside the editor's link popup.
- `mobile`: use the session Files surface or a file link at a phone width.

## Driving it with the repro environment

Preconditions: follow the [Verify Omnigent skill](skills/verify-omnigent/SKILL.md).
Build the exact checkout and use its frozen dependency lock. The browser-contract
journeys below need no server: they run the production SPA with a sealed mock
backend, a fresh browser context, and a fixed Markdown fixture. Server-backed
journeys require an isolated verification instance.

### Standard comparison journey

`tests/browser_ui/files/test_markdown_editor_journey.py::test_markdown_editor_review_journey`
starts at `direct-link` and performs these actions in order:

1. Read the heading, ordered list, table, and original code block.
2. Create another Python code block and type two lines; verify the caret stays
   inside it. Change the original block's language with the pointer and the new
   block's language with the keyboard, then verify autosave submits the changes.
3. Reload and verify the edit and fence language are returned by the mock
   filesystem.
4. Find the edited text and verify exactly one match.
5. Switch through Source, Preview, and Edit, retaining the changed text.
6. Resize from desktop to phone and back, retaining the saved content.
7. Double-click "Review" in the first paragraph, verify the comment anchors to
   that word, and start an unsubmitted comment; end with its text focused.

The fixture is
[markdown_editor_journey.md](../tests/browser_ui/files/markdown_editor_journey.md).
The test fixes Chromium's viewport and recording size at 1600 x 1000, scale 1,
light color scheme, en-US, and UTC. Phone width is 390. Use the same test and
fixture revision on both builds. These actions exercise saved-content resizing;
they do not claim unsaved-draft preservation across a resize.

### Record before and after

Use separate worktrees and evidence directories for each revision. Apply only
the same CUJ test/fixture changes to older revisions that lack them. Rebuild each
SPA, use the same browser binary and machine, and run comparisons sequentially.
Run from each worktree root, changing only `CUJ_EVIDENCE` between runs:

```sh
CUJ_EVIDENCE=/tmp/markdown-editor-cuj/before
mkdir -p "$CUJ_EVIDENCE"
git rev-parse HEAD > "$CUJ_EVIDENCE/revision.txt"
git diff --binary > "$CUJ_EVIDENCE/working-tree.patch"
sha256sum pnpm-lock.yaml tests/browser_ui/files/markdown_editor_journey.md \
  tests/browser_ui/files/test_markdown_editor_journey.py \
  > "$CUJ_EVIDENCE/inputs.sha256"
pnpm --filter web run build
uv run --no-sync pytest \
  tests/browser_ui/files/test_markdown_editor_journey.py::test_markdown_editor_review_journey \
  --browser-ui-skip-build --browser=chromium --video=on --slowmo=250 \
  --screenshot=on --tracing=on --reruns=0 --output="$CUJ_EVIDENCE/review"
uv run --no-sync pytest \
  tests/browser_ui/files/test_markdown_editor_journey.py::test_markdown_editor_load_journey \
  --browser-ui-skip-build --browser=chromium --video=on \
  --screenshot=on --reruns=0 --output="$CUJ_EVIDENCE/load"
```

Use `after` for the candidate. Compare corresponding clips at normal playback
speed; preserve the waiting period when the UI stalls. Do not splice out waits
or use the deliberately slowed review clip as a performance measurement.
Attach the videos to the PR with `gh pr edit --attach`, identifying the revision,
entry point, fixture, and whether APIs were mocked. Keep artifacts out of Git.

`test_markdown_editor_load_journey` repeats the same fixture to roughly 64 and
256 KiB. Each test writes `markdown-load.json` beside its recording: fixture hash,
actual bytes, browser version, viewport, editor count, selection-read count,
longest main-thread task, and navigation-to-ready time through two animation
frames. Readiness waits directly for the visible editor's terminal marker;
locator assertions can build a full accessibility snapshot and inflate timings.
Results carry `readiness_probe: terminal-marker`; older results without this
field include assertion overhead and must be remeasured for comparisons.
The load runs use real time and no slow-motion or simulated clock.
Report repeated samples for performance decisions; a single video is illustrative.
This does not cover every Markdown shape or establish a safe 1 MiB limit.

### Complementary correctness journeys

- `files`, `read`, and `modes`:
  `tests/e2e_ui/files/test_markdown_rich_rendering.py::test_markdown_renders_rich_constructs_and_source_toggle`
  opens from Files and checks rich constructs, reflow, copying, and Source.
- `files`, oversized read-only content:
  `tests/e2e_ui/files/test_right_panel.py::test_file_viewer_download_saves_the_complete_file`
  includes a single-line Markdown file beyond the server read cap. Its loaded
  prefix must render read-only, and downloading must preserve the entire file.
- `direct-link`, `large-files`, `edit-save`:
  `tests/e2e_ui/files/test_file_autosave.py::test_large_markdown_source_autosaves`
  edits a complete file above 1 MiB in source, verifies every saved byte through
  the real server, finds its final line, and shrinks it below the limit. Source
  stays mounted until reload, which then restores the rich editor.
- `chat-link`, `large-files`, `mobile`:
  `tests/browser_ui/files/test_file_line_navigation.py::test_large_markdown_citation_and_unsaved_resize`
  follows a final-line citation, rejects saves, and preserves the unsaved source
  editor through phone/desktop resizing and the mobile close guard.
- `direct-link`, `edit-save`:
  `tests/e2e_ui/files/test_file_autosave.py::test_markdown_edit_autosaves`
  confirms the edit, undo, and redo through fresh filesystem API reads from
  the real server, then reloads the saved edit.
- `files`, `review`:
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_markdown_rich_text_editor_add_comment`
  submits a comment and checks its stored anchor. The heading variant is
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_heading_text_anchor_content_excludes_prefix`.
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_repeated_word_keeps_selected_occurrence_after_reopen`
  checks exact raw endpoints and the highlighted occurrence after save/reopen,
  including a word split across formatting beside a hidden URL and a plain copy,
  a bold word in an ordered-list parent followed by a nested ordered list,
  and a repeated bold word outside a table containing an escaped pipe.
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_mixed_structures_accumulated_comments_after_autosave`
  edits the mixed fixture, waits for real autosave, then comments on every
  duplicate through the UI, selecting the nested-list parent last. It checks
  persisted raw endpoints and every highlight again after a full page reload.
  This covers accumulated decorations and serialization, unlike single-comment
  tests or parser-only input matrices.
  `tests/browser_ui/files/test_markdown_editor_journey.py::test_markdown_comment_boundary_save_reload`
  checks raw endpoints and highlights after saving/reloading selections ending
  on a CRLF soft break, hard break, or image next to duplicate text.
  `test_markdown_existing_comment_after_autosave` checks an existing highlight
  before clicking it or adding another comment.
  `test_markdown_source_comment_after_external_edit` reloads externally changed
  text and checks Source-mode ranges with formatting retained or removed.
  These browser-contract cases reuse the file journey's mock backend; they do
  not establish server persistence. Parser input matrices remain in unit tests.
- `agent-api`, desktop and phone-sized browser:
  `tests/e2e_ui/files/test_right_panel.py::test_agent_opens_workspace_file`
  drives a scripted agent tool call through the runner, server and stream into
  the real file viewer. Grid targeting and dirty-navigation guards are covered
  by `web/src/grid/GridSessionPane.test.tsx`; native app/device runs remain manual.
- `document-link`:
  `tests/e2e_ui/files/test_markdown_editor_link_popup.py::test_editor_link_popup_opens_relative_link_from_keyboard`
  opens a relative file from the keyboard-accessible link popup.
- `chat-link`, `offline`:
  `tests/browser_ui/files/test_file_line_navigation.py::test_citation_preserves_offline_markdown_draft_with_diff_preference`
  exercises a citation, offline editing, and the dirty-navigation guard. Its
  health polling uses simulated time; it is not a performance sample.
- `chat-link` Copy path: the "copying a file link's path" component tests in
  `web/src/components/blocks/ChatMarkdown.fileLinks.test.tsx` cover openable,
  missing, and unverified links. Manually long-press a link in the mobile apps
  and right-click one in the desktop app; neither should open the file.
- `changes`: manually open Markdown from Changes, exit diff, edit and save,
  then re-enter diff and verify the saved edit. The standard CUJ starts from a
  direct link; it does not establish this entry point's correctness.
- `mobile`, unsaved `responsive`: record starting at both desktop and phone
  widths. Create an unsent comment, resize in both directions, and verify its
  text, focus, and selection. Repeat with an offline edit and the mobile Back
  dirty guard. These are separate from the saved-content resize above; the
  automated regressions are in https://github.com/omnigent-ai/omnigent/pull/10715.

Record the server-backed tests through `verify-env run`, using `--ui-skip-build`,
`--video=on`, `--slowmo=250`, `--screenshot=on`, and `--reruns=0`. Keep each test's
fixture and viewport unchanged between revisions. List any entry point not run
instead of treating one comparison clip as complete feature verification.

## Gotchas

- Comment offsets remain raw-source ranges; the quote is rendered text. A
  selection such as `wo**rd**` can span markup, and its minimal source range
  ends at the last selected character, before the closing `**`. Comments whose
  raw ranges span markup are remapped through the same parser in Edit, Source,
  and Diff views,
  preferring an exact stored range, then the closest verified source occurrence.
  Unsupported or oversized documents retain stored ranges in Source mode.
  If the remapping module cannot load, Source and Diff use the legacy text search.
  Equal-length source ranges still use legacy search after external edits,
  including selections entirely inside bold or italic delimiters.
  This does not repair previously mis-saved offsets or guarantee tracking
  through arbitrary subsequent edits.
- Older clients can display a formatted comment on a plain literal copy.
  Refresh those tabs to load the source-map behavior; this display difference
  does not overwrite the comment's saved offsets.
- The automatic source fallback uses UTF-8 bytes, not JavaScript string length.
  It applies above 1 MiB regardless of the saved view preference. The choice
  stays fixed for that file visit so an autosave cannot replace an editor with
  newer unsaved edits. Reopen another file and return, or reload, to reassess.
  This is an initial-load guard, not a live cap on edits or external updates.
- File editing uses TipTap. Chat Markdown, read-only Preview, and Source use
  different rendering paths; speeding up one does not validate the others.
- Task-list tokenization and table-start detection bound the source passed to
  TipTap 3.31.4. `web/src/shell/tiptapBoundedTokenizers.test.ts` checks upstream
  token compatibility and line-splitting volume using the CUJ fixture. Remove
  these bounds when upstream avoids the repeated whole-remainder scans.
- Load measurements do not establish typing latency. Dirty detection compares
  immutable editor documents; autosave still serializes the whole document.
  Changes that serialize identically become clean when autosave checks them.
- Setext-heading detection uses a line scanner with marked 17 interruption
  rules. Compatibility tests compare it with upstream; remove the workaround
  when upstream handles long lines without exhausting the regex stack.
- Browser-contract saves update an in-memory mock filesystem. A successful
  reload there does not prove server/runner persistence or external-file edits.
- Desktop and mobile can have hidden viewer instances on older revisions.
  Interact with the visible viewer and report total editor count separately.
- Resizing saved content is not evidence that unsaved edits or comments survive.
- A phone-sized Chromium viewport is not native iOS keyboard, Safari, touch,
  or Android verification. Record those gaps explicitly when relevant.
- The 64/256 KiB probes are report-only. Readiness checks can fail, but there is
  no machine-dependent latency assertion and no silent plain-text fallback.
