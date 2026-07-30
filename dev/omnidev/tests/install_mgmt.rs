//! Exercises install-management logic without network or a real install:
//! spec building, config round-trip, and the update-availability/staleness
//! decisions.

use std::sync::{Mutex, MutexGuard};

// These modules reference each other via `crate::`, so declare the whole set at
// the test crate root. Each test target exercises only part of the included
// source, so allow dead code rather than chase per-item warnings.
#[allow(dead_code)]
#[path = "../src/install.rs"]
mod install;
#[allow(dead_code)]
#[path = "../src/paths.rs"]
mod paths;
#[allow(dead_code)]
#[path = "../src/update_check.rs"]
mod update_check;

use install::InstallConfig;
use update_check::{is_stale, update_available, CheckCache};

/// Tests here mutate process-global `XDG_*` env vars; serialize them.
static ENV_LOCK: Mutex<()> = Mutex::new(());

fn lock_env() -> MutexGuard<'static, ()> {
    ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

#[test]
fn spec_default_has_databricks_extra_and_main() {
    let c = InstallConfig::default();
    assert_eq!(
        c.spec(),
        "omnigent[databricks] @ git+https://github.com/omnigent-ai/omnigent.git@main"
    );
}

#[test]
fn spec_no_extras_is_bare_git_url() {
    let c = InstallConfig {
        repo: "https://github.com/omnigent-ai/omnigent.git".into(),
        git_ref: "main".into(),
        extras: vec![],
    };
    assert_eq!(
        c.spec(),
        "git+https://github.com/omnigent-ai/omnigent.git@main"
    );
}

#[test]
fn spec_reflects_custom_ref_and_extras() {
    let c = InstallConfig {
        repo: "https://example.com/x.git".into(),
        git_ref: "dev".into(),
        extras: vec!["databricks".into(), "kubernetes".into()],
    };
    assert_eq!(
        c.spec(),
        "omnigent[databricks,kubernetes] @ git+https://example.com/x.git@dev"
    );
}

#[test]
fn config_round_trips_through_disk() {
    let _guard = lock_env();
    let tmp = tempdir();
    std::env::set_var("XDG_CONFIG_HOME", &tmp);

    let c = InstallConfig {
        repo: "https://github.com/omnigent-ai/omnigent.git".into(),
        git_ref: "main".into(),
        extras: vec!["databricks".into()],
    };
    c.save().unwrap();
    let loaded = InstallConfig::load().unwrap().expect("config present");
    assert_eq!(c, loaded);

    std::env::remove_var("XDG_CONFIG_HOME");
}

#[test]
fn missing_config_loads_as_none() {
    let _guard = lock_env();
    let tmp = tempdir();
    std::env::set_var("XDG_CONFIG_HOME", &tmp);

    assert!(InstallConfig::load().unwrap().is_none());

    std::env::remove_var("XDG_CONFIG_HOME");
}

#[test]
fn update_available_logic() {
    let cache = CheckCache {
        remote_sha: Some("bbbb".into()),
        ..Default::default()
    };
    // Remote differs from installed and wasn't prompted → available.
    assert!(update_available(&cache, Some("aaaa")));
    // Installed already matches remote → not available.
    assert!(!update_available(&cache, Some("bbbb")));
    // No remote sha known → not available.
    assert!(!update_available(&CheckCache::default(), Some("aaaa")));

    // Declining a commit (last_prompted_sha == remote) suppresses it.
    let declined = CheckCache {
        remote_sha: Some("bbbb".into()),
        last_prompted_sha: Some("bbbb".into()),
        ..Default::default()
    };
    assert!(!update_available(&declined, Some("aaaa")));
}

#[test]
fn staleness_window() {
    let now = 1_000_000u64;
    let day = 24 * 60 * 60;

    let fresh = CheckCache {
        last_checked: now - 10,
        ..Default::default()
    };
    assert!(!is_stale(&fresh, now));

    let old = CheckCache {
        last_checked: now - day - 1,
        ..Default::default()
    };
    assert!(is_stale(&old, now));

    // Never checked (last_checked == 0) → stale.
    assert!(is_stale(&CheckCache::default(), now));
}

/// Minimal unique temp dir without pulling a dev-dependency.
fn tempdir() -> std::path::PathBuf {
    let base = std::env::temp_dir();
    let unique = format!(
        "omnidev-mgmt-{}-{}",
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
