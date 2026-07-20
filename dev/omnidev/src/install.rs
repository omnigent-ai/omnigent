//! Manage the user's git-based omnigent installation via `uv tool install`.
//!
//! None of this needs a local checkout: it drives `uv` and reads the installed
//! tool's metadata, and any git call targets the remote.

use std::path::PathBuf;
use std::process::Command;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::paths;

pub const DEFAULT_REPO: &str = "https://github.com/omnigent-ai/omnigent.git";
pub const DEFAULT_REF: &str = "main";
pub const DEFAULT_EXTRA: &str = "databricks";
const PYTHON_VERSION: &str = "3.12";

/// Durable record of how the user wants omnigent installed. Persisted so
/// `update` reinstalls the same repo/ref/extras without re-specifying them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InstallConfig {
    pub repo: String,
    #[serde(rename = "ref")]
    pub git_ref: String,
    pub extras: Vec<String>,
}

impl Default for InstallConfig {
    fn default() -> Self {
        InstallConfig {
            repo: DEFAULT_REPO.to_string(),
            git_ref: DEFAULT_REF.to_string(),
            extras: vec![DEFAULT_EXTRA.to_string()],
        }
    }
}

impl InstallConfig {
    pub fn load() -> Result<Option<InstallConfig>> {
        let path = paths::install_config_path()?;
        match std::fs::read_to_string(&path) {
            Ok(text) => Ok(Some(
                toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?,
            )),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
        }
    }

    pub fn save(&self) -> Result<()> {
        let path = paths::install_config_path()?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        let text = toml::to_string(self).context("serializing install config")?;
        std::fs::write(&path, text).with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }

    /// The PEP 508 install spec, e.g.
    /// `omnigent[databricks] @ git+https://github.com/omnigent-ai/omnigent.git@main`.
    /// With no extras it collapses to the bare `git+<repo>@<ref>` URL.
    pub fn spec(&self) -> String {
        let source = format!("git+{}@{}", self.repo, self.git_ref);
        if self.extras.is_empty() {
            source
        } else {
            format!("omnigent[{}] @ {}", self.extras.join(","), source)
        }
    }
}

/// Fail early with a clear message if the toolchain a git install needs is
/// missing. Installing from git builds the web UI from source (Node/npm),
/// unlike the PyPI wheel which ships it prebuilt.
fn preflight() -> Result<()> {
    if which("uv").is_none() {
        bail!("`uv` is not on PATH. Install it first: https://docs.astral.sh/uv/");
    }
    if which("npm").is_none() {
        bail!(
            "`npm` is not on PATH. Installing omnigent from git builds the web UI \
             from source and needs Node 22+/npm. Install Node, then retry."
        );
    }
    Ok(())
}

/// Install omnigent from git per `config`. `reinstall` forces uv past its cache
/// so a moving ref (e.g. `main`) actually re-resolves.
pub fn run_uv_install(config: &InstallConfig, reinstall: bool) -> Result<()> {
    preflight()?;
    let spec = config.spec();

    let mut cmd = Command::new("uv");
    cmd.args(["tool", "install", "--force", "--python", PYTHON_VERSION]);
    if reinstall {
        cmd.arg("--reinstall");
    }
    cmd.arg(&spec);

    eprintln!("omnidev: uv tool install {spec}");
    let status = cmd
        .status()
        .context("running `uv tool install` (is uv installed?)")?;
    if !status.success() {
        bail!("`uv tool install` failed ({status})");
    }
    Ok(())
}

/// `install` subcommand: persist intent, install, then record the resolved sha.
pub fn install(config: &InstallConfig) -> Result<()> {
    config.save()?;
    run_uv_install(config, false)?;
    record_installed_sha(config);
    println!("omnidev: installed omnigent ({})", config.spec());
    Ok(())
}

/// `update` subcommand: reinstall the latest of the persisted ref/extras. Falls
/// back to defaults when no config has been written yet.
pub fn update() -> Result<()> {
    let config = InstallConfig::load()?.unwrap_or_default();
    config.save()?;
    run_uv_install(&config, true)?;
    record_installed_sha(&config);
    println!("omnidev: updated omnigent ({})", config.spec());
    Ok(())
}

/// After a successful install, capture the remote sha of the tracked ref and
/// stash it in the cache so `check` has a baseline even before the dist-info
/// reader runs. Best-effort — failures here never fail the install.
fn record_installed_sha(config: &InstallConfig) {
    if let Some(sha) = crate::update_check::remote_sha(&config.repo, &config.git_ref) {
        let _ = crate::update_check::set_installed_sha(&sha);
    }
}

/// Read the commit the installed omnigent tool was built from, via its PEP 610
/// `direct_url.json`. Returns `None` for a non-VCS install or when uv/metadata
/// can't be read. Never touches the working directory.
pub fn installed_commit() -> Option<String> {
    let dir = uv_tool_dir()?;
    // …/omnigent/**/omnigent-*.dist-info/direct_url.json
    let omnigent_root = dir.join("omnigent");
    let dist_info = find_dist_info(&omnigent_root)?;
    let text = std::fs::read_to_string(dist_info.join("direct_url.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value
        .get("vcs_info")?
        .get("commit_id")?
        .as_str()
        .map(str::to_string)
}

fn uv_tool_dir() -> Option<PathBuf> {
    let output = Command::new("uv").args(["tool", "dir"]).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let path = String::from_utf8(output.stdout).ok()?;
    let trimmed = path.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(PathBuf::from(trimmed))
    }
}

/// Find the `omnigent-*.dist-info` dir under a uv tool's environment. uv lays
/// tools out as `<tool>/lib/pythonX.Y/site-packages/<pkg>-<ver>.dist-info`, so
/// we walk rather than hardcode the python version.
fn find_dist_info(root: &std::path::Path) -> Option<PathBuf> {
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with("omnigent-") && name.ends_with(".dist-info") {
                return Some(path);
            }
            stack.push(path);
        }
    }
    None
}

/// Locate an executable on PATH (portable `which`, no external dep).
fn which(program: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(program);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}
