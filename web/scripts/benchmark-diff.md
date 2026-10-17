# File diff computation benchmark

From the repository root, after installing frontend dependencies:

```sh
node web/scripts/benchmark-diff.mjs > /tmp/diff-current.jsonl
node web/scripts/benchmark-diff.mjs --budget-ms 5000 > /tmp/diff-default.jsonl
node web/scripts/benchmark-diff.mjs --algorithm legacy > /tmp/diff-legacy.jsonl
```

The default is the viewer's initial 1,000ms computation budget, the advanced algorithm,
and three samples per fixture. `--samples 1` runs a quicker comparison. Run
comparisons sequentially on the same machine without other CPU-heavy work.

Fixtures cover dense edits at several sizes, sparse edits, indentation, moved
blocks, repeated lines, a long single line, and new/deleted files. Each JSONL
record reports computation time, timeout status, line hunks, and character-level
mappings. Every sample verifies that applying the line hunks reconstructs the
modified file. That check catches omitted changes; it does not establish that
the alignment is the most readable possible diff.

The viewer retains Monaco's advanced algorithm because legacy skips character
refinement for changed blocks of 20 or more lines. Advanced returns coarser
hunks when its budget expires. After an initial timeout, the viewer automatically retries with 10,000ms in a
separate worker while retaining the first result. The status overlay disappears
on completion; a second timeout leaves a notice that highlighting may include
unchanged text. Model disposal or content changes terminate that worker.
There is also a 12-second worker-lifetime watchdog. Sparse edits typically finish well below either budget, including
in large files. The algorithm budget is cooperative, not a hard wall-clock deadline.

This benchmark imports the installed Monaco implementation directly. It measures
computation in Node, excluding API requests, worker queueing, editor loading,
syntax highlighting, and rendering. It is not a managed-production latency
measurement. Revisit the comparison when upgrading Monaco.

For the real browser worker and rendered split/unified/mobile views:

```sh
corepack pnpm --dir web run build
.venv/bin/python -m pytest tests/browser_ui/files/test_file_line_navigation.py \
  -k large_diff --browser-ui-skip-build -s
```

The browser test checks initial and refinement budgets, complete changed-line
coverage, character mappings, scroll stability, navigation cancellation, and
final-timeout feedback. Dense cases shorten the initial algorithm deadline to
1ms to force the boundary even on fast machines, and hold the detailed response
while checking that the initial diff remains usable. The final-timeout case also
shortens the refinement deadline, and the navigation case stalls the refinement
worker so cancellation is deterministic. Timing is diagnostic rather than a machine-dependent CI limit.

Refinement integration uses Monaco's internal worker bootstrap and runtime
`getDiffComputationResult()` method. Re-run the browser tests and both builds on
Monaco upgrades. The embedded bundle emits two worker assets; the managed host
must serve both workers.
