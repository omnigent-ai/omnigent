# Files and workspace uploads

The Files rail lets users browse a session's workspace, expand folders, search,
open and edit files, and download them. When workspace uploads are enabled,
editors can pick local files or drop them onto a folder or file row, watch real
progress, and resolve conflicts. Touch layouts expose the same picker and tray
in the Files drawer.

## Sub-features

- `browse`: working folder, expanded subfolders, navigation, hidden files, sorting,
  search results, loading, empty, offline and error states.
- `viewer`: open files from the tree, Changed list, transcript links or shared
  file links; previews, source editing, autosave, download and return to the tree.
- `upload-picker`: multi-select to the browsed folder; keyboard focus returns to
  Upload after selection or cancellation.
- `upload-tree`: folder drops, file drops to their parent, spring-loaded folders,
  root drops and Changed-view root drops; a folder target tints its full row,
  swaps its icon to Upload, while root targets show a dashed edge cue
  that leaves the tree visible in both themes.
- `upload-tray`: committed progress, cancel, retry, conflict decisions, completion
  and announcements; survives a viewer swap, drawer close and session switch.
- `upload-gates`: flag off and viewers see no controls; pending permission waits;
  older runners explain their disabled state; sleeping runners wake on upload.
- `upload-large`: raw binary uploads remain byte-exact and server memory stays
  bounded by chunk size and concurrency.

## How to get to it (user POV)

**Web / desktop:** open a session, expand Workspace, then select Files. Use the
Working folder tree or Changed list, the search and sort controls, folder
expansion or folder navigation. Open a file from either list, a transcript file
link, or a shared file URL to reach the viewer. The desktop app shares these
surfaces; Reveal in file manager is available only on a host with that support.

**Uploads in the rail:** choose Upload in the Files header, or drag local files
onto a folder, a file, the tree's empty space or the header. Rest over a collapsed
folder for 1000 ms to expand it, then drop inside. Folder targets tint their full
row; file rows inside a folder tint their parent folder. Top-level files, empty
space, the header and Changed view show the dashed workspace edge cue, with folder
rows still visible and targetable. Search results target the browsed root; Changed
view targets the workspace root. Select
Cancel or Retry in Uploads, choose Replace / Keep both / Skip for duplicates,
open a file or switch sessions while the transfer runs, or close the rail to see
its persistent progress notification.

**Mobile / touch:** open Conversation actions → Files to reach the drawer; use
Upload and the same progress and conflict controls. File links also open the
mobile viewer. Android and iOS use their native file pickers; desktop drag targets
are a separate entry point from these touch controls.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md), use this
checkout's built UI and an isolated local server and runner. Upload scenarios
below own their environment, enable the workspace-upload feature for that server,
and bind a primary-session runner. Run them with plain `uv run --no-sync pytest`,
`--ui-skip-build --video=on --screenshot=on --output=<evidence-directory>` after
building. Keep dependencies already installed; do not share a build across
checkout revisions. The feature stays disabled by default.

- **Rail browsing and headers:** `tests/e2e_ui/files/test_files_panel_header.py`,
  `tests/e2e_ui/files/test_file_search.py` and
  `tests/e2e_ui/files/test_files_panel_sort.py`; run through the isolated instance.
- **Hover and virtualized scrolling:**
  `tests/e2e_ui/files/test_folder_tree_hover_tooltip_scroll.py` and
  `tests/e2e_ui/files/test_files_scroll_persistence.py`; run through the instance.
- **Viewer and transcript/shared links:**
  `tests/e2e_ui/files/test_file_line_navigation.py`,
  `tests/e2e_ui/files/test_file_link_sharing.py` and
  `tests/e2e_ui/files/test_file_autosave.py`; run through the instance.
  For viewer Find and workspace tab overflow, run
  `tests/browser_ui/files/test_file_find.py` and
  `tests/browser_ui/files/test_workspace_tab_overflow.py` with the built UI;
  these own a mocked backend and need no verification instance.
- **Picker, tray, conflicts, viewer permissions and mobile drawer (own environment):**
  `tests/e2e_ui/files/test_workspace_upload.py` covers binary multi-select, all
  conflict choices, cancellation cleanup, rail/chat exclusivity, near-miss
  navigation prevention, viewer gating and the mobile picker.
- **Folder and file drops, spring-load and preserved expansion/scroll (own environment):**
  `tests/e2e_ui/files/test_workspace_upload_tree.py::test_workspace_upload_tree_folder_file_and_spring_load`.
- **Large nested multi-file drop (own environment):**
  `tests/e2e_ui/files/test_workspace_upload_tree.py::test_workspace_upload_tree_large_binary_byte_exact`.
  The separate `test_workspace_upload_tree_isolated_rss` gates server RSS on one
  process: the 200 MiB peak stays at or below baseline + 3 × chunk × in-flight +
  64 MiB, at most a fixed 32 MiB above a small upload's peak. Within 30 s, Linux
  RSS or macOS malloc in-use bytes (`heap -s` on the same server pid) settle to a
  baseline taken the same way + 64 MiB. Set `WORKSPACE_UPLOAD_TREE_EVIDENCE` to an
  external JSON path to retain source and disk SHA-256, and
  `WORKSPACE_UPLOAD_TREE_RSS_EVIDENCE` to retain baseline, small and large peaks,
  settled RSS and malloc in-use bytes, and chunk and concurrency limits.
- **Sleeping host-bound runner (own environment):**
  `tests/e2e_ui/files/test_workspace_upload.py::test_workspace_upload_asleep_runner_wakes_without_a_message`.
- **Desktop shell, Android and iOS:** repeat the picker from the respective Files
  surface on a device; select two files, cancel one transfer, then upload it again
  and confirm both files appear. iOS multi-select requires a device release check.
- **Older runner, remote host, managed sandbox and real ingress:** use only an
  already-configured host and credentials. On an older runner, verify Upload is
  disabled with an update/restart reason. On a capable remote or managed runner,
  drop multiple files into a nested folder plus a binary of at least 50 MiB and
  compare SHA-256 on disk. Record server baseline, small-upload and large-upload
  peak and settled RSS; the large peak must stay at or below baseline + 3 × chunk ×
  in-flight + 64 MiB and within 32 MiB of the small peak. Within 30 s, Linux RSS
  or macOS malloc in-use bytes must return to baseline + 64 MiB or less. Stop
  for missing credentials, login, new resources or excessive memory. Unavailable
  lanes and ingress binary-frame checks remain manual gates before enabling the
  feature by default.

## Gotchas

- Upload saves to session files; chat drag-and-drop attaches to the next message.
  Only one surface owns feedback. A rail drop must leave the composer empty.
- File rows target their parent; folder rows target themselves. Hover highlights
  clear on leaving, scrolling or unmounting; the actual drop freezes its destination.
- Folder uploads are refused, while sibling files remain accepted. Uploads outside
  the workspace are disabled. Unknown permission is not an edit grant.
- Sleeping sessions show Connecting during wake. Ended or failed sessions need
  reconnecting. Old runners need a host update and session restart.
- Expanded folders and scroll survive commit refreshes. Virtualized offscreen rows
  are absent from the browser; scroll to the destination before expecting it.
- Upload controls disappear with the feature off. Empty and Changed views accept
  root drops when enabled; search-result drops also use the browsed root. Touch devices use the header picker.
- The folder-row Upload menu is deferred until the row-actions menu lands.
- Staging files may be visible to the operating system and agents during a transfer;
  the Files tree hides them until atomic publication.
