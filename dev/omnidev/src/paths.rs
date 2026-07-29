//! Repo-root discovery and per-repo pod-directory resolution.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

/// Walk up from `start` looking for the checkout root.
///
/// The root is the first ancestor holding a `.jj/` or `.git/` marker — the VCS
/// root. We then require `web/` and `omnigent/` to be present so we fail early
/// on an unrelated repo rather than mid-spawn.
pub fn find_repo_root(start: &Path) -> Result<PathBuf> {
    let start = start
        .canonicalize()
        .with_context(|| format!("resolving start dir {}", start.display()))?;

    let mut cur: Option<&Path> = Some(&start);
    while let Some(dir) = cur {
        if dir.join(".jj").is_dir() || dir.join(".git").exists() {
            let root = dir.to_path_buf();
            if !root.join("omnigent").is_dir() || !root.join("web").is_dir() {
                bail!(
                    "found a VCS root at {} but it lacks omnigent/ and web/ — \
                     run omnidev from inside an Omnigent checkout",
                    root.display()
                );
            }
            return Ok(root);
        }
        cur = dir.parent();
    }
    bail!(
        "could not find a checkout root above {} (no .jj or .git marker)",
        start.display()
    )
}

/// Stable per-repo pod directory: `${XDG_CACHE_HOME:-~/.cache}/omnidev/<slug>-<hash8>/`.
///
/// The hash of the canonical repo path keeps two worktrees on distinct pods;
/// the slug (repo basename) keeps the path human-readable.
pub fn default_pod_dir(repo_root: &Path) -> Result<PathBuf> {
    let cache = cache_home()?;
    let slug = repo_root
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "repo".to_string());
    let hash = short_hash(repo_root.to_string_lossy().as_bytes());
    Ok(cache.join("omnidev").join(format!("{slug}-{hash}")))
}

/// `${XDG_CACHE_HOME:-~/.cache}`.
pub fn cache_home() -> Result<PathBuf> {
    if let Some(x) = std::env::var_os("XDG_CACHE_HOME") {
        if !x.is_empty() {
            return Ok(PathBuf::from(x));
        }
    }
    let home = std::env::var_os("HOME").context("HOME is not set")?;
    Ok(PathBuf::from(home).join(".cache"))
}

/// `${XDG_CONFIG_HOME:-~/.config}`.
pub fn config_home() -> Result<PathBuf> {
    if let Some(x) = std::env::var_os("XDG_CONFIG_HOME") {
        if !x.is_empty() {
            return Ok(PathBuf::from(x));
        }
    }
    let home = std::env::var_os("HOME").context("HOME is not set")?;
    Ok(PathBuf::from(home).join(".config"))
}

/// `~/.config/omnidev/install.toml` — durable record of install intent.
pub fn install_config_path() -> Result<PathBuf> {
    Ok(config_home()?.join("omnidev").join("install.toml"))
}

/// `~/.cache/omnidev/omnigent-check.json` — volatile update-check state.
pub fn check_cache_path() -> Result<PathBuf> {
    Ok(cache_home()?.join("omnidev").join("omnigent-check.json"))
}

/// FNV-1a 64-bit, rendered as 8 hex chars. No external dep needed — we only
/// need a stable, collision-unlikely tag for a filesystem path.
fn short_hash(bytes: &[u8]) -> String {
    let mut hash: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{:08x}", (hash ^ (hash >> 32)) as u32)
}
