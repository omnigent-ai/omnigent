//! Exercises the non-TUI setup path: repo detection, pod dir tree, ports.

use std::fs;
use std::sync::{Mutex, MutexGuard};

// The crate is a binary, so pull in the modules under test directly. Each test
// target uses only part of the included source, so allow dead code.
#[allow(dead_code)]
#[path = "../src/lock.rs"]
mod lock;
#[allow(dead_code)]
#[path = "../src/paths.rs"]
mod paths;
#[allow(dead_code)]
#[path = "../src/pod.rs"]
mod pod;
#[allow(dead_code)]
#[path = "../src/ports.rs"]
mod ports;

use pod::Pod;
use ports::Ports;

/// A fake checkout (.git + omnigent/ + web/) is recognized as a root, and a
/// nested subdir resolves up to it.
#[test]
fn finds_repo_root_from_subdir() {
    let tmp = tempdir();
    fs::create_dir_all(tmp.join(".git")).unwrap();
    fs::create_dir_all(tmp.join("omnigent/server")).unwrap();
    fs::create_dir_all(tmp.join("web/src")).unwrap();

    let root = paths::find_repo_root(&tmp.join("omnigent/server")).unwrap();
    assert_eq!(root, tmp.canonicalize().unwrap());
}

/// A VCS root without omnigent/+web/ is rejected.
#[test]
fn rejects_non_omnigent_project() {
    let tmp = tempdir();
    fs::create_dir_all(tmp.join(".git")).unwrap();
    assert!(paths::find_repo_root(&tmp).is_err());
}

/// Two different repo paths get distinct pod dirs; the same path is stable.
#[test]
fn pod_dir_is_per_repo_and_stable() {
    let a1 = paths::default_pod_dir(std::path::Path::new("/repos/one")).unwrap();
    let a2 = paths::default_pod_dir(std::path::Path::new("/repos/one")).unwrap();
    let b = paths::default_pod_dir(std::path::Path::new("/repos/two")).unwrap();
    assert_eq!(a1, a2);
    assert_ne!(a1, b);
}

/// npm install is needed when node_modules is missing, and when a manifest is
/// newer than it; not needed when node_modules is up to date.
#[test]
fn needs_npm_install_tracks_manifests() {
    let repo = tempdir();
    let web = repo.join("web");
    fs::create_dir_all(&web).unwrap();
    fs::write(web.join("package.json"), "{}").unwrap();

    let pod = Pod {
        repo_root: repo.clone(),
        dir: repo.join("pod"),
        ports: Ports {
            server: 6767,
            vite: 5173,
        },
        vite_host: "127.0.0.1".into(),
        trusted_origins: Vec::new(),
    };

    // No node_modules yet → install needed.
    assert!(pod.needs_npm_install());

    // Fresh node_modules created after the manifest → up to date.
    fs::create_dir_all(web.join("node_modules")).unwrap();
    assert!(!pod.needs_npm_install());

    // A manifest touched after node_modules → stale, install needed.
    // (Sleep briefly so the mtime is observably newer on coarse filesystems.)
    std::thread::sleep(std::time::Duration::from_millis(10));
    fs::write(web.join("package-lock.json"), "{}").unwrap();
    assert!(pod.needs_npm_install());
}

/// Ports probe to bindable values and persist/reuse across calls.
#[test]
fn ports_resolve_and_persist() {
    let tmp = tempdir();
    let p1 = Ports::resolve(&tmp, None, None).unwrap();
    assert_ne!(p1.server, p1.vite);
    assert!(tmp.join("pod.toml").is_file());

    // A second resolve reuses the persisted pair (both still free).
    let p2 = Ports::resolve(&tmp, None, None).unwrap();
    assert_eq!(p1.server, p2.server);
    assert_eq!(p1.vite, p2.vite);

    // Explicit overrides win.
    let p3 = Ports::resolve(&tmp, Some(19191), Some(19292)).unwrap();
    assert_eq!(p3.server, 19191);
    assert_eq!(p3.vite, 19292);
}

