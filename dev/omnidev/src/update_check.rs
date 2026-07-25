//! Daily update check for a git-installed omnigent.
//!
//! Fills a real gap: omnigent's own update notice only works for PyPI-wheel
//! installs and bails on VCS installs. The hot path (`check`) never blocks on
//! the network — it reads a cache and spawns a detached `refresh` when stale.

use std::io::{IsTerminal, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::install::{self, InstallConfig};
use crate::paths;

const STALE_SECS: u64 = 24 * 60 * 60;
const LS_REMOTE_TIMEOUT_SECS: u64 = 5;

/// Volatile update-check state cached between runs.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CheckCache {
    #[serde(default)]
    pub last_checked: u64,
    #[serde(default)]
    pub remote_sha: Option<String>,
    #[serde(default)]
    pub installed_sha: Option<String>,
    /// The remote sha we already prompted about, so a declined update isn't
    /// re-nagged until a newer commit lands.
    #[serde(default)]
    pub last_prompted_sha: Option<String>,
}

impl CheckCache {
    pub fn load() -> CheckCache {
        let Ok(path) = paths::check_cache_path() else {
            return CheckCache::default();
        };
        std::fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) -> Result<()> {
        let path = paths::check_cache_path()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        let text = serde_json::to_string_pretty(self).context("serializing check cache")?;
        std::fs::write(&path, text).with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }
}

/// Whether the cache indicates an update the user hasn't already declined.
/// Pure so it can be unit-tested without touching disk or the network.
///
/// `installed` is the best-known installed commit (dist-info first, else the
/// cached `installed_sha`). An update is available when we have a remote sha
/// that differs from what's installed and that we haven't already prompted for.
pub fn update_available(cache: &CheckCache, installed: Option<&str>) -> bool {
    let Some(remote) = cache.remote_sha.as_deref() else {
        return false;
    };
    if Some(remote) == installed {
        return false;
    }
    if cache.last_prompted_sha.as_deref() == Some(remote) {
        return false;
    }
    true
}

/// Whether `last_checked` is older than the staleness window.
pub fn is_stale(cache: &CheckCache, now: u64) -> bool {
    now.saturating_sub(cache.last_checked) > STALE_SECS
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// The remote HEAD sha of `git_ref` in `repo`, via `git ls-remote` (targets the
/// remote, so no local checkout is needed). `None` on any failure/timeout.
pub fn remote_sha(repo: &str, git_ref: &str) -> Option<String> {
    // `timeout` isn't portable (absent on macOS by default), so bound the call
    // with git's own connect timeout and a wait guard instead.
    let mut child = Command::new("git")
        .args(["ls-remote", repo, git_ref])
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .stdin(Stdio::null())
        .spawn()
        .ok()?;

    let deadline = SystemTime::now() + Duration::from_secs(LS_REMOTE_TIMEOUT_SECS);
    loop {
        match child.try_wait().ok()? {
            Some(_) => break,
            None => {
                if SystemTime::now() > deadline {
                    let _ = child.kill();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
    }
    let output = child.wait_with_output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    // First whitespace-delimited token of the first line is the sha.
    text.lines()
        .next()
        .and_then(|l| l.split_whitespace().next())
        .map(str::to_string)
}

/// Record the installed sha into the cache (called after install/update).
pub fn set_installed_sha(sha: &str) -> Result<()> {
    let mut cache = CheckCache::load();
    cache.installed_sha = Some(sha.to_string());
    cache.save()
}

/// `refresh` subcommand: hit the network, update `remote_sha` + `last_checked`.
/// Invoked detached by `check`, but also runnable directly.
pub fn refresh() -> Result<()> {
    let config = InstallConfig::load()?.unwrap_or_default();
    let mut cache = CheckCache::load();
    cache.remote_sha = remote_sha(&config.repo, &config.git_ref);
    cache.last_checked = now_epoch();
    cache.save()
}

/// Best-known installed commit: the tool's dist-info first (authoritative),
/// else the sha we recorded at install time.
fn installed_commit(cache: &CheckCache) -> Option<String> {
    install::installed_commit().or_else(|| cache.installed_sha.clone())
}

/// `check` subcommand: the fast hook primitive. Never blocks on the network.
///
/// - Stale cache ⇒ spawn a detached `refresh` and return.
/// - An available update ⇒ notice; on a TTY, prompt and update in the
///   foreground on yes, else record the decline.
/// - `quiet` suppresses the "up to date" path so shell startup stays silent.
pub fn check(quiet: bool) -> Result<()> {
    let cache = CheckCache::load();

    if is_stale(&cache, now_epoch()) {
        spawn_detached_refresh();
        // Still evaluate against whatever we already had cached.
    }

    let installed = installed_commit(&cache);
    if !update_available(&cache, installed.as_deref()) {
        if !quiet {
            println!("omnigent is up to date.");
        }
        return Ok(());
    }

    let remote = cache.remote_sha.clone().unwrap_or_default();
    let short = |s: &str| s.chars().take(8).collect::<String>();
    let installed_desc = installed
        .as_deref()
        .map(short)
        .unwrap_or_else(|| "unknown".to_string());
    eprintln!(
        "omnigent update available: {} → {} (git)",
        installed_desc,
        short(&remote),
    );

    // Only prompt on an interactive terminal; scripts/CI just see the notice.
    if !(std::io::stdin().is_terminal() && std::io::stderr().is_terminal()) {
        return Ok(());
    }

    if prompt_yes_no("Update omnigent now? [y/N] ") {
        install::update()?;
    } else {
        // Don't re-nag for this same commit.
        let mut cache = CheckCache::load();
        cache.last_prompted_sha = Some(remote);
        cache.save()?;
    }
    Ok(())
}

/// Prompt on the controlling terminal. Reads from `/dev/tty` so it works even
/// when the hook's stdin is redirected. Any read failure ⇒ treated as "no".
fn prompt_yes_no(prompt: &str) -> bool {
    use std::io::BufRead;
    let Ok(tty) = std::fs::OpenOptions::new().read(true).open("/dev/tty") else {
        return false;
    };
    eprint!("{prompt}");
    let _ = std::io::stderr().flush();
    let mut line = String::new();
    if std::io::BufReader::new(tty).read_line(&mut line).is_err() {
        return false;
    }
    matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

/// Launch `omnidev refresh` fully detached so shell startup never waits on it.
fn spawn_detached_refresh() {
    let Ok(exe) = std::env::current_exe() else {
        return;
    };
    let _ = Command::new(exe)
        .arg("refresh")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}
