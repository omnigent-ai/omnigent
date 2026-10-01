# relocate_modules.py

Deterministic module relocation codemod for the `omnigent/` package reorganisation.

## Usage

```
# Move modules and rewrite all in-repo references
python dev/refactor/relocate_modules.py apply --map dev/refactor/module_moves.toml [--dry-run] [--report out.json]

# Rewrite references only (no git mv), e.g. on an in-flight branch
python dev/refactor/relocate_modules.py rewrite --map dev/refactor/module_moves.toml [paths...]

# Check for remaining references to old names; exit 1 if any genuine hits
python dev/refactor/relocate_modules.py check --map dev/refactor/module_moves.toml
```

All three commands accept `--repo DIR` (default: current directory).

## Idempotency

`apply` is idempotent:
- Re-running after a **successful** first run is a no-op: every move is detected
  as "already done" and skipped; the text-rewrite pass uses post-move semantics
  so already-new-style references are left unchanged.
- Re-running after a **partial failure** completes the remaining work.

Detection of "already moved":
1. Old path missing and new path present — simple case.
2. Old path is a file listed as an `[[alias]]` stub in the map.
3. Old path is a directory and `new.startswith(old + ".")` — this is the
   file-to-package rename case (e.g. `cli.py` → `cli/commands.py`); after the
   move, `omnigent/cli/` is the *new* package, not the old module.
4. Old path is a directory whose every non-`__init__.py` file is a known alias
   stub.

`rewrite` and `check` always use post-move semantics (they are designed for
in-flight branches after `apply` has already run on main).

## Residual ambiguity (file → package rename)

When a module file is renamed into a package of the same name — e.g.
`omnigent/cli.py` → `omnigent/cli/commands.py` — the name `omnigent.cli` now
refers to the **new package**, not the old module file.  The text rewriter
cannot distinguish:

- `from omnigent.cli import ui` — new-style (importing from the new package)
- `omnigent.cli.main` — old-style (attribute of the old module, now in
  `commands.py`)

The post-move guard (`rewrite`/`check`) resolves this by applying the
*longest-new-prefix* rule: if the longest prefix of a candidate token that
matches a new module name is **strictly longer** than the longest old prefix,
the token is left alone (it is definitively new-style).  If both prefixes are
equal in length, the candidate is left alone only when the entire token
*equals* a new name.  References whose disambiguation requires knowing the
full chain (e.g. `omnigent.cli.main` — is `main` an attribute of the new
package or the old module?) are rewritten as old-style; review the diff.

## Map format

See the comments at the top of `module_moves.toml` and the module docstring in
`relocate_modules.py` for the full TOML schema.

## Design notes

- **Single-pass text rewrite**: one finder regex detects candidates by first
  segment; the callback does an O(depth) prefix lookup so a 900-module map
  stays fast on large trees.
- **AST-based import split**: `from P import a, b` where only `a` is moved
  produces two separate statements, preserving local binding aliases and
  trailing line comments (`# noqa`, `# type:`).
- **Relative-import pre-pass**: moved files get all relative imports converted
  to absolute before `git mv` so the moved file is self-consistent immediately.
- **Compat stubs**: `[[alias]]` entries generate a `sys.modules` re-export at
  the old path so call sites on older branches keep working during the
  transition.
- **Ruff post-format**: changed `.py` files are formatted automatically (skip
  with `RELOCATE_SKIP_FORMAT=1`).

## Tests

```
pytest -q tests/dev/test_relocate_modules.py
```
