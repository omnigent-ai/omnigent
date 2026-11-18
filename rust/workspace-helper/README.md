# omnigent-workspace-helper

A small Rust binary that serves one machine's workspace files to the Omnigent
host that starts it: stat, directory listings, streamed file reads, fuzzy and
substring file search, streamed text search, and read-only Git comparisons.

It implements `WorkspaceService` from
[`omnigent/api/workspace/v1alpha1/workspace.proto`](../../omnigent/api/workspace/v1alpha1/workspace.proto),
the same API the host serves in Python, plus the host-only control service in
[`workspace_helper.proto`](../../omnigent/api/workspace/v1alpha1/workspace_helper.proto)
(`GetInfo`, `OpenWorkspace`, `CloseWorkspace`).

## Behavior

- **Same answers as the host.** `ListDirectory` and `ReadFile` follow the
  host's `omnigent.workspace_fs.WorkspaceReader` exactly: its path rules and
  errors, paging, entry types, the 10 MiB read limit, regular files only (a
  FIFO or device answers NOT_FOUND without blocking), and metadata that
  describes the delivered bytes, with a UTF-8 character split by the limit
  dropped. The host fills in `content_type`.
  `tests/host/test_workspace_helper_parity.py` compares the two call by call.
- **Same search results as the runner.** Substring search returns what the
  runner's file search returns, and text search reproduces the runner's `rg`
  script output exactly (`tests/parity.rs` runs that script against the same
  tree).
- **Git ignore rules.** Entries carry an `ignored` flag computed with Git's
  semantics (nested `.gitignore`, `.git/info/exclude`, global excludes,
  negation, ignored ancestors, nested repositories). Text search also honours
  `.ignore` and `.rgignore`, as the runner's `rg` does. `tests/parity.rs`
  checks the rules against `git check-ignore` and `git ls-files`.
- **Fast search, nothing on disk.** File search runs over an in-memory list
  of paths built by a parallel walk and refreshed from Git; fuzzy and
  substring queries over a 900k-path checkout take 5 to 20 ms. Text search
  scans files directly, as `rg` does; nothing about contents is stored.
- **Directory scopes.** File and text search take include and exclude
  directories. Text search starts its walk at the includes and never enters
  an exclude, which keeps it fast in a large monorepo.
- **Confinement.** Request paths are relative to an opened workspace root;
  absolute paths, `..`, and symlinks that resolve outside the root are
  refused. Each workspace call names its workspace in the
  `omnigent-workspace-id` header, with a random id from `OpenWorkspace`.
- **Read-only.** The helper never writes to the workspace.

## Compatibility

`GetInfo` reports `protocol_version` and a list of `capabilities`: the
`WorkspaceService` methods it serves and optional features such as
`search_files.fuzzy`. Add features as new methods, fields, and capabilities;
bump the protocol version only for incompatible changes.

## Running

```bash
omnigent-workspace-helper --connection-fd N [--exit-on-stdin-eof]
omnigent-workspace-helper --socket PATH [--exit-on-stdin-eof]
omnigent-workspace-helper --version
```

With `--connection-fd`, the helper serves the one connection on descriptor
`N`, such as an end of a socketpair the host kept the other end of, and exits
when it closes. The descriptor must be a socket owned by the same user; the
helper keeps it from the processes it starts. With `--socket`, for
development and tests, it serves an owner-only Unix socket, replaces a stale
one, exits with status 3 if another helper serves it, and removes it on exit.

On startup it prints one JSON line with `"ready": true` and its build.
Shutdown waits at most two seconds for open requests. With
`--exit-on-stdin-eof` it also exits when its parent closes stdin.

## Building and testing

```bash
just test-workspace-helper   # fmt, clippy, tests, speed test
cargo build --release --manifest-path rust/Cargo.toml
OMNIGENT_WORKSPACE_HELPER=rust/target/debug/omnigent-workspace-helper \
  uv run pytest tests/host/test_workspace_helper_parity.py
cargo run --release --manifest-path rust/Cargo.toml --example benchmark -- ~/some/checkout
```
