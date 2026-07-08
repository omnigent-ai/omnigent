//! A `Pod` = one isolated dev instance: its own state dir, ports, and the env
//! map injected into every supervised child.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::ports::Ports;

pub struct Pod {
    pub repo_root: PathBuf,
    pub dir: PathBuf,
    pub ports: Ports,
}

impl Pod {
    /// Create the pod directory tree (idempotent) and return the pod handle.
    /// Mirrors the isolation layout proven by `scripts/backend-smoke.sh`.
    pub fn create(repo_root: PathBuf, dir: PathBuf, ports: Ports) -> Result<Pod> {
        for sub in [
            "home",
            "tmp",
            "config/xdg",
            "data/xdg",
            "cache/xdg",
            "config/omnigent",
            "data/omnigent",
            "artifacts",
            "logs",
        ] {
            let p = dir.join(sub);
            std::fs::create_dir_all(&p)
                .with_context(|| format!("creating pod dir {}", p.display()))?;
        }
        Ok(Pod {
            repo_root,
            dir,
            ports,
        })
    }

    pub fn db_uri(&self) -> String {
        format!(
            "sqlite:///{}",
            self.dir.join("data/omnigent/chat.db").display()
        )
    }

    pub fn artifacts_dir(&self) -> PathBuf {
        self.dir.join("artifacts")
    }

    pub fn server_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.ports.server)
    }

    /// Clickable URLs for display. Terminals linkify `localhost` but often not
    /// a bare `127.0.0.1`. Functional uses (server bind, host `--server`,
    /// `OMNIGENT_URL`) stay on `127.0.0.1` so we don't accidentally target IPv6
    /// `localhost` (`::1`), where the server isn't listening.
    pub fn server_display_url(&self) -> String {
        format!("http://localhost:{}", self.ports.server)
    }

    pub fn vite_display_url(&self) -> String {
        format!("http://localhost:{}", self.ports.vite)
    }

    pub fn web_dir(&self) -> PathBuf {
        self.repo_root.join("web")
    }

    /// Directory to watch for backend source changes.
    pub fn omnigent_dir(&self) -> PathBuf {
        self.repo_root.join("omnigent")
    }

    pub fn log_file(&self, name: &str) -> PathBuf {
        self.dir.join("logs").join(format!("{name}.log"))
    }

    /// The env overrides applied on top of the inherited parent env for every
    /// child. Keeps PATH/uv resolvable while redirecting all Omnigent state
    /// into the pod dir. `OMNIGENT_URL` is the seam `web/vite.config.ts` reads
    /// to point its proxy at this pod's backend.
    pub fn env(&self) -> Vec<(String, String)> {
        let d = |p: &str| self.dir.join(p).display().to_string();
        vec![
            ("HOME".into(), d("home")),
            ("TMPDIR".into(), d("tmp")),
            ("XDG_CONFIG_HOME".into(), d("config/xdg")),
            ("XDG_DATA_HOME".into(), d("data/xdg")),
            ("XDG_CACHE_HOME".into(), d("cache/xdg")),
            ("OMNIGENT_CONFIG_HOME".into(), d("config/omnigent")),
            ("OMNIGENT_DATA_DIR".into(), d("data/omnigent")),
            ("OMNIGENT_DATABASE_URI".into(), self.db_uri()),
            ("OMNIGENT_URL".into(), self.server_url()),
        ]
    }
}

/// Remove a pod directory (for `--clean`). No-op if it does not exist.
pub fn clean(dir: &Path) -> Result<()> {
    if dir.exists() {
        std::fs::remove_dir_all(dir)
            .with_context(|| format!("removing pod dir {}", dir.display()))?;
    }
    Ok(())
}
