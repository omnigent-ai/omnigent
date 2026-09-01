# Codex provider homes

A Codex-backed provider may select a separate Codex credential/configuration
root for native Codex launches:

```yaml
providers:
  codex-personal:
    kind: subscription
    cli: codex
    default: openai
  codex-work:
    kind: subscription
    cli: codex
    cli_home: ~/.codex-work
```

`cli_home` accepts `~` and environment-variable references. After expansion it
must be absolute; relative paths are rejected so the host daemon, runner, and
local CLI cannot resolve the same provider to different accounts. Referenced
environment variables are forwarded to runner processes automatically.

Native Codex sessions bridge `auth.json` and `config.toml` from the selected
home into their private per-session home. Readiness and model discovery inspect
that same local home without reading the OS keychain or running an interactive
login command. Model catalogs are cached separately by selected home and Codex
binary.

To sign in a second account:

```bash
CODEX_HOME="$HOME/.codex-work" codex login
```

This setting currently applies only to native Codex launch paths, including the
native terminal and its background-title worker. The in-process `codex` harness
does not yet support per-provider homes, so the provider inventory does not
advertise general multiple-profile support.
