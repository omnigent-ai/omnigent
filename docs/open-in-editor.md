# Open workspace files in an editor

The Files panel and file viewer have an outlined **Open** button. Click it to
open the current folder or file in the preferred editor, or use its chevron to
choose VS Code or Cursor. The selection is shared between both controls.

Install the selected editor on the machine where you click the button. The
desktop shell may ask permission to open the editor's protocol link. Local file
links are offered only when the desktop app can identify the session's host as
its own machine. Mobile shells hide the controls.

The link builder supports explicit SSH targets, but the current host API does
not provide a generic SSH target for a remote session. Remote sessions therefore
do not show editor actions. Host display names are not treated as SSH targets.

To verify, open a local session in the desktop app, choose **Files**, and click
**Open** beside the folder path. Confirm the editor selects the local folder.
Open a file in the viewer and repeat, then check the menu remembers the chosen
editor.
