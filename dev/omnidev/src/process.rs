//! Concrete command specs for the three supervised processes.

use std::path::PathBuf;

use crate::pod::Pod;

/// A resolved command line + working dir for one process. Env is applied by the
/// supervisor from `Pod::env()`, so it is not duplicated here.
pub struct ProcSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
}

impl ProcSpec {
    /// `uv run omnigent server --host 127.0.0.1 --port <p> --database-uri <db>
    /// --artifact-location <dir>`, from the repo root.
    pub fn server(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "omnigent".into(),
                "server".into(),
                "--host".into(),
                "127.0.0.1".into(),
                "--port".into(),
                pod.ports.server.to_string(),
                "--database-uri".into(),
                pod.db_uri(),
                "--artifact-location".into(),
                pod.artifacts_dir().display().to_string(),
            ],
            cwd: pod.repo_root.clone(),
        }
    }

    /// `uv run omnigent host --server http://127.0.0.1:<p>`, from the repo root.
    pub fn host(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "omnigent".into(),
                "host".into(),
                "--server".into(),
                pod.server_url(),
            ],
            cwd: pod.repo_root.clone(),
        }
    }

    /// `npm install`, from `web/`. Run before Vite when deps are missing or
    /// stale so Vite's dependency scan doesn't fail on an unresolved import.
    ///
    /// `--loglevel http` makes npm emit a line per package fetch even when its
    /// stdout is piped (its progress bar is TTY-only), so the pane streams real
    /// progress. `--no-fund --no-audit` trims the trailing noise.
    pub fn npm_install(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "npm".into(),
            args: vec![
                "install".into(),
                "--no-fund".into(),
                "--no-audit".into(),
                "--loglevel".into(),
                "http".into(),
            ],
            cwd: pod.web_dir(),
        }
    }

    /// `npm run dev -- --port <p> --strictPort`, from `web/`. `OMNIGENT_URL`
    /// (in the pod env) points Vite's proxy at this pod's backend.
    pub fn vite(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "npm".into(),
            args: vec![
                "run".into(),
                "dev".into(),
                "--".into(),
                "--port".into(),
                pod.ports.vite.to_string(),
                "--strictPort".into(),
            ],
            cwd: pod.web_dir(),
        }
    }
}
