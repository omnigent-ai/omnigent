//! Which directories are Git repositories, as git decides (`is_git_directory`):
//! a `.git` directory, or a `.git` file naming one with `gitdir:`, that holds
//! a valid HEAD and, in its common directory, `objects/` and `refs/`. An
//! empty `.git` file or a `.git` directory with only some of that is not a
//! repository, so its directory's rules don't apply.

use std::{
    ffi::{CString, OsStr},
    fs,
    io::Read,
    os::unix::{ffi::OsStrExt, fs::OpenOptionsExt},
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq)]
pub struct Repository {
    /// The working tree's top level, holding `.git`.
    pub root: PathBuf,
    /// Where HEAD is: `.git` itself, or what a `.git` file names.
    pub git_directory: PathBuf,
    /// Where `objects/`, `refs/` and `info/exclude` are: the git directory, or
    /// the main repository's for a linked worktree.
    pub common_directory: PathBuf,
}

/// Git reads a `.git` file only up to this size.
const MAXIMUM_GITFILE_BYTES: u64 = 1 << 20;
/// Git reads only this much of HEAD, and judges it by that prefix.
const MAXIMUM_HEAD_BYTES: u64 = 255;

/// The repository whose top level is `directory`, if it has a valid `.git`.
pub fn repository_at(directory: &Path) -> Option<Repository> {
    let dot_git = directory.join(".git");
    let metadata = fs::metadata(&dot_git).ok()?;
    let git_directory = if metadata.is_dir() {
        dot_git
    } else if metadata.is_file() && metadata.len() <= MAXIMUM_GITFILE_BYTES {
        let contents = read_whole_file(&dot_git, MAXIMUM_GITFILE_BYTES)?;
        // Git reads the whole file and trims only the line ending.
        let target = without_line_ending(contents.strip_prefix(b"gitdir: ")?);
        if target.is_empty() {
            return None;
        }
        directory.join(OsStr::from_bytes(target))
    } else {
        // A FIFO, socket or device named `.git` is never opened.
        return None;
    };
    let common_directory = common_directory(&git_directory)?;
    Some(Repository {
        root: directory.to_path_buf(),
        git_directory,
        common_directory,
    })
}

/// The nearest repository whose top level is `path` or one of its ancestors.
pub fn discover(path: &Path) -> Option<Repository> {
    path.ancestors().find_map(repository_at)
}

/// The common directory of a valid git directory, or none if it isn't one.
fn common_directory(git_directory: &Path) -> Option<PathBuf> {
    if !valid_head(&git_directory.join("HEAD")) {
        return None;
    }
    let commondir = git_directory.join("commondir");
    let common = if fs::symlink_metadata(&commondir).is_ok() {
        // Git fails on a commondir it can't read or that is empty.
        let contents = read_whole_file(&commondir, MAXIMUM_GITFILE_BYTES)?;
        if contents.is_empty() {
            return None;
        }
        let common = without_line_ending(&contents);
        git_directory.join(OsStr::from_bytes(common))
    } else {
        git_directory.to_path_buf()
    };
    (searchable(&common.join("objects")) && searchable(&common.join("refs"))).then_some(common)
}

/// Whether this user may search `path`, as git's `access(path, X_OK)`.
fn searchable(path: &Path) -> bool {
    let Ok(path) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    // SAFETY: `path` is a valid NUL-terminated string for the duration of the call.
    unsafe { libc::access(path.as_ptr(), libc::X_OK) == 0 }
}

/// A whole regular file of at most `limit` bytes, or none when it's longer:
/// git reads `.git` files and `commondir` in full.
fn read_whole_file(path: &Path, limit: u64) -> Option<Vec<u8>> {
    let contents = read_regular_file(path, limit + 1)?;
    (contents.len() as u64 <= limit).then_some(contents)
}

/// Up to `limit` bytes of a regular file. The open doesn't wait, so a FIFO
/// put in its place can't block the walk.
fn read_regular_file(path: &Path, limit: u64) -> Option<Vec<u8>> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .ok()
        .filter(|file| file.metadata().is_ok_and(|metadata| metadata.is_file()))?;
    let mut contents = Vec::new();
    file.take(limit).read_to_end(&mut contents).ok()?;
    Some(contents)
}

/// A HEAD git accepts: a symbolic link into `refs/`, `ref: refs/…`, or an
/// object name. The branch it names need not exist yet.
fn valid_head(head: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(head) else {
        return false;
    };
    if metadata.file_type().is_symlink() {
        return fs::read_link(head)
            .is_ok_and(|target| target.as_os_str().as_bytes().starts_with(b"refs/"));
    }
    let Some(contents) = read_regular_file(head, MAXIMUM_HEAD_BYTES) else {
        return false;
    };
    if let Some(reference) = contents.strip_prefix(b"ref:") {
        // Git's whitespace: space, tab, line feed and carriage return.
        let start = reference
            .iter()
            .position(|byte| !matches!(byte, b' ' | b'\t' | b'\n' | b'\r'))
            .unwrap_or(reference.len());
        return reference[start..].starts_with(b"refs/");
    }
    // Git parses an object name from the start and ignores what follows it.
    contents
        .iter()
        .take_while(|byte| byte.is_ascii_hexdigit())
        .count()
        >= 40
}

/// `bytes` without trailing line feeds and carriage returns, as git trims.
fn without_line_ending(bytes: &[u8]) -> &[u8] {
    let end = bytes
        .iter()
        .rposition(|byte| !matches!(byte, b'\n' | b'\r'))
        .map_or(0, |last| last + 1);
    &bytes[..end]
}

/// A real repository at `directory`, for tests that need one.
#[cfg(test)]
pub(crate) fn init_repository(directory: &Path) {
    let status = std::process::Command::new("git")
        .args(["init", "-q"])
        .current_dir(directory)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .status()
        .unwrap();
    assert!(status.success());
}

#[cfg(test)]
#[path = "git_repository_test.rs"]
mod tests;
