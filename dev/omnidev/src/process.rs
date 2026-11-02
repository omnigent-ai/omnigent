//! Concrete command specs for the three supervised processes.

use std::ffi::OsStr;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use anyhow::{bail, Result};

use crate::install::PYTHON_VERSION;
use crate::pod::Pod;

/// A resolved command line + working dir for one process. Env is applied by the
/// supervisor from `Pod::env()`, with per-process additions from `extra_env`.
pub struct ProcSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub extra_env: Vec<(String, String)>,
}

/// Web commands resolved before the supervisor starts any children.
pub struct WebCommands {
    pub vite: ProcSpec,
    pub prepare: Option<ProcSpec>,
}

impl WebCommands {
    pub fn resolve(pod: &Pod) -> Result<Self> {
        Self::resolve_on_path(pod, std::env::var_os("PATH").as_deref())
    }

    fn resolve_on_path(pod: &Pod, path: Option<&OsStr>) -> Result<Self> {
        let mut commands = Self {
            vite: ProcSpec::vite(pod),
            prepare: if pod.profile.as_ref().is_some_and(|p| p.prepare.is_none()) {
                None
            } else {
                Some(ProcSpec::web_prepare(pod))
            },
        };
        if pod.profile.is_some() {
            return Ok(commands);
        }

        let available = |program: &str| {
            path.is_some_and(|path| {
                std::env::split_paths(path).any(|dir| {
                    std::fs::metadata(pod.web_dir().join(dir).join(program))
                        .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
                })
            })
        };
        if available("pnpm") {
            return Ok(commands);
        }
        if !available("corepack") {
            bail!(
                "Neither `pnpm` nor `corepack` is on PATH. The dev UI requires Node 22+ \
                 and pnpm. Install pnpm (`npm install -g pnpm`), or install Corepack \
                 (`npm install -g corepack`), then retry. Use --no-vite for backend-only development."
            );
        }
        // Corepack can run the repository's pinned pnpm without installing global shims.
        for spec in std::iter::once(&mut commands.vite).chain(commands.prepare.iter_mut()) {
            spec.program = "corepack".into();
            spec.args.insert(0, "pnpm".into());
        }
        Ok(commands)
    }
}

impl ProcSpec {
    fn from_profile(pod: &Pod, profile: &crate::profile::ProcessProfile) -> ProcSpec {
        let expand = |value: &str| {
            value
                .replace("{server_port}", &pod.ports.server.to_string())
                .replace("{vite_port}", &pod.ports.vite.to_string())
                .replace("{vite_host}", &pod.vite_host)
                .replace("{pod_dir}", &pod.dir.display().to_string())
                .replace("{repo_root}", &pod.repo_root.display().to_string())
        };
        ProcSpec {
            program: expand(&profile.command[0]),
            args: profile.command[1..].iter().map(|arg| expand(arg)).collect(),
            cwd: pod.repo_root.join(&profile.cwd),
            extra_env: Vec::new(),
        }
    }

    fn omnigent_log_env() -> Vec<(String, String)> {
        // Child stderr is a pipe that omnidev reads into its process panes.
        // Let Omnigent's process logger mirror to that pipe despite it not
        // being a terminal, and force ANSI colors because omnidev parses them.
        vec![
            ("OMNIGENT_LOG_TTY_FD".into(), "2".into()),
            ("OMNIGENT_LOG_FORCE_COLOR".into(), "1".into()),
        ]
    }

