# Codex-native adapter tests

Tests here mirror `omnigent/harnesses/codex_native/`. Runner launch wiring stays
under `tests/runner/`; runtime scaffolding, onboarding and end-to-end journeys
remain in their owning suites. See [the placement guide](../../README.md).

Run the adapter family with retries disabled:

```sh
uv run --no-sync pytest tests/harnesses/codex_native --reruns 0 -n 4 --dist loadfile
```

Existing filenames and test names are retained from `tests/test_codex_native*.py`.
To find an old failure, search its test name here. This directory adds no shared
fixtures; module-local fixtures retain their existing scope.
