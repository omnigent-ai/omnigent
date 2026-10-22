# Portable plugin choices

After adopting a workspace, new Claude-native and Codex-native sessions apply
its plugin toggles. Machine overrides win over shared defaults; native project,
explicit session and managed settings retain their precedence. Existing sessions
keep their frozen preference snapshot.

The identity is `plugin/ECOSYSTEM/MARKETPLACE/NAME`. The marketplace is the
registry name already known to that native CLI, not a path or installation URL.
Claude and Codex identities remain separate even for the same plugin name.

```sh
omnigent preferences --server URL set --scope shared plugin/claude/team/tools false
omnigent preferences --server URL set --scope machine plugin/codex/team/tools true
```

Claude receives an invocation settings file; Codex receives a private user config.
Neither path edits the user's native global config. Enabling a missing plugin
keeps the requested value and emits a warning, but does not install it. A
project/session/policy choice that differs from the saved Claude choice is also
reported. Codex applies its own project/policy tiers above the private user tier.
Hermetic Claude skill filters and Codex minimal-config workers keep their
existing isolation.

To include explicit native user toggles in a reviewed import:

```sh
omnigent preferences --server URL import SOURCE.yaml --plugins claude --plugins codex > plan.json
# Inspect every change before applying it.
omnigent preferences --server URL apply plan.json
```

Only explicit enable/disable values are imported. Managed policy, project
settings, credentials, install paths and plugin files are not copied. The
existing import/restore workflow covers those plugin edits too.

Real Codex process coverage checks concurrent opposite choices with an installed
local fixture. Claude coverage checks generated invocation settings and source
precedence; it does not establish actual vendor plugin execution. SDK consumers,
browser controls, managed deployment and broader model availability remain
separate integration work. Global rollout stays disabled.
