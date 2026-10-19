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

## How to get to it (user POV)

- `files`: open a session's Workspace panel, select Files, then a Markdown file.
- `changes`: select a changed Markdown file, then exit diff to edit it.
- `direct-link`: open a session URL with a selected Markdown file.
- `chat-link`: follow a Markdown file link in a message; a line citation opens
  Source at that line, rather than the rich editor.
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
   inside it and autosave submits the changed Markdown.
3. Reload and verify the edit is returned by the mock filesystem.
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
frames. The load runs use real time and no slow-motion or simulated clock.
Report repeated samples for performance decisions; a single video is illustrative.
This does not cover every Markdown shape or establish a safe 1 MiB limit.

### Complementary correctness journeys

- `files`, `read`, and `modes`:
  `tests/e2e_ui/files/test_markdown_rich_rendering.py::test_markdown_renders_rich_constructs_and_source_toggle`
  opens from Files and checks rich constructs, reflow, copying, and Source.
- `direct-link`, `edit-save`:
  `tests/e2e_ui/files/test_file_autosave.py::test_markdown_edit_autosaves`
  confirms the edit through a fresh filesystem API read from the real server.
- `files`, `review`:
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_markdown_rich_text_editor_add_comment`
  submits a comment and checks its stored anchor. The heading variant is
  `tests/e2e_ui/comments/test_markdown_editor_comments.py::test_heading_text_anchor_content_excludes_prefix`.
- `document-link`:
  `tests/e2e_ui/files/test_markdown_editor_link_popup.py::test_editor_link_popup_opens_relative_link_from_keyboard`
  opens a relative file from the keyboard-accessible link popup.
- `chat-link`, `offline`:
  `tests/browser_ui/files/test_file_line_navigation.py::test_citation_preserves_offline_markdown_draft_with_diff_preference`
  exercises a citation, offline editing, and the dirty-navigation guard. Its
  health polling uses simulated time; it is not a performance sample.
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

- File editing uses TipTap. Chat Markdown, read-only Preview, and Source use
  different rendering paths; speeding up one does not validate the others.
- Task-list tokenization and table-start detection bound the source passed to
  TipTap 3.31.4. `web/src/shell/tiptapBoundedTokenizers.test.ts` checks upstream
  token compatibility and line-splitting volume using the CUJ fixture. Remove
  these bounds when upstream avoids the repeated whole-remainder scans.
- Load measurements do not establish typing latency. The editor currently
  serializes the full document on each edit to detect unsaved changes.
- Browser-contract saves update an in-memory mock filesystem. A successful
  reload there does not prove server/runner persistence or external-file edits.
- Desktop and mobile can have hidden viewer instances on older revisions.
  Interact with the visible viewer and report total editor count separately.
- Resizing saved content is not evidence that unsaved edits or comments survive.
- A phone-sized Chromium viewport is not native iOS keyboard, Safari, touch,
  or Android verification. Record those gaps explicitly when relevant.
- The 64/256 KiB probes are report-only. Readiness checks can fail, but there is
  no machine-dependent latency assertion and no silent plain-text fallback.
