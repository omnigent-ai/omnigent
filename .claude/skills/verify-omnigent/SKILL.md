---
name: verify-omnigent
description: Drive Omnigent the way a user does and prove a behavior with recorded evidence, using an isolated server, runner, and mock model plus a feature map of every user entry point. Load before reproducing a user-facing bug, before claiming a fix works, or when reviewing whether a change covered every surface (session vs. new-session composer, terminal strip vs. agent terminal, all twelve native harnesses). Covers the web UI, native harness terminals, and the CLI.
---

# Verify Omnigent

Use this skill to see a behavior happen in the real app and to prove a change,
not to reason about it from code. It has two parts:

- **An isolated instance.** [`scripts/verify-env`](scripts/verify-env) wraps
  `python -m dev.repro_env`: a server, runner, and mock model server on private
  ports, with their own config, data, Claude, and Codex directories. It never
  touches `~/.omnigent`, a running host daemon, or another developer server.
- **A feature map.** [`features/`](features/README.md) lists each user-facing
  feature's entry points, the tests that drive them, and the traps. A fix is
  verified only when every entry point listed for its feature has proof.

Run all commands from the repository root. Put `scripts/` on your path or call
`.claude/skills/verify-omnigent/scripts/verify-env` directly.

## Launch

1. Install dependencies and build the web UI once per checkout:

   ```sh
   uv sync --frozen --group test
   pnpm install --frozen-lockfile --filter web && pnpm --filter web run build
   ```

2. Start an instance and load its paths:

   ```sh
   verify-env start          # waits until the runner is online, about 10 seconds
   eval "$(verify-env paths)"
   ```

   `start` writes `VERIFY_ROOT` to `.omnigent/verify/current`, so later shells
   find the same instance. The instance stops itself after its lease (default
   90 minutes; `--lease SECONDS`, 60 to 21600).

Ready means the server answers, the runner reports online, and the mock model
server answers. `start` fails with the environment's error and log path
otherwise.

In CI, the repro workflow already runs this environment. Use
`python -m dev.repro_env exec -- ...` there instead of starting another.

## Doctor

Run `verify-env doctor` before the first drive, after any failed drive, and
whenever something looks off. It checks that the instance is ready, that its
supervisor is alive, that the runner and model server answer, and it warns when
the checkout has moved since launch. A warning about a moved checkout means the
instance runs old code: stop it and start a new one.

## Drive

1. Open the matching file in [`features/`](features/README.md) and list every
   entry point for the behavior in question.
2. For each entry point, run the named test through the instance, recording on:

   ```sh
   verify-env run -- python -m pytest <tests/...py::test_name> \
     --ui-skip-build --video=on --screenshot=on \
     --output="$VERIFY_EVIDENCE/<feature>"
   ```

   Tests under `tests/browser_ui/` need no instance:
   `uv run pytest <test> --browser-ui-skip-build --video=on --output=...`.
   Tests the feature file marks "own environment" also run with plain
   `uv run pytest`.
3. For an entry point with no test, hand-drive it with Playwright against
   `OMNIGENT_REPRO_SERVER_URL` inside `verify-env run -- python <script>`, and
   script model replies with the mock helpers named in the feature map README.
4. To reproduce a bug, run the same journey on the unfixed code first and keep
   that evidence; then run it again on the fix.

## Evidence

Everything under `$VERIFY_EVIDENCE` is the proof, one directory per feature.
Instance logs, the database, and the mock model's recorded requests stay in
`$VERIFY_ENV`. For each claim, record the feature file, the entry point ID, the
command, and the resulting artifact. The proof standards are in the
[feature map README](features/README.md#proof-and-coverage). Mock runs prove
Omnigent's integration with Claude and Codex, not a live vendor model.

## Cleanup

`verify-env stop` stops only this instance's supervisor, which stops its own
server, runner, and model processes and saves the model's request log. It never
deletes `$VERIFY_ROOT`, so the evidence survives; remove old roots under
`.omnigent/verify/` or `/tmp/verify-omnigent.*` yourself once the evidence is
attached. Never kill Omnigent processes by name: a developer's own server and
host daemon may be running on the same machine.

## Helpers

`scripts/verify-env` is the only helper. Its subcommands are `start [--lease
SECONDS]`, `doctor`, `run -- COMMAND...`, `paths`, and `stop`; run it with no
arguments for usage.

On macOS the helper places the instance under `/tmp/verify-omnigent.*` when the
checkout path is long. macOS limits Unix socket paths to 104 bytes, and the
environment's socket relay only works around long paths on Linux.

## Keeping the map honest

`tests/dev/test_verify_omnigent_feature_map.py` fails when a referenced test is
renamed or removed, a feature file breaks the entry contract, or a native
harness has no matrix row. When a reviewer says a change missed a surface, add
that surface to the feature file in the same change.
