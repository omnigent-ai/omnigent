//! Single-instance guard per pod.
//!
//! Two omnidev runs in the same checkout resolve to the same pod dir (the dir
//! is keyed to the canonical repo root), so their processes would fight over
//! the same ports and state. An advisory `flock` on a file in the pod dir lets
//! only the first in. The lock is held for the process lifetime and released
//! by the OS on exit or crash — no stale-file cleanup needed.

use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::path::Path;

use anyhow::{bail, Context, Result};

/// An acquired pod lock. Dropping it (on process exit) releases the flock.
pub struct PodLock {
    _file: File,
}

/// Try to take the pod's exclusive lock. Returns an error naming the pod dir if
/// another omnidev already holds it.
pub fn acquire(pod_dir: &Path) -> Result<PodLock> {
    let path = pod_dir.join("omnidev.lock");
    let file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&path)
        .with_context(|| format!("opening lock file {}", path.display()))?;

    // Non-blocking exclusive lock: EWOULDBLOCK means a peer holds it.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::EWOULDBLOCK) {
            bail!(
                "another omnidev is already running for this checkout (pod {}). \
                 Quit it first, or run in a different worktree.",
                pod_dir.display()
            );
        }
        return Err(err).with_context(|| format!("locking {}", path.display()));
    }

    Ok(PodLock { _file: file })
}
