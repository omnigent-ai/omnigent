# Omnigent CLI output contract

This is the contract every `omnigent` command follows when it writes to a
terminal, so the whole CLI reads as one coherent, branded product. The
runtime lives in two small modules:

- **`omnigent/inner/ui.py`** — the styling layer: shared consoles, the
  brand palette/theme, and the status/structure helpers. This is the
  module command code should import.
- **`omnigent/inner/wordmark.py`** — the brand art: the Otto + "omnigent"
  wordmark lockup and the compact one-line brandmark. Imported by `ui`.

The interactive REPL header keeps its own builder
(`omnigent/inner/banner.py`) — that's the live-session box, not part of
this non-interactive contract.

## The one rule: stdout is data, stderr is decoration

Everything else follows from this:

- **stdout** carries machine-readable output — IDs, paths, config dumps,
  the `version` string, anything a script might parse. Use `ui.console`
  (or `click.echo`) for it.
- **stderr** carries decoration and diagnostics — warnings, errors, the
  brand banner, spinners, progress. Use `ui.err_console` / `ui.warn` /
  `ui.error` / the banner helpers.

So `omnigent version | cat`, `omnigent config list | jq`, and piped
one-shot output stay byte-clean, while the human at a terminal still gets
color and branding on stderr.

**Never** hand-roll raw ANSI escapes or call `click.secho(fg=...)` in new
code. **Never** print the banner or status decoration to stdout.

## Palette

One brand accent; semantic colors stay conventional. Defined as a
`rich.theme.Theme` in `ui.py`:

| Token          | Color              | Use                                  |
| -------------- | ------------------ | ------------------------------------ |
| `omni.accent`  | `#F43BA6` magenta  | Brand — wordmark, headers, `==>`, spinner |
| `omni.success` | green              | Success / done                       |
| `omni.warning` | yellow             | Warnings (stderr)                    |
| `omni.error`   | red                | Errors (stderr)                      |
| `omni.info`    | cyan               | Informational                        |
| `omni.muted`   | dim                | Metadata, secondary text             |

`#F43BA6` is Otto's magenta — the single source is
`omnigent.inner.mascots.MASCOT_ART_COLOR`, re-exported as `ui.ACCENT`.
The `scripts/install_oss.sh` installer mirrors the same accent
(`\033[38;2;244;59;166m`) so the installer and the tool agree.

## Helper API (`omnigent.inner.ui`)

Status lines — consistent glyph + color, correct stream:

```python
ui.step("Installing Omnigent")     # ==>  accent   (stdout)
ui.success("Verified omnigent")    #  ✓   green    (stdout)
ui.info("Using ~/.omnigent")       #  ·   dim      (stdout)
ui.warn("tmux not found")          #  !   yellow   (stderr)
ui.error("uv is required")         #  ✗   red      (stderr)
```

Messages are emitted verbatim (never reparsed as rich markup), so a
message containing `[...]` is safe.

Structure:

```python
ui.header("Configured credentials")     # bold accent section header
ui.kv("Session", "New session")         # aligned label / value row
ui.rule("Setup")                        # horizontal accent rule
tbl = ui.table(title="Hosts"); ...; ui.console.print(tbl)   # branded Table
ui.console.print(ui.panel(body, title="Note"))              # branded Panel
```

Raw streams when you need them: `ui.console` (stdout), `ui.err_console`
(stderr).

## When to show the banner

Banner output is drawn on stderr and TTY-gated by `ui.show_banner()` (a
no-op off a TTY or when `OMNIGENT_NO_BANNER` is set):

- **Full lockup** — `ui.print_landing(...)` — Otto + wordmark, optional
  gradient / tagline / epilogue. The hero moment, reserved for the few
  landing surfaces:
  - `omnigent --help` (the top-level group, via `_OmnigentCLI.format_help`)
  - `omnigent setup` (first-run experience)
  - the installer banner
- **Nothing** — every other command. Regular commands (`version`,
  `upgrade`, `server status`, `config list`, …) print their output
  unbranded so the CLI stays quiet and scriptable. We deliberately do
  *not* sprinkle a brandmark on individual commands.

A compact one-line brandmark helper (`ui.print_brandmark(subtitle=...)`,
`✦ omnigent`) exists for opt-in use, but is intentionally **not** wired
onto any command today — add it only if a specific surface clearly wants
light branding.

The bare `omnigent` invocation on a TTY launches the REPL (its own
branded header); it only falls back to `--help` when non-interactive, so
the landing banner naturally appears there.

## Gating & environment

| Condition                    | Effect                                     |
| ---------------------------- | ------------------------------------------ |
| stdout/stderr not a TTY      | No banner; rich emits no color (data clean)|
| `NO_COLOR` set               | rich renders monochrome (art still shows)  |
| `OMNIGENT_NO_BANNER` truthy  | No banner/brandmark even on a TTY          |
| `OMNIGENT_NO_SPINNER` truthy | No startup spinner (pre-existing)          |

## Adding a new command — checklist

1. `from omnigent.inner import ui` (import lazily inside the command body
   if the module is import-cost sensitive).
2. Print **data** to stdout via `ui.console.print` / `click.echo`.
3. Print **status** via `ui.step/success/info`; **problems** via
   `ui.warn/error` (these go to stderr automatically).
4. Build tables/panels with `ui.table()` / `ui.panel()`.
5. Add a banner only if the command is a landing/first-run surface
   (`print_landing`) or a read-only branded command (`print_brandmark`).
   Leave it off scripted/data commands.
6. No raw ANSI, no `click.secho(fg=...)`, no decoration on stdout.
