//! ListDirectory and ReadFile exactly as the Python host's
//! `omnigent.workspace_fs.WorkspaceReader` answers them, so a client can't
//! tell which one served a call: its path rules, its errors, and its read cap.

use std::{
    fs,
    io::Read,
    os::unix::fs::OpenOptionsExt,
    path::{Component, Path, PathBuf},
};

use tonic::Status;

use crate::{proto::Entry, workspace::Workspace};

/// The host reader's read limit, `_MAX_READ_BYTES`.
pub const MAX_READ_BYTES: usize = 10 * 1024 * 1024;
/// The host's largest page, `_MAX_PAGE_SIZE`.
pub const MAX_PAGE_SIZE: usize = 1000;
/// Symlinks followed while resolving one path before giving up, as Linux does.
const MAX_SYMLINKS: usize = 40;

/// Python's `repr` of a path, for messages that match the host's.
pub fn quoted(path: &str) -> String {
    if path.contains('\'') && !path.contains('"') {
        format!("\"{path}\"")
    } else {
        format!("'{}'", path.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// `posixpath.normpath`: collapses `.`, `//` and `name/..` without touching
/// the disk, keeping leading `..` components.
fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let absolute = path.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|last| *last != "..") => {
                parts.pop();
            }
            ".." if absolute => {}
            part => parts.push(part),
        }
    }
    let joined = parts.join("/");
    match (absolute, joined.is_empty()) {
        (true, _) => format!("/{joined}"),
        (false, true) => ".".into(),
        (false, false) => joined,
    }
}

/// `_validate_path`: the normalized relative path, empty for the root.
fn validate(path: &str) -> Result<String, Status> {
    if path.contains('\0') {
        return Err(Status::invalid_argument("Path contains NUL bytes"));
    }
    let normalized = normpath(path);
    if normalized.starts_with('/') {
        return Err(Status::invalid_argument("Absolute paths are not allowed"));
    }
    if normalized.starts_with("..") {
        return Err(Status::invalid_argument("Path traversal is not allowed"));
    }
    Ok(if normalized == "." {
        String::new()
    } else {
        normalized
    })
}

/// `os.path.realpath(path, strict=False)`: follows every symlink that exists
/// and keeps missing components as they are. `None` after following
/// [`MAX_SYMLINKS`] links: a longer chain or a loop never resolves, so no
/// link it still holds can lead the open outside the root.
fn realpath(path: &Path) -> Option<PathBuf> {
    let mut resolved = PathBuf::from("/");
    let mut pending: Vec<std::ffi::OsString> = path
        .components()
        .rev()
        .filter_map(|component| match component {
            Component::Normal(name) => Some(name.to_os_string()),
            Component::ParentDir => Some("..".into()),
            _ => None,
        })
        .collect();
    let mut followed = 0;
    while let Some(name) = pending.pop() {
        if name == ".." {
            resolved.pop();
            continue;
        }
        let candidate = resolved.join(&name);
        match fs::read_link(&candidate) {
            Ok(_) if followed == MAX_SYMLINKS => return None,
            Ok(target) => {
                followed += 1;
                if target.is_absolute() {
                    resolved = PathBuf::from("/");
                }
                pending.extend(
                    target
                        .components()
                        .rev()
                        .filter_map(|component| match component {
                            Component::Normal(name) => Some(name.to_os_string()),
                            Component::ParentDir => Some("..".into()),
                            _ => None,
                        }),
                );
            }
            Err(_) => resolved = candidate,
        }
    }
    Some(resolved)
}

/// `_resolve_workspace_path`: the validated relative path and where it
/// resolves, which must be inside the root.
pub fn resolve(workspace: &Workspace, path: &str) -> Result<(String, PathBuf), Status> {
    let validated = validate(path)?;
    let root = workspace.root.path();
    if validated.is_empty() {
        return Ok((validated, root.to_path_buf()));
    }
    let full = realpath(&root.join(&validated)).ok_or_else(|| {
        Status::invalid_argument(format!(
            "Path {} goes through too many symlinks",
            quoted(path)
        ))
    })?;
    if !full.starts_with(root) {
        return Err(Status::invalid_argument(format!(
            "Path {} escapes the workspace root",
            quoted(path)
        )));
    }
    Ok((validated, full))
}

/// The error the host gives for a directory it can't list.
pub fn unlistable(shown: &str) -> Status {
    Status::not_found(format!(
        "Directory {} not found or not accessible",
        quoted(shown)
    ))
}

/// One page of a listing in name order, from just after `page_token`, and the
/// token for the next page. A token whose entry is gone still resumes at its
/// place, since paths sort like names under one parent.
pub fn page(
    entries: Vec<Entry>,
    page_size: i32,
    page_token: &str,
) -> Result<(Vec<Entry>, String), Status> {
    if page_size < 0 {
        return Err(Status::invalid_argument("page_size must not be negative"));
    }
    let limit = match page_size as usize {
        0 => MAX_PAGE_SIZE,
        size => size.min(MAX_PAGE_SIZE),
    };
    let start = if page_token.is_empty() {
        0
    } else {
        entries.partition_point(|entry| entry.path.as_str() <= page_token)
    };
    let more = start + limit < entries.len();
    let page: Vec<Entry> = entries.into_iter().skip(start).take(limit).collect();
    let next_page_token = match page.last() {
        Some(last) if more => last.path.clone(),
        _ => String::new(),
    };
    Ok((page, next_page_token))
}

/// `WorkspaceReader._read_capped`: up to the limit plus one byte of a regular
/// file, opened without blocking so a FIFO or device can't hang the read.
pub fn read_capped(
    resolved: &Path,
    shown: &str,
    maximum: usize,
) -> Result<(Vec<u8>, fs::Metadata), Status> {
    let not_found = || Status::not_found(format!("Path {} not found", quoted(shown)));
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(resolved)
        .map_err(|_| not_found())?;
    let metadata = file.metadata().map_err(|_| not_found())?;
    if !metadata.is_file() {
        return Err(not_found());
    }
    let mut data = Vec::new();
    file.take(maximum as u64 + 1)
        .read_to_end(&mut data)
        .map_err(|_| not_found())?;
    Ok((data, metadata))
}

/// The bytes to deliver and how to describe them.
#[derive(Debug, PartialEq)]
pub struct CappedFile {
    pub data: Vec<u8>,
    pub truncated: bool,
    pub is_text: bool,
}

/// `_cap_file_bytes`: caps at `maximum` and decides whether the bytes are
/// UTF-8. A codepoint split by the cap is dropped so oversize text stays text.
pub fn cap_file_bytes(mut data: Vec<u8>, maximum: usize) -> CappedFile {
    let truncated = data.len() > maximum;
    data.truncate(maximum);
    let is_text = match std::str::from_utf8(&data) {
        Ok(_) => true,
        Err(error) if truncated && error.valid_up_to() + 3 >= data.len() => {
            data.truncate(error.valid_up_to());
            true
        }
        Err(_) => false,
    };
    CappedFile {
        data,
        truncated,
        is_text,
    }
}

#[cfg(test)]
#[path = "host_reader_test.rs"]
mod tests;
