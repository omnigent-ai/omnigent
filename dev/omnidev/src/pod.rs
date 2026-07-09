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
    /// Only omnigent's own state is isolated (DB, artifacts, logs); the pod
    /// otherwise inherits your real home, credentials, config, and caches.
    pub fn create(repo_root: PathBuf, dir: PathBuf, ports: Ports) -> Result<Pod> {
        for sub in ["data/omnigent", "artifacts", "logs"] {
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

    /// Whether `web/` needs `npm install` before Vite can start: either
    /// `node_modules/` is absent, or the lockfile / `package.json` is newer
    /// than the installed tree (a dependency was added/changed since the last
    /// install — the case that makes Vite's dependency scan fail).
    pub fn needs_npm_install(&self) -> bool {
        let web = self.web_dir();
        let modules = web.join("node_modules");
        if !modules.is_dir() {
            return true;
        }
        let mtime = |p: PathBuf| std::fs::metadata(p).and_then(|m| m.modified()).ok();
        let Some(installed) = mtime(modules) else {
            return true;
        };
        // Reinstall if either manifest is newer than node_modules.
        [web.join("package-lock.json"), web.join("package.json")]
            .into_iter()
            .filter_map(mtime)
            .any(|t| t > installed)
    }

    /// Directory to watch for backend source changes.
    pub fn omnigent_dir(&self) -> PathBuf {
        self.repo_root.join("omnigent")
    }

    pub fn log_file(&self, name: &str) -> PathBuf {
        self.dir.join("logs").join(format!("{name}.log"))
    }

    /// The env overrides applied on top of the inherited parent env for every
    /// child. We isolate only omnigent's own state — the DB and data dir — so
    /// concurrent pods don't share a database or pidfile. Everything else
    /// (real `HOME`, credentials, config, uv/npm caches) is inherited, since
    /// the agents omnigent runs need it. `OMNIGENT_URL` is the seam
    /// `web/vite.config.ts` reads to point its proxy at this pod's backend.
    pub fn env(&self) -> Vec<(String, String)> {
        let d = |p: &str| self.dir.join(p).display().to_string();
        vec![
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
