# Copy configuration between machines

Save a portable snapshot from an existing machine, then review and import it
on another. Later edits stay local until you explicitly save and import again.
The server must enable preference storage with a stable
`shared_preferences_authority_id`; use your normal server login.
Use a trusted HTTPS server outside local loopback tests: the CLI uses the normal
credential resolver, which can fall back to ambient Databricks credentials.

```sh
omnigent config cloud --server "$SERVER_URL" save
omnigent config cloud --server "$SERVER_URL" show
# On the other machine:
omnigent config cloud --server "$SERVER_URL" import
```

Save and import show before/after values and ask for confirmation. `--yes`
accepts without prompting. A competing cloud save or login change requires a
fresh preview. Failed requests leave the local configuration unchanged.

| Copied | Kept on each machine |
| --- | --- |
| Model and OpenCode model defaults | Provider credentials and login tokens |
| Default harness and ordered startup arguments | Executable commands and host metadata |
| Launcher plugin opt-outs, when supplied | Installed plugins, binaries and project config |

Arguments copy verbatim: review any secrets or local paths in them before saving.
A missing model/default in the snapshot clears that portable setting on import.
Selecting Pi does not install it or make it available on Arclets.

A launcher can include its existing plugin preferences:

```sh
omnigent config cloud --server "$SERVER_URL" \
  --plugin-opt-outs "$PLUGIN_PREFERENCES_FILE" save
```

Set `PLUGIN_PREFERENCES_FILE` to the file supplied by your launcher and use the
same option for import. Omnigent does not install or manage those plugins.
The JSON contains `opted_out` (bare plugin names)
and `disable_all` (freeze automatic plugin management, leaving installed files
alone). Without this option, save preserves existing cloud opt-outs and import
leaves local opt-outs alone. A snapshot without an opt-out block also leaves
local opt-outs unchanged.

`onboard` offers save/import once per interactive server/login context and
remembers a decline. With a server selected, it does nothing without both input
and output terminals. A missing server still produces a configuration error.
Explicit `save` and `import` always remain available. OSS startup does not call
this hook automatically; managed launcher integration is separate.

Import saves private `*.before-import-*` backups next to changed files. If a
write fails, it attempts to restore already-written files. Each file replacement
is atomic; the pair is not a filesystem transaction. Avoid simultaneous edits
from other tools. After an interrupted import, inspect both files and restore
their printed backups if needed.

The API uses `GET`/`PUT /v1/preferences/configuration`, scoped to the authenticated
user and workspace. One `configuration.v1` row contains the snapshot and its
revision. Saves include the current identity and expected revision, reject stale
changes, and do not accumulate history. Portable JSON is limited to 12,000 bytes;
the PUT body is limited to 16,000 bytes. The existing preferences table needs no
new session column or migration.

Imports rewrite YAML formatting and comments, and replace symlinked files with
regular files. Backups retain the original bytes, including any credentials;
they use mode 0600 and remain until you remove them. Imported files also use mode
0600. The local `configuration_import` receipt lets managed launchers preserve
imported defaults during their configuration reconciliation.

The current format rejects unknown fields; future additions need a compatible
protocol/version transition. Older or disabled servers return 404. Request
validation errors use a short `detail` string; streaming request bodies are not
described in OpenAPI. See the [shared API limits](shared-preferences-api.md#storage-and-limits)
for admission behavior shared by both APIs.
