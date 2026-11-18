//! Confines request paths to a workspace root.

use std::{
    io,
    path::{Component, Path, PathBuf},
};

use thiserror::Error;

#[derive(Debug, Error)]
pub enum PathError {
    #[error("{0}")]
    Invalid(&'static str),
    #[error("The path is outside the workspace")]
    OutsideWorkspace,
    #[error("No such file or directory: {0}")]
    NotFound(String),
    #[error("{0}")]
    Io(#[from] io::Error),
}

/// Normalizes a client path to `/`-separated components, empty for the root.
/// Rejects NUL bytes, absolute paths, and `..`.
pub fn normalize_relative(path: &str) -> Result<String, PathError> {
    if path.contains('\0') {
        return Err(PathError::Invalid("The path contains a NUL byte"));
    }
    if path.starts_with('/') {
        return Err(PathError::Invalid(
            "Use a path relative to the workspace root",
        ));
    }
    let mut components = Vec::new();
    for component in Path::new(path).components() {
        match component {
            Component::Normal(name) => components.push(
                name.to_str()
                    .ok_or(PathError::Invalid("The path is not valid UTF-8"))?,
            ),
            Component::CurDir => {}
            Component::ParentDir => {
                return Err(PathError::Invalid("The path may not contain \"..\""));
            }
            Component::RootDir | Component::Prefix(_) => {
                return Err(PathError::Invalid(
                    "Use a path relative to the workspace root",
                ));
            }
        }
    }
    Ok(components.join("/"))
}

/// A canonical workspace root and the paths beneath it.
#[derive(Clone, Debug)]
pub struct WorkspaceRoot {
    root: PathBuf,
}

impl WorkspaceRoot {
    pub fn open(root: &str) -> Result<Self, PathError> {
        if !root.starts_with('/') {
            return Err(PathError::Invalid(
                "The workspace root must be an absolute path",
            ));
        }
        let root = std::fs::canonicalize(root).map_err(|error| match error.kind() {
            io::ErrorKind::NotFound => PathError::NotFound(root.to_string()),
            _ => PathError::Io(error),
        })?;
        if !root.is_dir() {
            return Err(PathError::Invalid("The workspace root is not a directory"));
        }
        Ok(Self { root })
    }

    pub fn path(&self) -> &Path {
        &self.root
    }

    /// The absolute path for a normalized relative path, without resolving it.
    pub fn join(&self, relative: &str) -> PathBuf {
        if relative.is_empty() {
            self.root.clone()
        } else {
            self.root.join(relative)
        }
    }

    /// Resolves a client path through symlinks and requires the result to
    /// stay inside the root. Returns the normalized relative path and the
    /// resolved absolute path.
    pub fn resolve(&self, path: &str) -> Result<(String, PathBuf), PathError> {
        let relative = normalize_relative(path)?;
        let resolved =
            std::fs::canonicalize(self.join(&relative)).map_err(|error| match error.kind() {
                io::ErrorKind::NotFound => PathError::NotFound(relative.clone()),
                _ => PathError::Io(error),
            })?;
        if !resolved.starts_with(&self.root) {
            return Err(PathError::OutsideWorkspace);
        }
        Ok((relative, resolved))
    }

    /// The `/`-separated path of an absolute path beneath the root.
    pub fn relative(&self, absolute: &Path) -> Option<String> {
        let suffix = absolute.strip_prefix(&self.root).ok()?;
        let text = suffix.to_str()?;
        Some(text.to_string())
    }
}

#[cfg(test)]
#[path = "workspace_path_test.rs"]
mod tests;
