//! A `Pod` = one isolated dev instance: its own state dir, ports, and the env
//! map injected into every supervised child.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::ports::Ports;

pub struct Pod {
    pub repo_root: PathBuf,
    pub dir: PathBuf,
    pub ports: Ports,
    pub vite_host: String,
    /// LAN origins to trust for device testing (`--trust-lan-origins`); empty
    /// otherwise. Fed to the server as `OMNIGENT_WS_ALLOWED_ORIGINS`.
    pub trusted_origins: Vec<String>,
}

impl Pod {
    /// Create the pod directory tree (idempotent) and return the pod handle.
    /// Only omnigent's own state is isolated (DB, artifacts, logs, config); the
    /// pod inherits your real home, credentials, and caches.
    pub fn create(
        repo_root: PathBuf,
        dir: PathBuf,
        ports: Ports,
        vite_host: String,
        trusted_origins: Vec<String>,
    ) -> Result<Pod> {
        for sub in ["data/omnigent", "artifacts", "logs", "config"] {
            let p = dir.join(sub);
            std::fs::create_dir_all(&p)
                .with_context(|| format!("creating pod dir {}", p.display()))?;
        }
        let pod = Pod {
            repo_root,
            dir,
            ports,
            vite_host,
            trusted_origins,
        };
        // Seed the pod's config from the developer's real one so it works out
        // of the box (keeps their providers). Best-effort: a copy failure just
        // starts the pod with an empty config, so warn rather than abort.
        if let Some(src) = real_config_path() {
            let dest = pod.config_dir().join("config.yaml");
            if let Err(e) = seed_config_file(&src, &dest) {
                eprintln!("omnidev: could not seed pod config: {e:#}");
            }
        }
        Ok(pod)
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

    /// The pod's isolated config home, exposed to children as
    /// `OMNIGENT_CONFIG_HOME` so its `config.yaml` is separate from the
    /// developer's real `~/.omnigent/config.yaml`.
    pub fn config_dir(&self) -> PathBuf {
        self.dir.join("config")
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
    /// child. We isolate omnigent's own state — the DB, data dir, and config
    /// home — so concurrent pods don't share a database, pidfile, or
    /// `config.yaml`. The rest (real `HOME`, credentials, uv/npm caches) is
    /// inherited, since the agents omnigent runs need it. `OMNIGENT_URL` is the
    /// seam `web/vite.config.ts` reads to point its proxy at this pod's backend;
    /// `OMNIGENT_CONFIG_HOME` is where the server/host/runner read `config.yaml`.
    pub fn env(&self) -> Vec<(String, String)> {
        let d = |p: &str| self.dir.join(p).display().to_string();
        let mut env = vec![
            ("OMNIGENT_DATA_DIR".into(), d("data/omnigent")),
            ("OMNIGENT_DATABASE_URI".into(), self.db_uri()),
            ("OMNIGENT_URL".into(), self.server_url()),
            (
                "OMNIGENT_CONFIG_HOME".into(),
                self.config_dir().display().to_string(),
            ),
        ];
        if let Some(allowed) = self.allowed_origins_env() {
            env.push(("OMNIGENT_WS_ALLOWED_ORIGINS".into(), allowed));
        }
        env
    }

    /// The `OMNIGENT_WS_ALLOWED_ORIGINS` value to inject, or `None` to leave it
    /// untouched. Merges the trusted LAN origins onto any value inherited from
    /// the parent environment (comma-separated, order-preserving, deduped) so a
    /// developer's own allowlist survives. Returns `None` when there are no LAN
    /// origins to add — then the parent's value (if any) simply passes through.
    fn allowed_origins_env(&self) -> Option<String> {
        if self.trusted_origins.is_empty() {
            return None;
        }
        let inherited = std::env::var("OMNIGENT_WS_ALLOWED_ORIGINS").unwrap_or_default();
        let mut merged: Vec<String> = Vec::new();
        let parts = inherited
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .chain(self.trusted_origins.iter().cloned());
        for part in parts {
            if !merged.contains(&part) {
                merged.push(part);
            }
        }
        Some(merged.join(","))
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

/// The developer's real omnigent `config.yaml` to seed a fresh pod from.
///
/// Honors `OMNIGENT_CONFIG_HOME` if the parent env sets it (nested/test
/// setups), else `~/.omnigent/config.yaml` via `HOME`. Returns `None` when the
/// file does not exist — a fresh pod then starts with an empty config, just
/// like a first-run user.
fn real_config_path() -> Option<PathBuf> {
    let home = match std::env::var_os("OMNIGENT_CONFIG_HOME") {
        Some(h) if !h.is_empty() => PathBuf::from(h),
        _ => PathBuf::from(std::env::var_os("HOME")?).join(".omnigent"),
    };
    let path = home.join("config.yaml");
    path.exists().then_some(path)
}

/// Copy `src` to `dest`, but only when `dest` does not already exist — a normal
/// pod restart must not clobber config the developer edited inside the pod.
/// After `--clean` the whole pod dir is gone, so `dest` is absent and this
/// re-seeds.
fn seed_config_file(src: &Path, dest: &Path) -> Result<()> {
    if dest.exists() {
        return Ok(());
    }
    std::fs::copy(src, dest)
        .with_context(|| format!("seeding {} from {}", dest.display(), src.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // `real_config_path` reads process-global env; serialize the tests that
    // set it so parallel runs don't observe each other's overrides.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn tempdir() -> PathBuf {
        let unique = format!(
            "omnidev-pod-test-{}-{}",
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

    fn make_pod(pod_dir: PathBuf) -> Pod {
        Pod::create(
            tempdir(),
            pod_dir,
            Ports {
                server: 19191,
                vite: 19292,
            },
            "127.0.0.1".into(),
            Vec::new(),
        )
        .unwrap()
    }

    /// Point `OMNIGENT_CONFIG_HOME` at `home` for the duration of `f`, restoring
    /// the previous value afterwards. Serialized against other env-touching
    /// tests via `ENV_LOCK`.
    fn with_config_home<T>(home: &Path, f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let prev = std::env::var_os("OMNIGENT_CONFIG_HOME");
        std::env::set_var("OMNIGENT_CONFIG_HOME", home);
        let out = f();
        match prev {
            Some(v) => std::env::set_var("OMNIGENT_CONFIG_HOME", v),
            None => std::env::remove_var("OMNIGENT_CONFIG_HOME"),
        }
        out
    }

    #[test]
    fn create_makes_config_dir() {
        let real = tempdir(); // empty config home -> nothing to seed
        let pod = with_config_home(&real, || make_pod(tempdir()));
        assert!(pod.config_dir().is_dir());
    }

    #[test]
    fn env_includes_config_home() {
        let real = tempdir();
        let pod = with_config_home(&real, || make_pod(tempdir()));
        let env = pod.env();
        let got = env
            .iter()
            .find(|(k, _)| k == "OMNIGENT_CONFIG_HOME")
            .map(|(_, v)| v.clone());
        assert_eq!(got, Some(pod.config_dir().display().to_string()));
    }

    #[test]
    fn create_seeds_pod_config_from_real() {
        let real = tempdir();
        std::fs::write(real.join("config.yaml"), "providers:\n  seeded: true\n").unwrap();

        let pod = with_config_home(&real, || make_pod(tempdir()));

        let seeded = std::fs::read_to_string(pod.config_dir().join("config.yaml")).unwrap();
        assert_eq!(seeded, "providers:\n  seeded: true\n");
    }

    #[test]
    fn create_skips_seed_when_real_config_absent() {
        let real = tempdir(); // no config.yaml inside
        let pod = with_config_home(&real, || make_pod(tempdir()));
        assert!(!pod.config_dir().join("config.yaml").exists());
    }

    #[test]
    fn seed_does_not_overwrite_existing() {
        let dir = tempdir();
        let src = dir.join("src.yaml");
        let dest = dir.join("dest.yaml");
        std::fs::write(&src, "from: real\n").unwrap();
        std::fs::write(&dest, "edited: in-pod\n").unwrap();

        seed_config_file(&src, &dest).unwrap();

        // Existing pod-local edits survive; the real config does not clobber them.
        assert_eq!(std::fs::read_to_string(&dest).unwrap(), "edited: in-pod\n");
    }

    #[test]
    fn real_config_path_honors_config_home() {
        let real = tempdir();
        std::fs::write(real.join("config.yaml"), "x: 1\n").unwrap();
        let got = with_config_home(&real, real_config_path);
        assert_eq!(got, Some(real.join("config.yaml")));
    }

    #[test]
    fn real_config_path_falls_back_to_home_dot_omnigent() {
        // With no OMNIGENT_CONFIG_HOME, the real config resolves under
        // `$HOME/.omnigent/` — the path a normal pod run seeds from.
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let prev_cfg = std::env::var_os("OMNIGENT_CONFIG_HOME");
        let prev_home = std::env::var_os("HOME");

        let home = tempdir();
        std::fs::create_dir_all(home.join(".omnigent")).unwrap();
        std::fs::write(home.join(".omnigent/config.yaml"), "y: 2\n").unwrap();

        std::env::remove_var("OMNIGENT_CONFIG_HOME");
        std::env::set_var("HOME", &home);
        let got = real_config_path();

        match prev_cfg {
            Some(v) => std::env::set_var("OMNIGENT_CONFIG_HOME", v),
            None => std::env::remove_var("OMNIGENT_CONFIG_HOME"),
        }
        match prev_home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }

        assert_eq!(got, Some(home.join(".omnigent/config.yaml")));
    }
}
