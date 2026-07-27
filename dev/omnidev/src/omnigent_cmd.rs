//! `omnidev omnigent …` — run an arbitrary omnigent command against this
//! checkout's pod via `uv run omnigent …`.
//!
//! Unlike the supervised `process::ProcSpec`s, this runs in the foreground
//! (inheriting the user's stdio) and does *not* inject the log-mirror env
//! (`OMNIGENT_LOG_TTY_FD` / `OMNIGENT_LOG_FORCE_COLOR`): the user has a real
//! TTY, so omnigent's own terminal detection should win. The pod's isolation
//! env (`OMNIGENT_DATA_DIR`, `OMNIGENT_DATABASE_URI`, `OMNIGENT_CONFIG_HOME`,
//! `OMNIGENT_URL`) is applied on top of the inherited parent env, so a command
//! talks to the same pod the supervisor runs.

use std::path::PathBuf;

use crate::pod::Pod;

/// A resolved `uv run omnigent …` invocation for the passthrough subcommand.
pub struct OmnigentCmd {
    pub program: String,
    pub args: Vec<String>,
    pub env: Vec<(String, String)>,
    pub cwd: PathBuf,
}

/// Build the command line + env for `uv run omnigent <passthrough…>` rooted at
/// the pod's repo, with the pod's `OMNIGENT_*` overrides applied.
pub fn build(pod: &Pod, passthrough: &[String]) -> OmnigentCmd {
    let mut args = vec!["run".to_string(), "omnigent".to_string()];
    args.extend_from_slice(passthrough);
    OmnigentCmd {
        program: "uv".into(),
        args,
        env: pod.env(),
        cwd: pod.repo_root.clone(),
    }
}

/// Spawn the command in the foreground, inheriting stdio, and exit with its
/// status code. A spawn failure returns an error instead of exiting.
pub fn run(cmd: OmnigentCmd) -> anyhow::Result<()> {
    let mut command = std::process::Command::new(&cmd.program);
    command.args(&cmd.args).current_dir(&cmd.cwd);
    for (k, v) in &cmd.env {
        command.env(k, v);
    }
    let status = command.status()?;
    std::process::exit(status.code().unwrap_or(1));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ports::Ports;

    fn tempdir() -> PathBuf {
        let unique = format!(
            "omnidev-omnigent-cmd-test-{}-{}",
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

    fn make_pod() -> Pod {
        Pod::create(
            tempdir(),
            tempdir(),
            Ports {
                server: 19191,
                vite: 19292,
            },
            "127.0.0.1".into(),
            Vec::new(),
        )
        .unwrap()
    }

    #[test]
    fn forwards_passthrough_args_after_uv_run_omnigent() {
        let pod = make_pod();
        let cmd = build(
            &pod,
            &["agent".into(), "run".into(), "fix tests".into()],
        );
        assert_eq!(cmd.program, "uv");
        assert_eq!(
            cmd.args.iter().map(String::as_str).collect::<Vec<_>>(),
            vec!["run", "omnigent", "agent", "run", "fix tests"]
        );
        assert_eq!(cmd.cwd, pod.repo_root);
    }

    #[test]
    fn empty_passthrough_is_just_uv_run_omnigent() {
        let pod = make_pod();
        let cmd = build(&pod, &[]);
        assert_eq!(
            cmd.args.iter().map(String::as_str).collect::<Vec<_>>(),
            vec!["run", "omnigent"]
        );
    }

    #[test]
    fn applies_pod_isolation_env() {
        let pod = make_pod();
        let cmd = build(&pod, &["config".into(), "show".into()]);

        let data_dir = cmd
            .env
            .iter()
            .find(|(k, _)| k == "OMNIGENT_DATA_DIR")
            .map(|(_, v)| v.clone());
        assert_eq!(
            data_dir,
            Some(pod.dir.join("data/omnigent").display().to_string())
        );

        let url = cmd
            .env
            .iter()
            .find(|(k, _)| k == "OMNIGENT_URL")
            .map(|(_, v)| v.clone());
        assert_eq!(url, Some(pod.server_url()));

        let db = cmd
            .env
            .iter()
            .find(|(k, _)| k == "OMNIGENT_DATABASE_URI")
            .map(|(_, v)| v.clone());
        assert_eq!(db, Some(pod.db_uri()));
    }

    #[test]
    fn omits_log_mirror_env() {
        let pod = make_pod();
        let cmd = build(&pod, &["host".into()]);
        assert!(cmd.env.iter().find(|(k, _)| k == "OMNIGENT_LOG_TTY_FD").is_none());
        assert!(cmd
            .env
            .iter()
            .find(|(k, _)| k == "OMNIGENT_LOG_FORCE_COLOR")
            .is_none());
    }
}