    /// `uv run --python <pinned> omnigent --log-to-stderr server --host 127.0.0.1 --port <p>
    /// --database-uri <db> --artifact-location <dir>`, from the repo root.
    pub fn server(pod: &Pod) -> ProcSpec {
        if let Some(profile) = &pod.profile {
            return Self::from_profile(pod, &profile.server);
        }
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "--python".into(),
                PYTHON_VERSION.into(),
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

    /// `uv run --python <pinned> omnigent --log-to-stderr host --server http://127.0.0.1:<p>`,
    /// from the repo root.
    pub fn host(pod: &Pod) -> ProcSpec {
        if let Some(profile) = &pod.profile {
            return Self::from_profile(
                pod,
                profile
                    .host
                    .as_ref()
                    .expect("host is disabled for this profile"),
            );
        }
        ProcSpec {
            program: "uv".into(),
            args: vec![
                "run".into(),
                "--python".into(),
                PYTHON_VERSION.into(),
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

    /// `pnpm install`, from `web/`. Run before Vite when deps are missing or
    /// stale so Vite's dependency scan doesn't fail on an unresolved import.
    pub fn web_prepare(pod: &Pod) -> ProcSpec {
        if let Some(profile) = &pod.profile {
            return Self::from_profile(
                pod,
                profile
                    .prepare
                    .as_ref()
                    .expect("web prepare is disabled for this profile"),
            );
        }
        ProcSpec {
            program: "pnpm".into(),
            args: vec!["install".into()],
            cwd: pod.web_dir(),
            extra_env: Vec::new(),
        }
    }

    /// `pnpm run dev --host <host> --port <p> --strictPort`, from `web/`.
    /// `OMNIGENT_URL` (in the pod env) points Vite's proxy at this pod's backend.
    pub fn vite(pod: &Pod) -> ProcSpec {
        if let Some(profile) = &pod.profile {
            return Self::from_profile(pod, &profile.vite);
        }
        ProcSpec {
            program: "pnpm".into(),
            // pnpm forwards script arguments directly; `--` would make Vite ignore the flags.
            args: vec![
                "run".into(),
                "dev".into(),
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
    use crate::profile::{ProcessProfile, Profile};

    fn web_pod(repo: PathBuf) -> Pod {
        std::fs::create_dir_all(repo.join("web")).unwrap();
        Pod {
            dir: repo.join("pod"),
            repo_root: repo,
            ports: Ports {
                server: 19191,
                vite: 19292,
            },
            vite_host: "127.0.0.1".into(),
            trusted_origins: Vec::new(),
            profile: None,
        }
    }

    fn fake_executable(dir: &std::path::Path, name: &str) {
        let path = dir.join(name);
        std::fs::write(&path, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n").unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    #[test]
    fn corepack_fallback_runs_install_and_vite_with_original_arguments() {
        let repo = tempdir();
        let bin = repo.join("bin");
        std::fs::create_dir(&bin).unwrap();
        fake_executable(&bin, "corepack");
        let pod = web_pod(repo.clone());

        let commands = WebCommands::resolve_on_path(&pod, Some(bin.as_os_str())).unwrap();
        for (spec, expected) in [
            (commands.prepare.unwrap(), "pnpm\ninstall\n"),
            (
                commands.vite,
                "pnpm\nrun\ndev\n--host\n127.0.0.1\n--port\n19292\n--strictPort\n",
            ),
        ] {
            assert_eq!(spec.program, "corepack");
            let output = std::process::Command::new(bin.join(&spec.program))
                .args(&spec.args)
                .current_dir(&spec.cwd)
                .output()
                .unwrap();
            assert!(output.status.success());
            assert_eq!(String::from_utf8(output.stdout).unwrap(), expected);
            assert_eq!(spec.cwd, pod.web_dir());
        }
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn standalone_pnpm_is_preferred_over_corepack() {
        let repo = tempdir();
        fake_executable(&repo, "pnpm");
        fake_executable(&repo, "corepack");
        let pod = web_pod(repo.clone());
        let commands = WebCommands::resolve_on_path(&pod, Some(repo.as_os_str())).unwrap();

        assert_eq!(commands.vite.program, "pnpm");
        assert_eq!(commands.vite.args[0], "run");
        let prepare = commands.prepare.unwrap();
        assert_eq!(prepare.program, "pnpm");
        assert_eq!(prepare.args, ["install"]);
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn missing_web_tools_report_setup_instructions() {
        let repo = tempdir();
        let pod = web_pod(repo.clone());
        for path in [None, Some(repo.as_os_str())] {
            let error = WebCommands::resolve_on_path(&pod, path).err().unwrap();
            let message = error.to_string();
            assert!(message.contains("Neither `pnpm` nor `corepack` is on PATH"));
            assert!(message.contains("npm install -g pnpm"));
            assert!(message.contains("--no-vite"));
        }
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn nonexecutable_pnpm_does_not_hide_corepack() {
        let repo = tempdir();
        let pod = web_pod(repo.clone());
        std::fs::write(repo.join("pnpm"), "not executable").unwrap();
        fake_executable(&repo, "corepack");

        let commands = WebCommands::resolve_on_path(&pod, Some(repo.as_os_str())).unwrap();
        assert_eq!(commands.vite.program, "corepack");
        std::fs::remove_dir_all(repo).unwrap();
    }

    #[test]
    fn vite_forwards_configured_host_and_port_but_backend_url_stays_loopback() {
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
        assert_eq!(
            vite.args,
            [
                "run",
                "dev",
                "--host",
                "0.0.0.0",
                "--port",
                "19292",
                "--strictPort",
            ]
        );
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
            assert_eq!(
                spec.args
                    .iter()
                    .take(4)
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
                vec!["run", "--python", PYTHON_VERSION, "omnigent"]
            );
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

    #[test]
    fn external_profile_expands_runtime_values() {
        let repo = tempdir();
        let pod_dir = tempdir();
        let mut pod = Pod::create(
            repo.clone(),
            pod_dir.clone(),
            Ports {
                server: 19191,
                vite: 19292,
            },
            "0.0.0.0".into(),
            Vec::new(),
        )
        .unwrap();
        let process = ProcessProfile {
            command: vec![
                "server".into(),
                "--port={server_port}".into(),
                "--ui={vite_port}".into(),
                "--host={vite_host}".into(),
                "--state={pod_dir}".into(),
            ],
            cwd: "service".into(),
        };
        pod.profile = Some(Profile {
            server: process.clone(),
            vite: process,
            prepare: None,
            host: None,
            backend_dir: "service".into(),
            web_dir: "ui".into(),
            dependency_manifests: vec!["package.json".into()],
        });

        let spec = ProcSpec::server(&pod);

        assert_eq!(spec.program, "server");
        assert_eq!(
            spec.args,
            [
                "--port=19191",
                "--ui=19292",
                "--host=0.0.0.0",
                &format!("--state={}", pod_dir.display()),
            ]
        );
        assert_eq!(spec.cwd, repo.join("service"));
        assert!(!pod.host_enabled());

        let web = WebCommands::resolve_on_path(&pod, None).unwrap();
        assert_eq!(web.vite.program, "server");
        assert_eq!(web.vite.args, spec.args);
        assert!(web.prepare.is_none());

        pod.profile.as_mut().unwrap().prepare = Some(ProcessProfile {
            command: vec!["custom-install".into(), "--offline".into()],
            cwd: "ui".into(),
        });
        let web = WebCommands::resolve_on_path(&pod, None).unwrap();
        let prepare = web.prepare.unwrap();
        assert_eq!(prepare.program, "custom-install");
        assert_eq!(prepare.args, ["--offline"]);
        assert_eq!(prepare.cwd, repo.join("ui"));
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
