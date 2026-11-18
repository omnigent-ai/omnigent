//! Stat and directory listings, described the way the runner's file service
//! describes them so both routes agree.

use std::{
    fs::{self, Metadata},
    io,
    path::Path,
    time::UNIX_EPOCH,
};

use rayon::prelude::*;

use crate::{
    ignore_rules::{DirectoryRules, IgnoreRules},
    proto::{Entry, EntryType},
};

/// Whole seconds, truncated toward zero as Python's `int(st_mtime)` is.
fn modified_seconds(metadata: &Metadata) -> Option<i64> {
    let modified = metadata.modified().ok()?;
    Some(match modified.duration_since(UNIX_EPOCH) {
        Ok(after) => after.as_secs() as i64,
        Err(before) => -(before.duration().as_secs() as i64),
    })
}

/// A file's modification time, in seconds.
pub fn modify_time(metadata: &Metadata) -> Option<prost_types::Timestamp> {
    modified_seconds(metadata).map(|seconds| prost_types::Timestamp { seconds, nanos: 0 })
}

/// Describes `absolute`, following symlinks for kind and size. A broken
/// symlink is a file with no size and the link's own modification time.
/// Returns `None` when the path cannot be described at all.
pub fn describe(
    absolute: &Path,
    name: &str,
    path: &str,
    rules: Option<&DirectoryRules>,
) -> Option<Entry> {
    let link = fs::symlink_metadata(absolute).ok()?;
    let is_symlink = link.file_type().is_symlink();
    let (kind, size, modified) = match fs::metadata(absolute) {
        Ok(target) if target.is_dir() => (EntryType::Directory, None, modify_time(&target)),
        Ok(target) => (
            EntryType::File,
            Some(target.len() as i64),
            modify_time(&target),
        ),
        Err(_) => (EntryType::File, None, modify_time(&link)),
    };
    let ignored = rules.is_some_and(|rules| rules.ignores(absolute, kind == EntryType::Directory));
    Some(Entry {
        name: name.to_string(),
        path: path.to_string(),
        r#type: kind as i32,
        size,
        modify_time: modified,
        is_symlink,
        ignored,
        leaf_directory_path: String::new(),
    })
}

/// Chains longer than this stop early; the client lists the rest on expand.
const MAXIMUM_CHAIN: usize = 64;

/// Follows `directory` while its only entry is a real (non-symlink)
/// directory, returning the names traversed.
fn single_child_chain(
    ignore_rules: &IgnoreRules,
    directory: &Path,
    hide_ignored: bool,
) -> Vec<String> {
    let mut names = Vec::new();
    let mut current = directory.to_path_buf();
    while names.len() < MAXIMUM_CHAIN {
        let Ok(mut entries) = fs::read_dir(&current) else {
            break;
        };
        let (Some(Ok(only)), None) = (entries.next(), entries.next()) else {
            break;
        };
        let Ok(name) = only.file_name().into_string() else {
            break;
        };
        if !only.file_type().is_ok_and(|kind| kind.is_dir()) {
            break;
        }
        let child = current.join(&name);
        if hide_ignored && ignore_rules.is_ignored(&child, true) {
            break;
        }
        names.push(name);
        current = child;
    }
    names
}

/// Describes one path. `absolute` is the unresolved path, so a symlink is
/// reported as one.
pub fn stat(ignore_rules: &IgnoreRules, absolute: &Path, path: &str) -> io::Result<Entry> {
    let name = path.rsplit('/').next().unwrap_or_default();
    let rules = absolute
        .parent()
        .map(|parent| ignore_rules.for_directory(parent));
    describe(absolute, name, path, rules.as_ref())
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "The path no longer exists"))
}

/// Lists a directory's entries sorted by name. Entries whose names are not
/// UTF-8, or that vanish while listing, are skipped.
pub fn list(
    ignore_rules: &IgnoreRules,
    directory: &Path,
    path: &str,
    hide_ignored: bool,
    collapse_single_child_directories: bool,
) -> io::Result<Vec<Entry>> {
    let rules = ignore_rules.for_directory(directory);
    let mut names: Vec<String> = fs::read_dir(directory)?
        .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
        .collect();
    names.sort_unstable();
    let mut entries: Vec<Entry> = names
        .iter()
        .filter_map(|name| {
            let child = if path.is_empty() {
                name.clone()
            } else {
                format!("{path}/{name}")
            };
            describe(&directory.join(name), name, &child, Some(&rules))
        })
        .filter(|entry| !(hide_ignored && entry.ignored))
        .collect();
    if collapse_single_child_directories {
        // One read per subdirectory; in parallel, a large root stays fast.
        entries.par_iter_mut().for_each(|entry| {
            if entry.r#type == EntryType::Directory as i32 && !entry.is_symlink {
                let chain =
                    single_child_chain(ignore_rules, &directory.join(&entry.name), hide_ignored);
                if !chain.is_empty() {
                    entry.leaf_directory_path = format!("{}/{}", entry.path, chain.join("/"));
                }
            }
        });
    }
    Ok(entries)
}
