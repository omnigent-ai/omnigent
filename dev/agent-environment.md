# Agent environment — general notes for repro-agent and resolve-agent

Shared reference for both agents. These apply to **everything you run in your
session's shell**, regardless of the task — not just recording.

## Package installs go through the Databricks registry proxies

You run inside a **Databricks-network session**, where **direct access to the
public npm and PyPI registries is blocked** (a supply-chain security control). So
any command that fetches from `registry.npmjs.org` or `pypi.org` — `npm`/`pnpm`/
`npx`/`yarn install`, `pip install`, `uv pip install`, `uv sync`, `pytest`/
`playwright`/`vhs`/`electron` installs — fails with an **auth/connection error**
that looks like your command is wrong but is really the registry being
unreachable. Point the tools at the internal proxies **once, up front, before any
install**:

```bash
# npm / pnpm / npx / yarn — writes ~/.npmrc, so all of them pick it up
npm config set registry https://npm-proxy.cloud.databricks.com/
# pip
pip3 config set global.index-url https://pypi-proxy.cloud.databricks.com/simple
# uv — pass the index explicitly (don't bake the proxy into uv.lock)
uv pip install --default-index https://pypi-proxy.cloud.databricks.com/simple/ <pkg>
uv sync --index-url https://pypi-proxy.cloud.databricks.com/simple/
```

Notes:

- **Never commit the proxy URL into a lockfile.** `uv sync` records the index it
  resolved against into `uv.lock`; passing `--index-url` on the CLI keeps it out
  of the committed lockfile (the proxy isn't reachable from GitHub-hosted CI, and
  it must not leak into a public repo). If an install step writes a lockfile,
  revert that lockfile change unless it's genuinely part of the fix.
- The proxies **embargo package versions newer than 7 days**; if an install fails
  only for a just-published version, that's the age filter, not a misconfig —
  note it rather than trying to bypass the block.
- If an install still fails **after** pointing at the proxy, treat that tooling as
  unavailable for the task at hand and say so plainly — **do not** attempt to
  reach the public registry directly (the block is deliberate).
