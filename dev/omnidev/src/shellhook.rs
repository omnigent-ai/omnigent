//! Emit the shell snippet that runs the daily update check.

/// The snippet to append to `.zshrc`/`.bashrc`
/// (`omnidev shell-hook >> ~/.zshrc`). All throttling and prompting live inside
/// `omnidev check`, so this stays trivial and shell-agnostic: run once per
/// interactive shell, quietly, and never fail the shell if it errors.
///
/// It self-guards on `command -v omnidev`, so it's meant to be appended to the
/// rc (a static no-op when omnidev is absent) rather than run via
/// `eval "$(omnidev shell-hook)"`, which would invoke omnidev on every shell
/// startup and error when it isn't on PATH.
const HOOK: &str = r#"# omnidev: daily omnigent update check
if [ -n "${PS1:-}" ] && command -v omnidev >/dev/null 2>&1; then
  omnidev check --quiet || true
fi"#;

pub fn print() {
    println!("{HOOK}");
}
