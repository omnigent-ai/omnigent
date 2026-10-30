# Open workspace files in an editor

The Files panel and file viewer have an outlined **Open** button. Click it to
open the current folder or file in the preferred editor, or use its chevron to
choose VS Code or Cursor. The selection is shared between both controls.

Remote editor links work in the browser and desktop app. Install the selected
editor and configure its Remote SSH support on the machine where you click the
button. The browser or desktop shell may ask permission to open the editor's
protocol link. Local file links are offered only when the desktop app can
identify the session's host as its own machine. Mobile shells hide the controls.
The browser uses a normal link to invoke the editor, so the page's `frame-src`
policy does not need to allow editor protocol schemes.

Remote controls require the host to be online. The UI infers standard SSH
aliases from the existing host list: provider
`databricks-arca` uses `arca.ssh`, and provider `arclet` uses
`arclet_<host.name>.ssh`. Arclet names follow Arclet's existing validation:
1–64 ASCII letters, digits, underscores, or hyphens; a letter or digit first;
and no `arclet_` or `arclet-` prefix. The alias must exist in your local SSH
configuration; Arca Companion creates these aliases for Arclets.
For user-connected hosts without a sandbox provider, the UI reads
`/etc/metric_proxy/labels/default` through the existing authenticated session
file API. A unique `arca_type arca` label selects `arca.ssh`. For
`arca_type arclet`, it reads `/opt/arclet/arclet-name` and uses the validated
canonical name for `arclet_<name>.ssh`. Display names, including IP-based
hostnames and custom names, do not determine the machine type. Other types,
including `isaac-arca`, are not treated as personal Arcas.
Discovery is cached per session and host for five minutes and shared by the
folder and file controls. Clicking Open performs no metadata request.
Remote links currently support POSIX paths, including Arca and Arclet workspaces.

Other remote providers, custom aliases, missing or offline host rows, invalid
Arclet names, and unreadable or malformed metadata hide the remote controls.
The existing file API's session permissions and filesystem reach still apply;
the UI does not bypass them. Cached discovery cannot keep an offline host's
controls visible. Host-list updates refresh availability and provider aliases.
No new host protocol, server API, or database migration is required. The browser
cannot check whether an editor or SSH alias is installed locally.

To verify, open an Arca or Arclet session, choose **Files**, and click **Open**
beside the folder path. Accept the protocol prompt and confirm the editor's
remote indicator names the intended host. Open a file in the viewer and repeat;
the editor should select that file on the same remote machine. Repeat in the
desktop app and a browser, and check the menu remembers the chosen editor.
