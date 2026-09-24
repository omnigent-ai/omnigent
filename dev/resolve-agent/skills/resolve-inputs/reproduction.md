### Recovering the handoff

For `session` and `ci_link`, you need four things before you can do anything: the
**verdict + per-facet breakdown**, the **journey**, the **`bug_url`**, and the
**e2e test's actual file content**. Recover them like this:

**From a `session`:**

1. `sys_session_get_history` on the session id. repro-agent's contract is that
   the **last ```json fenced block in its final message** is the machine-readable
   handoff. Find that block and parse `verdict`, `facets`, `test_path`,
   `journey`, `bug_url`, `evidence`.
2. The session transcript **truncates large tool-call arguments** (to ~2000
   chars), so it does **not** contain the test file's full content — only its
   path. To get the real file, call `sys_session_get_info` on the session id and
   read its **`workspace`** field: that is the `repro/<slug>` worktree the repro
   ran in, where repro-agent left the authored test **uncommitted** at
   `test_path`. Read the full file from `<workspace>/<test_path>` off disk and
   copy it into your own worktree at `test_path` — with **shell** commands: that
   workspace sits outside your own worktree, where your file tools cannot reach.
   (Do **not** rely on the transcript for the test body — it is truncated; the
   file on disk is the source of truth. The session's own `workspace` is the
   authoritative link back to the right reproduction — never guess by picking
   some "newest" repro worktree, which may belong to an unrelated bug.)
3. If `sys_session_get_info` returns no `workspace`, or that path/`test_path`
   doesn't exist (e.g. the repro worktree was removed), stop with
   `needs_more_info` naming what you couldn't recover — do not reconstruct the
   test from the truncated transcript.

**From a `ci_link`:**

The repro worktree is gone, so recover from the run's artifacts and logs with the
`gh` CLI. Be **tolerant** — the exact artifact layout may vary, so try in order
and fall back rather than assuming a fixed structure:

1. Download this run's `repro-bundle-<run-id>` artifact first, staging it
   **inside your worktree** at `.omnigent/repro-bundle/` (e.g. `gh run download
   <run-id> --name repro-bundle-<run-id> --dir .omnigent/repro-bundle`). Your
   file tools are worktree-scoped: a bundle staged under `/tmp` or
   `$RUNNER_TEMP` sits outside the environment root, so every file-tool read of
   it errors and only shell fallbacks work. `.omnigent/` is gitignored and
   excluded by the commit rules, so nothing staged there can leak into your
   diff. If the bundle contains top-level `repro-handoff.json`, parse and
   validate that checkpoint before reading the full job log: it is the
   smallest, most direct structured source for
   `verdict`/`facets`/`test_path`/`journey`/`bug_url`/`session_id`. Copy each
   test named by `test_path` from the artifact's `files/` tree into your checkout.
   Also retain `patch.diff`, recordings, and `run.log` as supporting evidence.

   The checkpoint is intentionally raw: a producing workflow may have classified
   a forward-compatible schema as unknown while your newer consumer understands
   it. Validate it yourself against the authoritative input `bug_url`; do not
   reject it merely because the old run reported an unknown handoff.

2. Only when the artifact checkpoint is absent or invalid, inspect `run.log` from
   the downloaded bundle, then fall back to `gh run view <ci_link> --log`. The
   final repro message is echoed in the job log **untruncated**, so its last
   ```json block can recover the handoff. The log may also carry the complete
   verbatim source of the e2e test as a path-labelled code block; use that only
   when the artifact's `files/` tree did not preserve the test.

   **This run's handoff is authoritative.** The `ci_link` you were given names
   exactly one repro run, and its `bug_url` is the bug you resolve — no other.
   You are running on a **shared server that hosts many other repro sessions**;
   do **not** call `sys_session_list` and pick "a" repro session, and do not
   resolve a different bug because its session looks handy on this server. If you
   cannot find a handoff in this run's artifact or logs, stop with
   `needs_more_info` — never fall back to a bug you found by browsing the server.
3. If the run also recorded a shareable `session_id` you can reach, read it with
   `sys_session_get_history` for richer context — but **only** the exact
   `session_id` this run's handoff named. Before trusting it, confirm that
   session's own handoff carries the **same `bug_url`** as the run log. If the
   ids don't match, or that session is about a different bug, ignore it and rely
   on the run log alone — do not resolve whatever bug that session turned out to
   describe.
4. If neither the artifacts nor the logs yield the test's content, **stop with
   `needs_more_info`** naming exactly what the run was missing. Do not reconstruct
   the test from a guess.
