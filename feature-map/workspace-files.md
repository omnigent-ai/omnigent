# Workspace files

Browse a session's workspace from Files. With the file-tree split enabled, a
plain file click previews it beside the tree without adding a tab. The folder
header stays above both columns, and the current file stays highlighted.

## Sub-features

- `gate`: `workspace_file_tree_split_ui` enables the split and its setting; it defaults off.
- `preview`: Files starts as a full-width tree and adds a preview after selection.
- `standalone`: Cmd/Ctrl-click, middle-click and **Open in new tab** open full-width file tabs.
- `resize`: drag the divider or use its arrow keys; the preferred width survives reloads.
- `fallback`: panels narrower than 524px use standalone file tabs, including narrow Canvas panes.
- `close`: the folder's IDE action becomes **Close file** while previewing; closing restores it.
- `drafts`: replacing or closing a dirty preview asks before discarding; hiding it preserves the draft.
- `preference`: **Always open files in a new tab** applies immediately and supports Reset and settings import/export.
- `states`: loading, empty, unavailable and filename-search states retain the existing Files behavior.

## How to get to it (user POV)

- Desktop web and desktop app: open a session's Workspace panel and choose
  **Files**, either in the tab strip or **Select panel** menu. Browse folders
  or search filenames, then click a file. **Full screen** keeps the same layout.
- Canvas: open a session's Workspace pane, choose **Files**, and widen that pane
  enough for a preview. Each session retains its own selected file.
- In Files, right-click a file and choose **Open in new tab**, or use Cmd-click
  on macOS, Ctrl-click elsewhere, or middle-click. Choose **Files** again to
  return to the expanded tree and previous preview.
- Settings → Appearance → Workspace → **Always open files in a new tab** opts
  out of inline previews. Appearance **Reset** restores inline opening.
- Chat citations, agent file opens, Content search results, Changes, explicit
  file URLs and the viewer's previous/next actions continue to open file tabs.
- The desktop composer's PR link can leave Files for Pull Requests. It must
  preserve an unsaved draft until the navigation decision is confirmed.
- Mobile keeps its existing file drawer; it does not show the split.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=workspace_file_tree_split_ui`, use a
session with filesystem access, and build the current SPA. Browser contracts
below use the built app with a mocked backend and need no live server or runner.
For a live drive, use the [verification skill](skills/verify-omnigent/SKILL.md).

- Desktop Files, fullscreen, explicit tab gestures, highlight and preserved
  tree scroll/expansion:
  `tests/browser_ui/files/test_pinned_file_tree.py::test_docked_tree_preview_and_explicit_soft_tabs`.
- Narrow fallback:
  `tests/browser_ui/files/test_pinned_file_tree.py::test_narrow_tree_opens_a_standalone_tab`.
- Minimum tree width, header and search controls, resize/reload, and Close file:
  `tests/browser_ui/files/test_pinned_file_tree.py::test_tree_minimum_width_keeps_controls_separate_and_persists`.
- Actual offline Markdown drafts, responsive hiding, cancellation and discard:
  `tests/browser_ui/files/test_pinned_file_tree.py::test_inline_tree_navigation_guards_unsaved_markdown`.
- Flag and setting gates, exact width boundary, Canvas isolation, external
  navigation and URL restoration are covered in
  `web/src/shell/WorkspacePanel.test.tsx`, `web/src/shell/AppShell.test.tsx`,
  and `web/src/grid/GridSessionPane.test.tsx`. Setting persistence, Reset and
  portability are covered in `web/src/pages/SettingsPage.test.tsx`,
  `web/src/lib/workspaceTabPreferences.test.ts`, and
  `web/src/lib/settingsPortability.test.ts`.
- Existing file journeys in `tests/e2e_ui/files/` retain mobile and standalone
  coverage. For Canvas, manually open two sessions, preview a different file
  in each, resize only one pane, and confirm selection and sizing remain usable.
- For the composer PR transition, select an editable Markdown file in a session
  with an associated PR. Disconnect runner and host while leaving the backend
  reachable, edit the cached document, then click the composer PR link. Cancel
  must keep the draft and URL; approving discard must open Pull Requests.

## Gotchas

- The split belongs to Files. Changes and standalone file tabs remain full width.
- Width is measured inside each pane; a large window can still contain a narrow pane.
- There is no empty preview placeholder. Widening an unused Files view keeps it tree-only.
- A URL containing a file opens a standalone tab on reload, even if that file was
  previously previewed. Returning to Files restores the saved preview.
- The opening preference and divider width are per device. Preview selection is
  per session; neither changes another session's open file tabs.
- Autosave may be unavailable offline or during an external-edit conflict.
  A successful online save does not exercise the unsaved-edit guard.
