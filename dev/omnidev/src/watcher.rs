//! Watches the backend source tree and asks the supervisor to reload on
//! Python changes. Frontend files are deliberately not watched — Vite HMR
//! handles those.

use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result};
use notify::RecursiveMode;
use notify_debouncer_full::new_debouncer;
use tokio::sync::mpsc;

use crate::supervisor::Cmd;

/// Start watching `omnigent_dir` for `*.py` changes. Coalesced bursts become a
/// single `Cmd::Reload(n)` on `cmd_tx`. The returned debouncer must be kept
/// alive for the watch to persist.
pub fn spawn(
    omnigent_dir: &Path,
    cmd_tx: mpsc::UnboundedSender<Cmd>,
) -> Result<impl Send + 'static> {
    // The debouncer coalesces rapid saves; we still filter to *.py and skip
    // caches so editor churn and __pycache__ writes don't trigger reloads.
    let mut debouncer = new_debouncer(
        Duration::from_millis(500),
        None,
        move |result: notify_debouncer_full::DebounceEventResult| {
            let Ok(events) = result else { return };
            let mut changed = 0usize;
            for event in &events {
                for path in &event.paths {
                    if is_relevant(path) {
                        changed += 1;
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

fn is_relevant(path: &Path) -> bool {
    if path.extension().and_then(|e| e.to_str()) != Some("py") {
        return false;
    }
    !path.components().any(|c| c.as_os_str() == "__pycache__")
}
