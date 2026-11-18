# Data-handling controls

Operators of regulated deployments can keep tool outputs out of every session export, refuse file downloads, and serve the web UI under a strict Content-Security-Policy. All three are off by default and set in the server config's `data_controls:` block or by environment variable (see [the deployment guide](../deploy/README.md#data-handling-controls-for-regulated-deployments)). `GET /v1/info` reports them as `data_controls`, and the web UI hides download buttons and labels exports to match.

## Sub-features

- `export-outputs-request`: With the default `include_outputs: request`, an export carries tool outputs unless the request asks to leave them out (`?outputs=false`).
- `export-outputs-never`: With `include_outputs: never`, every export replaces tool outputs, terminal and slash-command output and provider-native tool results with a placeholder, drops compactions' transcript copies, and reduces unknown item types to their ids. Its `session_meta` line says `"outputs_omitted": true`, and the web Export and the CLI say so.
- `export-legacy-server`: Only when the export route answers 404 (an older server) do the web and CLI build the JSONL themselves, as before; any other refusal fails the export.
- `downloads-off`: With `allow_downloads: false`, the full-file download stream answers 403 "Downloads are turned off on this server.", and the Files tree, Changes list, file viewer menu and recording players show no Download control.
- `downloads-off-previews`: Text, image, PDF and notebook previews keep working through the capped viewer read; workspace recordings show that playback is unavailable; remote recordings still play.
- `csp-enforce`: With `content_security_policy: enforce`, every HTML response of the web UI carries the policy and the app runs without reported violations.
- `csp-report-only`: With `report-only`, the same policy is sent as `Content-Security-Policy-Report-Only`, so violations are only logged.
- `csp-html-preview`: Under either mode, the HTML preview loads its comment bridge from a file, since the preview frame inherits the policy; its same-page link helper runs by hash.
- `csp-extensions`: Under `enforce`, extension pages show that they can't run, since their bundle is inline script in a frame that inherits the policy.
- `csp-dictation`: Under either mode, dictation loads its audio worklet from a built file rather than a `blob:` URL.
- `invalid-config`: An unrecognized value stops the server at startup instead of leaving a control silently off.

## How to get to it (user POV)

- Operator: add the `data_controls:` block to the server config (`omnigent server -c config.yaml`, `OMNIGENT_CONFIG`, or `<data_dir>/config.yaml`), or set `OMNIGENT_EXPORT_INCLUDE_OUTPUTS`, `OMNIGENT_ALLOW_DOWNLOADS` or `OMNIGENT_CONTENT_SECURITY_POLICY`, then restart the server.
- Web export: open a session, open its menu (the header's "⋯" on desktop, the session menu on mobile) and choose Export.
- CLI export: `omnigent session export --id <session-id>`.
- API export: `GET /v1/sessions/{id}/export?format=jsonl[&outputs=false]`.
- Web downloads: hover a file in the Files tree or the Changes list, open a file's "⋯" View settings menu, or open a workspace recording in chat or the file viewer.
- Web UI under the policy: load any page of the web app; the policy rides on the HTML response.

## Driving it with the repro environment

Preconditions: build this checkout's web UI. Server and component tests need no running server; the browser-contract lane serves the build with a mocked backend.

- Export with outputs, and outputs left out by the request or the deployment: `tests/server/integration/test_sessions_export.py::test_exports_the_transcript_the_session_api_serves` and `tests/server/integration/test_sessions_export.py::test_leaves_tool_outputs_out`.
- `/v1/info` reporting: `tests/server/integration/test_sessions_export.py::test_info_reports_the_data_controls`.
- Paging: `tests/server/integration/test_sessions_export.py::test_reads_every_store_page_in_order`.
- Every item type has an export decision: `tests/server/test_data_controls.py::test_every_item_type_has_an_export_decision`.
- CLI export through the server, the older-server fallback, and a refusal that must not fall back: `tests/host/test_cli_session_export.py::test_session_export_uses_the_server_export`, `tests/host/test_cli_session_export.py::test_session_export_writes_jsonl` and `tests/host/test_cli_session_export.py::test_session_export_fails_on_a_server_export_refusal`.
- Web Export and its notice: `tests/e2e_ui/sessions/test_export_chat_menu.py` covers the menu item in a real browser; the server-export branch and the notice are component-tested in `web/src/shell/HeaderConversationMenu.test.tsx`.
- Downloads refused while previews answer: `tests/server/routes/test_session_resources.py::test_filesystem_download_refused_when_downloads_are_off`.
- Download controls hidden: component tests in `web/src/shell/FileDownloadButton.test.tsx`, `web/src/shell/FileViewer.test.tsx` and `web/src/components/VideoPlayer.test.tsx`.
- Config parsing, export redaction and the policy header (enforce, report-only, off, 304 revalidation, a changed mode invalidating cached HTML, base-path script hash): `tests/server/test_data_controls.py::test_reads_the_config_and_environment`, `tests/server/test_data_controls.py::test_spa_html_carries_the_policy`, `tests/server/test_data_controls.py::test_changing_the_mode_invalidates_cached_html` and `tests/server/test_data_controls.py::test_create_app_wires_the_configured_policy`.
- The SPA under the policy: `tests/browser_ui/test_content_security_policy.py::test_spa_core_pages_load_without_policy_violations` renders a chat reply with highlighted code, a Mermaid diagram and KaTeX math, a file in Monaco, the home page, and an HTML preview whose same-page link it follows, and fails on any reported violation in the page or its frames.
- Extension pages, dictation and the preview hash: component tests in `web/src/extensions/ExtensionViewHost.test.tsx`, `web/src/lib/dictationWorklet.test.ts` and `web/src/shell/htmlPreviewPolicy.test.ts` (which fails if the preview script changes without the server's hash).
- Human check: start a server with all three controls set, open a session with a tool call, and confirm: Export downloads a `.jsonl` whose tool output reads `[output omitted by this server's export settings]` and a notice appears; no Download control shows on file rows or in the file viewer menu; `curl -i '<server>/v1/sessions/<id>/resources/environments/default/filesystem/README.md?download=true'` answers 403; `curl -sI <server>/` shows `content-security-policy`; and the browser console shows no "Content Security Policy" errors while you open chats, files, PDFs and HTML previews.

## Gotchas

- `include_outputs` governs exports only. The transcript view and the items API still show outputs, since that is where the agent's work is read.
- `allow_downloads: false` covers the full-file stream the Download buttons and recording players use. Files a user uploaded into the conversation stay reachable at `GET .../resources/files/{id}/content`, which renders them inline and lets a rewind attach them again. Saving a code block, table or diagram from a reply copies text already on screen. Agent bundle downloads that runners need are unaffected.
- YAML reads a bare `off` as false; the server accepts that as `off`, and `true` as `enforce`.
- Under the policy, remote images and videos (for example, in a pull request description) don't load, HTML previews can't run their own inline or remote scripts, and a browser OpenTelemetry collector on another origin is unreachable. The policy sets no `frame-ancestors`, so the VS Code extension can still embed the app. `report-only` has no reporting endpoint: violations appear only in the browser console.
- The browser test covers neither PDFs, dictation (it needs a microphone), nor extension pages; use the human check for PDFs.
- The browser-contract lane adds the header to the static build itself, with the same policy builder the server uses; `tests/server/test_data_controls.py` proves the server sends it.
- New lazy-loaded renderers must work without `eval`: configure their libraries for it (as for zod and pdf.js) and extend the browser test with a page that loads them.
