//! Exercises the non-TUI setup path: repo detection, pod dir tree, ports.

use std::fs;

// The crate is a binary, so pull in the modules under test directly.
#[path = "../src/lock.rs"]
mod lock;
#[path = "../src/paths.rs"]
mod paths;
#[path = "../src/ports.rs"]
mod ports;

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
