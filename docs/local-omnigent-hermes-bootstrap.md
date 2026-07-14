# Local Omnigent/Hermes Bootstrap

This is the reproducible bootstrap for Spencer's Mac. It is intentionally based
on the installed working setup, not this checkout's import path.

## Ground Truth

Verified on 2026-07-07 from `/tmp`:

- Omnigent CLI: `/Users/spencer/.local/bin/omnigent`
- `omni` alias: `/Users/spencer/.local/bin/omni`
- Omnigent version: `0.4.0` built `2026-07-03T01:26:51Z`
- Hermes CLI: `/Users/spencer/.local/bin/hermes`
- Hermes version: `Hermes Agent v0.18.0 (2026.7.1)`
- Local Omnigent server: `http://127.0.0.1:6767`
- Local model endpoint: `http://127.0.0.1:8080/v1`
- Local model alias: `qwen3-coder-next-local`
- Hermes model config: `provider=custom`, `context_length=65536`

Secret-bearing state stays outside the repo:

- Hermes config: `/Users/spencer/.hermes/config.yaml`
- Hermes secrets: `/Users/spencer/.hermes/.env`
- Hermes Docker private env, if used: `/Users/spencer/.config/hermes-docker/.env`
- Omnigent local state: `/Users/spencer/.omnigent/`

Do not copy `.env`, tokens, private keys, or OAuth material into this checkout.

## Launch

Use the ambient launcher:

```bash
scripts/omnigent-hermes-local.sh
```

The launcher:

1. Resolves installed `omni` and `hermes`, preferring `/Users/spencer/.local/bin`.
2. Leaves Hermes auth/provider config in `~/.hermes`.
3. Starts or reuses the local Omnigent server.
4. Runs `omni hermes --server ""` so Omnigent creates the `hermes-native-ui`
   wrapper and attaches to the Hermes TUI.

Optional overrides:

```bash
OMNIGENT_CLI=/path/to/omni \
OMNIGENT_HERMES_PATH=/path/to/hermes \
scripts/omnigent-hermes-local.sh
```

## Smoke

Run the neutral-cwd proof:

```bash
scripts/omnigent-hermes-smoke.py
```

The smoke script:

1. Resolves the installed `omni` entrypoint and re-execs into its uv-tool Python.
2. Changes to `/tmp` before importing Omnigent.
3. Confirms `qwen3-coder-next-local` is exposed by `http://127.0.0.1:8080/v1/models`.
4. Runs one direct `hermes chat` marker check.
5. Creates a disposable `hermes-native-ui` Omnigent session through the local
   daemon path.
6. Sends one web/session event and waits for the assistant marker response.
7. Deletes the disposable session unless `--keep-session` is passed.

Expected final line:

```text
PASS: installed Omnigent/Hermes native bootstrap is usable from a neutral cwd
```

Useful variants:

```bash
scripts/omnigent-hermes-smoke.py --skip-direct
scripts/omnigent-hermes-smoke.py --timeout 360 --keep-session
```
