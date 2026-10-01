# relocate_modules.py

Deterministic module relocation codemod for the `omnigent/` package reorganisation.

## Usage

```
# Move modules and rewrite all in-repo references
python dev/refactor/relocate_modules.py apply --map dev/refactor/module_moves.toml [--dry-run] [--report out.json]

# Rewrite references only (no git mv), e.g. on an in-flight branch
python dev/refactor/relocate_modules.py rewrite --map dev/refactor/module_moves.toml [paths...]

# Check for remaining old-name references; exit 1 if any found
python dev/refactor/relocate_modules.py check --map dev/refactor/module_moves.toml
```

All three commands accept `--repo DIR` (default: current directory).

## Map format

See the comments at the top of `module_moves.toml` and the module docstring in
`relocate_modules.py` for the full TOML schema.

## Design notes

- **Idempotent**: re-running `apply` after a successful run is a no-op; re-running
  after a partial failure resumes from where it stopped.
- **Single-pass text rewrite**: all old→new substitutions use one combined regex
  alternation (longest match first) so no rule can re-match another rule's output.
- **AST-based import split**: `from P import a, b` where only `a` is moved produces
  two separate statements, preserving local binding aliases.
- **Relative-import pre-pass**: moves files get all relative imports converted to
  absolute before the git mv, so the moved file is self-consistent immediately.
- **Compat stubs**: `[[alias]]` entries generate a `sys.modules` re-export at the
  old path so call sites on older branches keep working during the transition.
- **Ruff post-format**: changed `.py` files are formatted with `ruff` automatically
  (skip with `RELOCATE_SKIP_FORMAT=1`).

## Tests

```
pytest -q tests/dev/test_relocate_modules.py
```
