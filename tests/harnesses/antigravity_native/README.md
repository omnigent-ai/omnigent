# Antigravity-native adapter tests

Tests here mirror `omnigent/harnesses/antigravity_native/`. Runner, runtime and full
CLI/browser journeys stay in their owning suites. See [the placement guide](../../README.md).

Recorded steps, RPC discovery, reader delivery and local bridge hooks. Recorded
step assets remain in `tests/fixtures/antigravity/`.

Run this adapter family with retries disabled:

```sh
uv run --no-sync pytest tests/harnesses/antigravity_native --reruns 0 -n 4 --dist loadfile
```

These files moved from `tests/` with their filenames and test names preserved.
Search an old failure's filename or test name here; fixtures keep their original scope.
