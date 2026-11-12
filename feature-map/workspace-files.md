# Workspace files

Browse a session's workspace from Files. With the file-tree split enabled, a
plain file click shows the file beside the tree without adding a tab. The
folder header spans both columns, and the shown file stays highlighted.

## Sub-features

- `gate`: `workspace_file_tree_split_ui` enables the split and its setting; it defaults off.
- `preview`: Files starts as a full-width tree and adds the file pane only after a click.
- `standalone`: Cmd/Ctrl-click, middle-click and **Open in new tab** open full-width file tabs.
  With **Always open files in a new tab** on, every click opens a tab as before, and
  middle-click and **Open in new tab** are not offered.
- `resize`: drag the divider or use its arrow keys; the preferred width survives reloads.
- `fallback`: Files narrower than 524px keeps a full-width tree and opens clicked files in tabs.
- `close`: while a file is shown, the folder's Open-in action becomes **Close file**.
- `drafts`: replacing or closing an unsaved file asks first; hiding it keeps the draft.
- `access`: losing file access hides the tree, but an open file keeps its editor and draft.
- `preference`: **Always open files in a new tab** applies immediately and supports Reset and settings import/export.
- `states`: loading, empty, unavailable and filename-search states keep the existing Files behavior.

## How to get to it (user POV)

- Desktop web and desktop app: open a session's Workspace panel and choose
  **Files** in the tab strip or the **Select panel** menu. Browse folders or
  search filenames, then click a file. **Full screen** keeps the same layout.
- Grid: open a cell's Workspace, choose **Files**, and widen it enough for the
  split. Each session keeps its own shown file.
- In Files, right-click a file and choose **Open in new tab**, or use Cmd-click
  on macOS, Ctrl-click elsewhere, or middle-click. Choose **Files** again to
  return to the expanded tree and the file beside it.
- Close the file beside the tree with the header's **Close file** button, with
  Escape (not while Find is open or focus is in a text field; in a Grid cell, only
  after working in the file), or with the desktop app's Cmd/Ctrl+W while the
  Workspace has focus.
- At narrow widths, the header's **Folder actions** menu holds **Copy folder path**
  and **Refresh files**.
- Settings → Appearance → Workspace → **Always open files in a new tab** opts
  out of the split. Appearance **Reset** turns it off again.
- Settings → Appearance → Settings data → **Export** saves the preference;
  **Import** restores it and updates Workspace panels already on screen.
- Chat citations, agent file opens, Content search results, Changes, file
  links (`?file=`) and the viewer's previous/next actions keep opening file tabs.
- Mobile keeps its file drawer and full-screen viewer; it has no split.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=workspace_file_tree_split_ui`, use a
session with filesystem access, and build the current SPA. The browser contracts
below use the built app with a mocked backend and need no live server or runner.
For a live drive, use the [verification skill](skills/verify-omnigent/SKILL.md).

- Split, highlight, explicit tab gestures, preserved tree scroll and expansion,
  full screen, live narrowing and the narrow header:
  `tests/browser_ui/files/test_workspace_file_tree.py::test_tree_beside_file_routes_gestures_and_keeps_tree_state`.
- Minimum tree width, header and search geometry, divider reload, link restore
  and Close file:
  `tests/browser_ui/files/test_workspace_file_tree.py::test_tree_minimum_width_keeps_controls_separate_and_persists`.
- A real offline Markdown draft hidden on a phone width, kept in its own tab,
  and then discarded once:
  `tests/browser_ui/files/test_workspace_file_tree.py::test_closing_a_previewed_files_tab_discards_its_real_draft_once`.
- Access loss, the width boundary, the setting, Grid isolation, guarded
  navigation, the composer's Pull Requests link and link restore are covered in
  `web/src/shell/WorkspacePanel.test.tsx`, `web/src/shell/AppShell.test.tsx`,
  and `web/src/grid/GridSessionPane.test.tsx`. Setting persistence, Reset and
  portability are covered in `web/src/pages/SettingsPage.test.tsx`,
  `web/src/lib/workspaceTabPreferences.test.ts`, and
  `web/src/lib/settingsPortability.test.ts`.
- Manually, for access loss: as a collaborator with Edit access and workspace
  file sharing off, leave an unsaved Markdown draft open, then have the owner
  downgrade you to Read. The draft stays but can't be edited or saved;
  restoring Edit keeps it.

## Gotchas

- The split belongs to Files. Changes and file tabs stay full width.
- Width is measured inside each panel; a large window can still hold a narrow Grid cell.
- There is no empty file pane. Widening an unused Files view keeps it tree-only.
- A `?file=` link opens a file tab on reload, even for the file shown beside the
  tree. Choosing Files then shows that file beside the tree again.
- The file beside the tree stays loaded behind other tabs, so switching tabs
  never asks about it; replacing or closing it does.
- The preference and divider width are per device. The shown file is per session.
- Autosave may be unavailable offline or during an external-edit conflict.
  A successful online save does not exercise the unsaved-edit guard.
