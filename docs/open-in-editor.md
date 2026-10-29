# Open workspace files in an editor

The Files panel and file viewer have an outlined **Open** button. Click it to
open the current folder or file in the preferred editor, or use its chevron to
choose VS Code or Cursor. The selection is shared between both controls.

Remote editor links work in the browser and desktop app. Install the selected
editor and configure its Remote SSH support on the machine where you click the
button. The browser or desktop shell may ask permission to open the editor's
protocol link. Local file links are offered only when the desktop app can
identify the session's host as its own machine. Mobile shells hide the controls.

Hosts advertise their SSH destination when they connect. Standard Arca and
Arclet images automatically report `arca.ssh` and `arclet_<name>.ssh`, using the
image markers and Arclet name file. The corresponding alias must exist in your
local SSH configuration; Arca Companion creates these aliases for Arclets.
Remote links currently support POSIX paths, including Arca and Arclet workspaces.

For another remote host, or a custom alias, set this in the host machine's
`~/.omnigent/config.yaml` (or the config under `OMNIGENT_CONFIG_HOME`):

```yaml
host:
  ssh_target: my-devbox
```

Preserve the other existing host settings. An explicit `ssh_target: null`
disables remote editor links. Reconnect the host after changing this setting;
it is read again on each connection. The value is compatible with Isaac's
`host.ssh_target` setting.

The server keeps this metadata on the live host connection, so no database
migration is needed. Remote controls are hidden when the host is disconnected,
does not report a destination, or belongs to another user. They refresh when
host state changes and periodically while the panel is open. The browser
cannot check whether an editor or SSH alias is installed locally.

To verify, open an Arca or Arclet session, choose **Files**, and click **Open**
beside the folder path. Accept the protocol prompt and confirm the editor's
remote indicator names the intended host. Open a file in the viewer and repeat;
the editor should select that file on the same remote machine. Repeat in the
desktop app and a browser, and check the menu remembers the chosen editor.
