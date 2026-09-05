//! Optional process profile for supervising a non-OSS Omnigent integration.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::Deserialize;

#[derive(Clone, Debug, Deserialize)]
pub struct ProcessProfile {
    /// Executable followed by its arguments. Runtime placeholders are expanded
    /// immediately before the process is spawned.
    pub command: Vec<String>,
    #[serde(default)]
    pub cwd: PathBuf,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Profile {
    pub server: ProcessProfile,
    pub vite: ProcessProfile,
    pub prepare: Option<ProcessProfile>,
    pub host: Option<ProcessProfile>,
    pub backend_dir: PathBuf,
    pub web_dir: PathBuf,
    #[serde(default = "default_dependency_manifests")]
    pub dependency_manifests: Vec<PathBuf>,
}

fn default_dependency_manifests() -> Vec<PathBuf> {
    vec![PathBuf::from("package.json")]
}

impl Profile {
    pub fn load(path: &Path) -> Result<Profile> {
        let source = fs::read_to_string(path)
            .with_context(|| format!("reading omnidev profile {}", path.display()))?;
        let profile: Profile = toml::from_str(&source)
            .with_context(|| format!("parsing omnidev profile {}", path.display()))?;
        profile.validate()?;
        Ok(profile)
    }

    fn validate(&self) -> Result<()> {
        for (name, process) in [
            ("server", Some(&self.server)),
            ("vite", Some(&self.vite)),
            ("prepare", self.prepare.as_ref()),
            ("host", self.host.as_ref()),
        ] {
            if process.is_some_and(|p| p.command.is_empty()) {
                bail!("omnidev profile process {name} has an empty command");
            }
        }
        if self.backend_dir.is_absolute() || self.web_dir.is_absolute() {
            bail!(
                "omnidev profile backend_dir and web_dir must be relative to the repository root"
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_external_profile() {
        let profile: Profile = toml::from_str(
            r#"
backend_dir = "service"
web_dir = "ui"

[server]
command = ["server", "--port", "{server_port}"]

[vite]
command = ["vite", "--port", "{vite_port}"]
"#,
        )
        .unwrap();

        profile.validate().unwrap();
        assert!(profile.host.is_none());
        assert_eq!(
            profile.dependency_manifests,
            [PathBuf::from("package.json")]
        );
    }
}
