# Correctness strategy

Use this guide to choose enough proof without creating tests that cost more than
the behavior they protect.

## State the property

Write the claim as an observable invariant before choosing a test:

```text
Given:
When:
Then:
Must not:
```

Include ordering, retries, persistence, and error behavior when they matter.
"The page looks right" is not a correctness claim.

## Use the lowest sufficient test level

Choose the first level that can falsify the claim:

1. **Pure function or render conditional.** Use a unit or component test. A
   label selected from an existing prop does not need a new Playwright file.
2. **Browser interaction or local persistence.** Use component tests plus the
   closest existing Playwright journey. Extend that journey before adding a new
   file.
3. **Network, process, lifecycle, or concurrency boundary.** Add a focused real
   journey. Examples include request ordering, remount recovery, Electron main
   process behavior, host registration, streaming, and authentication.
4. **Native behavior that automation cannot reproduce.** Keep automated
   coverage for the browser or process event path, then name the narrow manual
   check. Software keyboards, OS accelerators, signing, and real SSO can fall
   into this category.

A new E2E file must protect a failure that a lower-level test or an existing E2E
cannot catch. Record that reason in the test docstring. If there is no such
reason, do not add the file.

## Prove failure modes, not only success

For stateful work, assert the negative outcomes directly:

- Count writes or requests to prove exactly-once behavior.
- Record order to prove one message cannot overtake another.
- Assert that cancellation stops retries.
- Assert that a dry run creates no file, row, grant, request, or ref.
- Test stale state, reload, remount, navigation, and final retry exhaustion.
- For URL or authentication handling, test trusted and untrusted origins.

Do not infer these properties from a final screenshot.

## Reproduce bugs against the base

For a regression fix, run the focused proof against the intended base when
practical. The test should fail for the reported reason before the fix and pass
after it. If the base cannot run, record the exact blocker and use captured
production evidence or a minimal deterministic reproduction.

## Handle unrelated failures with evidence

Do not dismiss a failure because it looks unrelated. Run the same command in an
unchanged base worktree. Classify it as pre-existing only when the same test
fails with the same signature there. `auto` compares only failed steps at the
unique merge base, with matched dependency inputs and a bounded timeout. Keep
the failing logs from both runs. A matching base failure is
`baseline_reproduced`, not a pass: the required lane remains failed until the
repository-wide blocker is resolved or explicitly handled outside verification.
The rerun request is private, signed, one-use evidence. Authenticate the
finalized child `steps.json`, exact requested indices, regenerated profile
commands, and execution-log hashes before using any comparison classification.
Exact-base reproduction is currently supported for server, harness-client, and
CLI lanes. Quality-gates, web-ui, and desktop remain `could_not_compare` until
isolated JavaScript dependencies can be supplied without installs or shared-tree
writes.

## Check compatibility at changed boundaries

When a client, server, database, or downstream package changes, verify the
combinations that can exist during rollout:

- current client with current server;
- old client with new server;
- new client with old server;
- downstream Universe when explicitly requested and its pin includes the code.

Missing fields must default safely. New server fields should be additive. A
downstream source patch that applies is not runtime proof.

## Finish with repository gates

The `quality-gates` lane runs potentially fixing pre-commit hooks only in a
disposable worktree, then runs non-mutating web format, lint, type-check, and
production-build binaries. Every profile and doctor snapshots repository bytes,
lockfiles, and dependency metadata before and after; unreadable snapshots or
new mutations fail closed while unchanged pre-existing dirty state is retained.
These checks do not replace behavioral tests, but a change is not verified when
the same checks CI will run are still failing.

Exact-base reproduction is limited to server, harness-client, and CLI.
When the selected base commit equals `HEAD`, exact-base reproduction remains
available only if the candidate contains uncommitted product/test changes; the
baseline worktree still comes from the clean `HEAD` commit.
JavaScript quality, browser UI, and desktop failures remain
`could_not_compare`; a green parent cannot reinterpret that exclusion as
baseline evidence. Text members of Playwright trace ZIPs receive deterministic
credential-pattern redaction while preserving ZIP validity. Before evidence is
finalized, every explicitly staged or allowlisted credential value is searched
byte-for-byte through files and recursively through ZIP members; a matching
artifact and any secret-bearing path components are removed and the run fails.
Archive nesting beyond the bounded scanner fails closed. An unconfirmed removal
is recorded as a privacy cleanup failure without reproducing the suspect path.
This is not a claim that traces are
generally sanitized: unnamed page secrets may evade heuristics, and image/video
pixels are not OCR-scanned. Screenshots and videos receive only the known-value
byte scan, which cannot detect visibly rendered credentials. Review captured
evidence before sharing it outside the verification boundary.
Browser network evidence covers only requests and responses observed by the
instrumented Playwright context. It is not a packet capture and does not prove
that unrelated processes or server-side dependencies made no network calls.

UI profiles only pass when they produce JUnit results, browser context metadata,
a screenshot, and a trace inside the run's evidence directory. Files in
`/tmp`, an agent's private folder, or an unrelated browser session do not count
as verification evidence.
