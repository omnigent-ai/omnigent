//! Concrete command specs for the three supervised processes.

use std::path::PathBuf;

use crate::pod::Pod;

/// A resolved command line + working dir for one process. Env is applied by the
/// supervisor from `Pod::env()`, with per-process additions from `extra_env`.
pub struct ProcSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub extra_env: Vec<(String, String)>,
}

impl ProcSpec {
    fn omnigent_log_env() -> Vec<(String, String)> {
        // Child stderr is a pipe that omnidev reads into its process panes.
        // Let Omnigent's process logger mirror to that pipe despite it not
        // being a terminal, and force ANSI colors because omnidev parses them.
        vec![
            ("OMNIGENT_LOG_TTY_FD".into(), "2".into()),
            ("OMNIGENT_LOG_FORCE_COLOR".into(), "1".into()),
        ]
    }

    /// `uv run omnigent --log-to-stderr server --host 127.0.0.1 --port <p>
    /// --database-uri <db> --artifact-location <dir>`, from the repo root.
    pub fn server(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "omnigent".into(),
                "--log-to-stderr".into(),
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
            extra_env: Self::omnigent_log_env(),
        }
    }

    /// `uv run omnigent --log-to-stderr host --server http://127.0.0.1:<p>`,
    /// from the repo root.
    pub fn host(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "omnigent".into(),
                "--log-to-stderr".into(),
                "host".into(),
                "--server".into(),
                pod.server_url(),
            ],
            cwd: pod.repo_root.clone(),
            extra_env: Self::omnigent_log_env(),
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
            extra_env: Vec::new(),
        }
    }

    /// `npm run dev -- --host <host> --port <p> --strictPort`, from `web/`.
    /// `OMNIGENT_URL` (in the pod env) points Vite's proxy at this pod's backend.
    pub fn vite(pod: &Pod) -> ProcSpec {
        ProcSpec {
            program: "npm".into(),
            args: vec![
                "run".into(),
                "dev".into(),
                "--".into(),
                "--host".into(),
                pod.vite_host.clone(),
                "--port".into(),
                pod.ports.vite.to_string(),
                "--strictPort".into(),
            ],
            cwd: pod.web_dir(),
            extra_env: Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ports::Ports;

    #[test]
    fn vite_uses_configured_bind_host_but_backend_url_stays_loopback() {
        let repo = tempdir();
        let pod_dir = tempdir();
        let pod = Pod::create(
            repo,
            pod_dir,
            Ports {
                server: 19191,
                vite: 19292,
            },
            "0.0.0.0".into(),
            Vec::new(),
        )
        .unwrap();

        let vite = ProcSpec::vite(&pod);
        let host_flag = vite.args.iter().position(|arg| arg == "--host").unwrap();
        assert_eq!(vite.args[host_flag + 1], "0.0.0.0");
        assert_eq!(pod.server_url(), "http://127.0.0.1:19191");
    }

    #[test]
    fn omnigent_processes_mirror_logs_to_omnidev_pipe() {
        let repo = tempdir();
        let pod_dir = tempdir();
        let pod = Pod::create(
            repo,
            pod_dir,
            Ports {
                server: 19191,
                vite: 19292,
            },
            "127.0.0.1".into(),
            Vec::new(),
        )
        .unwrap();

        for spec in [ProcSpec::server(&pod), ProcSpec::host(&pod)] {
            assert!(
                spec.args.iter().any(|arg| arg == "--log-to-stderr"),
                "omnigent command should request stderr logging: {:?}",
                spec.args
            );
            assert_eq!(
                spec.extra_env
                    .iter()
                    .find(|(key, _)| key == "OMNIGENT_LOG_TTY_FD")
                    .map(|(_, value)| value.as_str()),
                Some("2")
            );
            assert_eq!(
                spec.extra_env
                    .iter()
                    .find(|(key, _)| key == "OMNIGENT_LOG_FORCE_COLOR")
                    .map(|(_, value)| value.as_str()),
                Some("1")
            );
        }
    }

    fn tempdir() -> std::path::PathBuf {
        let unique = format!(
            "omnidev-process-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }
}