/// Two sibling pods under the same cache root never collide, even before their
/// processes have bound anything — the second reads the first's pod.toml.
#[test]
fn sibling_pods_get_distinct_ports() {
    let root = tempdir();
    let pod_a = root.join("repo-aaaa");
    let pod_b = root.join("repo-bbbb");
    fs::create_dir_all(&pod_a).unwrap();
    fs::create_dir_all(&pod_b).unwrap();

    // Pod A resolves and persists first (no process is ever spawned).
    let a = Ports::resolve(&pod_a, None, None).unwrap();
    // Pod B must avoid A's ports purely from A's persisted claim.
    let b = Ports::resolve(&pod_b, None, None).unwrap();

    assert_ne!(a.server, b.server);
    assert_ne!(a.vite, b.vite);
    assert_ne!(a.server, b.vite);
    assert_ne!(a.vite, b.server);
}

/// A pod admits one holder; a second acquire fails until the first is dropped.
#[test]
fn pod_lock_is_exclusive() {
    let pod = tempdir();

    let held = lock::acquire(&pod).expect("first acquire succeeds");
    assert!(
        lock::acquire(&pod).is_err(),
        "second acquire must fail while the first is held"
    );

    drop(held);
    lock::acquire(&pod).expect("acquire succeeds again after release");
}

const ALLOWED_ORIGINS_ENV: &str = "OMNIGENT_WS_ALLOWED_ORIGINS";

/// Tests that read/write the process-global allowlist env var; serialize them.
static ENV_LOCK: Mutex<()> = Mutex::new(());

fn lock_env() -> MutexGuard<'static, ()> {
    ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

/// Run `body` with `OMNIGENT_WS_ALLOWED_ORIGINS` set to `value` (or unset when
/// `None`), restoring the prior value afterward so tests don't leak env state.
fn with_allowlist_env(value: Option<&str>, body: impl FnOnce()) {
    let _guard = lock_env();
    let prev = std::env::var(ALLOWED_ORIGINS_ENV).ok();
    match value {
        Some(v) => std::env::set_var(ALLOWED_ORIGINS_ENV, v),
        None => std::env::remove_var(ALLOWED_ORIGINS_ENV),
    }
    body();
    match prev {
        Some(v) => std::env::set_var(ALLOWED_ORIGINS_ENV, v),
        None => std::env::remove_var(ALLOWED_ORIGINS_ENV),
    }
}

fn pod_with_trusted(trusted: Vec<String>) -> Pod {
    Pod {
        repo_root: std::path::PathBuf::from("/repo"),
        dir: std::path::PathBuf::from("/pod"),
        ports: Ports {
            server: 6767,
            vite: 5173,
        },
        vite_host: "0.0.0.0".into(),
        trusted_origins: trusted,
    }
}

fn allowlist_from_env(pod: &Pod) -> Option<String> {
    pod.env()
        .into_iter()
        .find(|(k, _)| k == ALLOWED_ORIGINS_ENV)
        .map(|(_, v)| v)
}

/// With no trusted origins, the pod leaves the allowlist var untouched — even
/// when the developer's shell already exports one (it passes through inherited).
#[test]
fn no_trusted_origins_does_not_set_allowlist() {
    with_allowlist_env(Some("https://dev.example.com"), || {
        let pod = pod_with_trusted(Vec::new());
        assert_eq!(allowlist_from_env(&pod), None);
    });
}

/// Trusted origins with no inherited value produce exactly those origins.
#[test]
fn trusted_origins_populate_allowlist() {
    with_allowlist_env(None, || {
        let pod = pod_with_trusted(vec!["http://192.168.1.42:5173".into()]);
        assert_eq!(
            allowlist_from_env(&pod).as_deref(),
            Some("http://192.168.1.42:5173")
        );
    });
}

/// A developer's inherited allowlist is preserved and the LAN origins are
/// appended (order-preserving, deduped) rather than clobbered.
#[test]
fn trusted_origins_merge_with_inherited_allowlist() {
    with_allowlist_env(
        Some("https://dev.example.com, http://192.168.1.42:5173"),
        || {
            let pod = pod_with_trusted(vec![
                "http://192.168.1.42:5173".into(), // already inherited → not duplicated
                "http://10.0.0.9:5173".into(),
            ]);
            assert_eq!(
                allowlist_from_env(&pod).as_deref(),
                Some("https://dev.example.com,http://192.168.1.42:5173,http://10.0.0.9:5173")
            );
        },
    );
}

/// Minimal unique temp dir without pulling a dev-dependency.
fn tempdir() -> std::path::PathBuf {
    let base = std::env::temp_dir();
    let unique = format!(
        "omnidev-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let dir = base.join(unique);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}
