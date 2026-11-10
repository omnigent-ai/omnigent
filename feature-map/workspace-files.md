# Workspace file tree

The desktop Workspace Files tab can show the folder tree beside the selected
file. Users browse files without adding tabs, resize the tree, or open a file
in its own tab when they want a standalone view.

## Sub-features

- `inline`: a plain tree-file click replaces the adjacent preview without adding
  a tab; before selection, the viewer asks the user to select a file.
- `tabs`: Cmd-click on macOS, Ctrl-click elsewhere, middle-click, or the file's
  **Open in new tab** context-menu action opens a standalone file tab.
- `resize`: drag the divider or focus it and use the arrow keys; the tree width
  persists on this device across sessions and reloads.
- `narrow`: a panel too narrow for both columns shows the full-width tree and
  opens files in tabs; widening it restores the split.
- `remember`: returning to Files restores the expanded tree and inline preview;
  replacing a dirty file goes through the editor's unsaved-changes prompt.
- `preference`: **Always open files in a new tab** restores the full-width tree;
  the setting applies immediately, persists on this device, and resets with Appearance.
- `gate`: the split and its setting require `workspace_file_tree_split_ui`.

## How to get to it (user POV)

- Desktop web and desktop app: open a session's Workspace panel and choose
  **Files**, either in the tab strip or through **Select panel**.
- Full screen: choose **Full screen** from Workspace; Files uses the same split.
- Session grid: open Files in a session pane's Workspace panel; it splits when
  that pane has enough room.
- Settings: open **Settings → Appearance → Workspace** and toggle **Always open
  files in a new tab**. Appearance's **Reset** restores inline opening.

## Driving it with the repro environment

Preconditions: enable `OMNIGENT_FEATURES=workspace_file_tree_split_ui` on the
test server and use a session with accessible workspace files. The browser tests
below own their environment and use a built SPA with mocked backend responses.

- Docked/full-screen tree navigation, narrow fallback, new-tab actions, divider
  persistence and minimum-width controls:
  `tests/browser_ui/files/test_pinned_file_tree.py`.
- Tab-strip sizing and full-screen transitions:
  `tests/browser_ui/files/test_workspace_tab_overflow.py`.
- Unsaved-editor navigation:
  `tests/browser_ui/files/test_hidden_file_navigation.py`.
- Component layout and file-opening routes:
  `web/src/shell/WorkspacePanel.test.tsx` and `web/src/shell/AppShell.test.tsx`.
- Feature defaults and advertised gates:
  `tests/server/test_feature_flags.py` and
  `tests/server/integration/test_utility_endpoints.py`.

For the picker and session-grid entry points, choose Files, widen the panel,
expand a folder and click two files. Confirm the preview changes beside the tree
without adding tabs. Narrow the panel until the split disappears and confirm a
file click now opens a standalone tab. Repeat in full screen.

For Settings, turn **Always open files in a new tab** on and return to Files:
the tree should fill the panel and clicks should create tabs. Reload to check
persistence, then use Appearance's **Reset** and confirm inline opening returns.
Restart without the feature flag and reload: the setting is absent and every
tree-file click opens a standalone tab.

## Gotchas

- The split belongs to Files. Changes, content-search results, chat file links,
  agent-opened files and URL file deep links still open standalone tabs.
- Reloading with a file in the URL selects a standalone tab, even when it matches
  the saved preview. Choose Files to return to that preview. The viewer's previous
  and next file actions also open standalone tabs.
- A standalone tab fills the panel. Choose Files to return to the tree and its
  last inline preview; opening a tab does not collapse the tree's folders.
- The split requires roughly 524 pixels. A narrow panel or grid pane falls back
  to tabs even when the feature is enabled and the preference is off.
- Mobile continues to use its existing file drawers.
- The setting and tree width are local to this browser/device, not account-wide.
