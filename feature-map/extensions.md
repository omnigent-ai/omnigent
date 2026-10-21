# Team extensions

Operator-installed extensions contribute isolated browser pages, portable agents,
policy presets, and trusted server observers through one namespaced manifest.

## Sub-features

- `author`: scaffold a separate package and validate its bundled resources.
- `activate`: explicitly grant backend versions and permissions in server config.
- `dependencies`: validate versions, detect cycles, and order lifecycle hooks.
- `agents`: export or install a portable bundle with skills, tools, and selected policies.
- `observe`: receive bounded session-status metadata and shut down with the server.
- `disable`: remove extension catalog entries and installs without removing owned copies.
- `diagnostics`: inspect rejected manifests, activation errors, and unavailable assets.

## How to get to it (user POV)

- Authors use `omnigent extensions init`, `check`, and `export` in the package's
  Python environment.
- Operators use `omnigent extensions enable` or `disable` with an explicit
  `--config` path, then restart the server using that configuration.
- Authenticated clients inspect `/v1/extensions` and the content-addressed agent
  bundle URLs it returns. POST the same URL with `/bundle` replaced by `/install`
  to add a private reusable-agent copy.
- Existing browser-page extensions keep their namespaced routes and sidebar slot.
  Agent copies use the existing new-session picker and session APIs.
- Administrators inspect `/v1/extensions/diagnostics`; authors inspect local
  manifests with `omnigent extensions list`, `doctor`, and `check`.

## Driving it with the repro environment

Preconditions: use an isolated checkout/environment and the
[reference package](../examples/extensions/incident-response/README.md). Install
the editable package, run `check`, and enable it into a disposable server YAML.
Start `omnigent server --config <that-file>` with isolated data and artifacts.

- Confirm the catalog contains the team owner, agent version/digest, and policy
  preset. Download the bundle and inspect `omnigent-extension-lock.json`.
- POST its install URL, then refresh the caller's owned agent list. A different
  user must neither see nor retrieve that copy. Unknown or stale bundle digests
  must not install a different version.
- Disable the extension, restart with the same data, and refresh clients. The
  catalog, downloads, and new installs must disappear; the previously installed
  copy remains owned by its user, and `/health` stays healthy.
- The example logs startup/shutdown and status metadata. Missing permission
  grants, bad dependency pins, and callback failures must appear only in admin
  diagnostics and server logs; other extensions continue operating.
- `tests/e2e/test_team_extension_pack_e2e.py` builds a wheel from a separate
  checkout, exposes only its installed files to real server/CLI processes,
  verifies install/export bytes and restart/disable behavior, and needs no LLM.
- Component coverage: `tests/extensions/test_manager.py`,
  `tests/extensions/test_packs.py`, `tests/extensions/test_cli.py`, and
  `tests/server/routes/test_extensions_routes.py`.

## Gotchas

- Python distribution installation is full code trust. Manifest factories still
  execute during discovery; server entrypoints require an explicit operator grant.
- An absent extension config preserves old browser-only enablement. A mapping is
  a complete allowlist, and `extensions: {}` disables everything.
- Version upgrades install a separate named agent copy. Explicitly reinstalling
  the same version follows normal owned-agent replacement behavior.
- Status hooks are best effort on the serving replica and cannot gate approvals.
  They are not durable event delivery or a cluster-wide event feed.
- Portable bundles need no extension installation on a runner. Extra provider
  packages and actual MCP endpoints still require their normal setup.
- This map adds CLI/server coverage. Browser extension host behavior continues
  through the existing isolated browser SDK; no new browser flow is introduced.
