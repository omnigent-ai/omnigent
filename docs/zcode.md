# ZCode harness

ZCode is Z.ai's coding CLI (`zcode`). Omnigent runs the CLI in non-interactive
print mode, not through the Agent Client Protocol or an
`ACP_CLI_HARNESSES` row.

```bash
omnigent run --harness zcode
```

`z-code` is an alias of `zcode`.

## Install and sign-in

Put `zcode` on `PATH`. Node 24 or newer is required. Browser OAuth is:

```bash
zcode login
```

If ZCode is built from source or is outside the host service's `PATH`, persist
its executable path in `~/.omnigent/config.yaml`. A shell-only environment
override does not update an already-running background host.

```yaml
harness:
  zcode:
    command: /absolute/path/to/zcode
```

ZCode stores OAuth credentials in `~/.zcode/v2/credentials.json` and model
selection in `~/.zcode/v2/provider_config.json`. `ZCODE_DATA_BASE_DIR`
overrides the home used for those paths.

A Coding Plan API key is entered in the ZCode TUI:

```text
/login zai-coding-plan-api-key <key>
```

This API-key path writes the same encrypted credential store and provider
configuration. Omnigent accepts either storage indicator conservatively by
checking only that `credentials.json` exists and is non-empty. It never reads
or decrypts the file, and ZCode has no supported auth-status command, so setup
reports **Credentials found**, not **Signed in**. Presence does not prove that
the credential is valid or unexpired.

Do not put a ZCode credential in `executor.auth`. The ZCode process receives
Omnigent's safe base environment, `ZCODE_*`, the Z.ai endpoint variables
`ZAI_OAUTH_ORIGIN`, `ZAI_BUSINESS_BASE_URL`, `ZAI_OAUTH_CLIENT_ID`, and
`BIGMODEL_API_BASE_URL`, plus names explicitly listed in
`os_env.sandbox.env_passthrough`. It does not inherit unrelated provider keys.

Omnigent does not use the official app-server because it requires the host to
handle `interaction/requestProviderRuntimeHeaders`, which means decrypting
provider credentials and returning runtime headers. Omnigent does not read or
decrypt vendor credentials.

## What a turn does

```bash
zcode --cwd <workspace> --mode yolo --output-format stream-json -p <prompt>
```

Each turn starts a process. Later turns add `--resume <session-id>` using the ID
returned by the previous successful turn. Omnigent keeps that ID only in the
executor process, so resume works while that process remains alive and is lost
after an executor or runner restart. The system prompt prefixes the first
successful turn in that process-local session.

Only `yolo` mode is supported because print mode cannot answer headless
permission requests. Model overrides are rejected because print mode has no
supported model-selection mechanism. Under `yolo`,
`executor.config.disallowed_tools` controls tool selection; it is not a hard
security boundary.

Tool calls run inside ZCode. Omnigent reports their events but does not
re-dispatch or re-execute them. To enforce filesystem or process access,
configure `os_env.sandbox`; Omnigent wraps the ZCode process tree in the active
OS sandbox. Without it, ZCode's own tools execute with the user's OS access.

An active sandbox does not automatically expose `~/.zcode`. Doing so would also
let tools in the same ZCode process read the credential store. Sandboxed users
must grant narrow read access to the credential and provider configuration files
and write access to ZCode's session database directory, normally
`~/.zcode/cli/db`. ZCode's tools share those grants.

Image and file data URIs in normal input blocks are materialized into the
session's attachment cache under `~/.omnigent/attachments/` (outside the working
directory, removed when the session closes) and passed with `--attach`. A
sandboxed turn gets read access to that cache directory only. Local attachment
paths must resolve inside the configured working directory. Remote HTTP(S) URLs
are rejected rather than downloaded.

## Limitations

- No `omnigent zcode` terminal wrapper.
- No approval or user-input elicitation in print mode.
- No model override, including `executor.model`, `--model`, or `/model`.
- No resume after an executor or runner restart.
- No dollar-cost reporting.
- No dynamic workflow tools. ZCode 3.14.3 and later turn them off in print mode
  unless `--enable-workflow` is passed, and Omnigent does not pass it.
