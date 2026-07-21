//! Watches the backend source tree and asks the supervisor to reload on
//! Python changes. Frontend files are deliberately not watched — Vite HMR
//! handles those.

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use notify::RecursiveMode;
use notify_debouncer_full::new_debouncer;
use tokio::sync::mpsc;

use crate::state::Shared;
use crate::supervisor::Cmd;

/// Start watching `omnigent_dir` for `*.py` changes. Coalesced bursts become a
/// single `Cmd::Reload(n)` on `cmd_tx`. The returned debouncer must be kept
/// alive for the watch to persist.
///
/// Gitignored files (e.g. the build-time `omnigent/_build_info.py`) are skipped
/// so churn from generated files doesn't trigger reloads. With `debug` on, every
/// observed change is logged with whether it triggered a reload or why it was
/// skipped.
pub fn spawn(
    repo_root: &Path,
    omnigent_dir: &Path,
    shared: Arc<Mutex<Shared>>,
    debug: bool,
    cmd_tx: mpsc::UnboundedSender<Cmd>,
) -> Result<impl Send + 'static> {
    let ignore = build_ignore(repo_root);
    let repo_root = repo_root.to_path_buf();

    // The debouncer coalesces rapid saves; we still filter to *.py, skip caches
    // and gitignored files so editor churn and generated writes don't reload.
    let mut debouncer = new_debouncer(
        Duration::from_millis(500),
        None,
        move |result: notify_debouncer_full::DebounceEventResult| {
            let Ok(events) = result else { return };
            let mut changed = 0usize;
            for event in &events {
                for path in &event.paths {
                    match classify(path, &ignore) {
                        Ok(()) => {
                            changed += 1;
                            if debug {
                                log_watch(&shared, &repo_root, path, "reload trigger");
                            }
                        }
                        Err(reason) => {
                            if debug {
                                log_watch(&shared, &repo_root, path, &format!("skip ({reason})"));
                            }
                        }
                    }
                }
            }
            if changed > 0 {
                let _ = cmd_tx.send(Cmd::Reload(changed));
            }
        },
    )
    .context("creating file watcher")?;

    debouncer
        .watch(omnigent_dir, RecursiveMode::Recursive)
        .with_context(|| format!("watching {}", omnigent_dir.display()))?;

    Ok(debouncer)
}

/// Build a gitignore matcher from the repo's root `.gitignore` and
/// `.git/info/exclude`. Both are best-effort — a missing or malformed file just
/// contributes no rules. Nested `.gitignore` files under `omnigent/` are not
/// consulted (the repo has none today); add them here if that changes.
fn build_ignore(repo_root: &Path) -> Gitignore {
    let mut b = GitignoreBuilder::new(repo_root);
    b.add(repo_root.join(".gitignore"));
    b.add(repo_root.join(".git").join("info").join("exclude"));
    b.build().unwrap_or_else(|_| Gitignore::empty())
}

/// Decide whether a changed path should trigger a reload, or why not. The `Err`
/// carries a short reason for the `--debug` log.
fn classify(path: &Path, ignore: &Gitignore) -> Result<(), &'static str> {
    if path.extension().and_then(|e| e.to_str()) != Some("py") {
        return Err("non-.py");
    }
    if path.components().any(|c| c.as_os_str() == "__pycache__") {
        return Err("__pycache__");
    }
    // `_or_any_parents` so files inside a gitignored directory (build/, dist/,
    // *.egg-info/, …) are skipped too, matching git's own behavior — plain
    // `matched` only catches paths named by a rule directly.
    if ignore.matched_path_or_any_parents(path, false).is_ignore() {
        return Err("gitignored");
    }
    Ok(())
}

/// Emit a `--debug` watch line into the combined pane, path shown relative to
/// the repo root when possible.
fn log_watch(shared: &Arc<Mutex<Shared>>, repo_root: &Path, path: &Path, what: &str) {
    let rel = path.strip_prefix(repo_root).unwrap_or(path);
    shared
        .lock()
        .unwrap()
        .event(format!("watch: {what} {}", rel.display()));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ignore_with(line: &str) -> Gitignore {
        let mut b = GitignoreBuilder::new("/repo");
        b.add_line(None, line).unwrap();
        b.build().unwrap()
    }

    #[test]
    fn plain_python_file_triggers_reload() {
        let ig = ignore_with("omnigent/_build_info.py");
        assert_eq!(classify(Path::new("/repo/omnigent/cli.py"), &ig), Ok(()));
    }

    #[test]
    fn gitignored_python_file_is_skipped() {
        let ig = ignore_with("omnigent/_build_info.py");
        assert_eq!(
            classify(Path::new("/repo/omnigent/_build_info.py"), &ig),
            Err("gitignored")
        );
    }

    #[test]
    fn file_inside_gitignored_dir_is_skipped() {
        // A directory rule must ignore everything beneath it, like git does.
        let ig = ignore_with("build/");
        assert_eq!(
            classify(Path::new("/repo/omnigent/build/foo.py"), &ig),
            Err("gitignored")
        );
    }

    #[test]
    fn non_python_file_is_skipped() {
        let ig = ignore_with("omnigent/_build_info.py");
        assert_eq!(
            classify(Path::new("/repo/omnigent/notes.txt"), &ig),
            Err("non-.py")
        );
    }

    #[test]
    fn pycache_file_is_skipped() {
        let ig = ignore_with("omnigent/_build_info.py");
        assert_eq!(
            classify(Path::new("/repo/omnigent/__pycache__/cli.py"), &ig),
            Err("__pycache__")
        );
    }
}
