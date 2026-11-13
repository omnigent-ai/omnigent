# Pi-native adapter tests

Tests here mirror `omnigent/harnesses/pi_native/`. Runner, runtime and full
CLI/browser journeys stay in their owning suites. See [the placement guide](../../README.md).

Provider configuration, resume state, bridge files and the Node extension. The
Node and Python-to-Node interrupt scenarios stay in the default pytest lane.

Run this adapter family with retries disabled:

```sh
uv run --no-sync pytest tests/harnesses/pi_native --reruns 0 -n 4 --dist loadfile
```

These files moved from `tests/` with their filenames and test names preserved.
Search an old failure's filename or test name here; fixtures keep their original scope.
