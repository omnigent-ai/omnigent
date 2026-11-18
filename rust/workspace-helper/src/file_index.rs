//! An in-memory index of a workspace's paths for fuzzy and substring file
//! search, built from Git's file list or a parallel walk, so a query scans
//! memory rather than the disk.

use std::{
    cmp::Reverse,
    collections::BinaryHeap,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
};

use ignore::WalkState;
use nucleo_matcher::{
    Config, Matcher, Utf32Str,
    pattern::{AtomKind, CaseMatching, Normalization, Pattern},
};
use rayon::prelude::*;

use crate::{
    globs::PathFilter,
    ignore_rules::IgnoreRules,
    walk::{IgnoreMode, WalkOptions, walk_parallel},
};

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct IndexedPath {
    pub path: Box<str>,
    pub is_dir: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SearchHit {
    /// Relative to the search scope.
    pub path: String,
    pub is_dir: bool,
    pub score: u32,
    pub matched_indices: Vec<u32>,
}

#[derive(Debug, Default)]
pub struct FileIndex {
    /// Sorted by path, by byte value.
    entries: Vec<IndexedPath>,
}

const BATCH: usize = 4096;

/// Stops a walk early: when `cancelled` is set, or once it has kept `maximum` entries.
pub struct WalkLimits<'a> {
    pub cancelled: &'a AtomicBool,
    pub maximum: usize,
}

/// A walk's entries, and whether it stopped at its maximum.
pub struct Walked {
    pub index: FileIndex,
    pub capped: bool,
}

/// Buffers one walker thread's entries and flushes them on drop.
struct Collector {
    buffer: Vec<IndexedPath>,
    sender: mpsc::Sender<Vec<IndexedPath>>,
}

impl Drop for Collector {
    fn drop(&mut self) {
        if !self.buffer.is_empty() {
            let _ = self.sender.send(std::mem::take(&mut self.buffer));
        }
    }
}

/// Walks `start` (the root or a directory beneath it) and returns its
/// entries' paths relative to `root`, excluding `start` itself unless it is
/// beneath the root.
fn walk_paths(
    root: &Path,
    start: &Path,
    mode: IgnoreMode,
    global_excludes: Option<&Path>,
    limits: &WalkLimits,
) -> (Vec<IndexedPath>, bool) {
    let (sender, receiver) = mpsc::channel::<Vec<IndexedPath>>();
    let kept = AtomicUsize::new(0);
    let capped = AtomicBool::new(false);
    walk_parallel(
        &[start.to_path_buf()],
        &WalkOptions::new(mode, global_excludes),
        || {
            let mut collector = Collector {
                buffer: Vec::with_capacity(BATCH),
                sender: sender.clone(),
            };
            let kept = &kept;
            let capped = &capped;
            Box::new(move |entry| {
                if limits.cancelled.load(Ordering::Relaxed) {
                    return WalkState::Quit;
                }
                let Ok(entry) = entry else {
                    return WalkState::Continue;
                };
                let Ok(relative) = entry.path().strip_prefix(root) else {
                    return WalkState::Continue;
                };
                let Some(relative) = relative.to_str().filter(|path| !path.is_empty()) else {
                    return WalkState::Continue;
                };
                let is_dir = match entry.file_type() {
                    Some(kind) if kind.is_symlink() => entry.path().is_dir(),
                    Some(kind) => kind.is_dir(),
                    None => false,
                };
                if kept.fetch_add(1, Ordering::Relaxed) >= limits.maximum {
                    capped.store(true, Ordering::Relaxed);
                    return WalkState::Quit;
                }
                collector.buffer.push(IndexedPath {
                    path: relative.into(),
                    is_dir,
                });
                if collector.buffer.len() >= BATCH {
                    let batch = std::mem::replace(&mut collector.buffer, Vec::with_capacity(BATCH));
                    let _ = collector.sender.send(batch);
                }
                WalkState::Continue
            })
        },
    );
    drop(sender);
    let mut entries: Vec<IndexedPath> = receiver.into_iter().flatten().collect();
    entries.par_sort_unstable();
    entries.dedup();
    (entries, capped.into_inner())
}

/// Workspace-relative directories a search is limited to and kept out of.
#[derive(Clone, Debug, Default)]
pub struct DirectoryScopes {
    pub include: Vec<String>,
    pub exclude: Vec<String>,
}

fn beneath(path: &str, directory: &str) -> bool {
    directory.is_empty()
        || (path.starts_with(directory)
            && (path.len() == directory.len() || path.as_bytes()[directory.len()] == b'/'))
}

impl DirectoryScopes {
    pub fn admits(&self, path: &str) -> bool {
        (self.include.is_empty()
            || self
                .include
                .iter()
                .any(|directory| beneath(path, directory)))
            && !self
                .exclude
                .iter()
                .any(|directory| beneath(path, directory))
    }
}

/// Ranks better hits first: higher score, then shorter path, then path order.
type RankKey = (u32, Reverse<usize>, Reverse<usize>);

/// The last component of a "/"-separated relative path.
fn basename(path: &str) -> &str {
    path.rfind('/').map_or(path, |slash| &path[slash + 1..])
}

impl FileIndex {
    /// Walks the whole tree; for tests and tools that want every entry.
    pub fn build(root: &Path, mode: IgnoreMode, rules: &IgnoreRules) -> Self {
        let unlimited = WalkLimits {
            cancelled: &AtomicBool::new(false),
            maximum: usize::MAX,
        };
        Self::walk(root, mode, rules, &unlimited).index
    }

    /// Walks the tree until it ends or `limits` stops it.
    pub fn walk(root: &Path, mode: IgnoreMode, rules: &IgnoreRules, limits: &WalkLimits) -> Walked {
        let (entries, capped) = walk_paths(root, root, mode, rules.global_excludes(), limits);
        Walked {
            index: Self { entries },
            capped,
        }
    }

    /// An index over workspace-relative file paths, as Git lists them, with
    /// every directory above them. A path ending in `/` (a nested repository)
    /// is skipped.
    pub fn from_files<'a>(files: impl IntoIterator<Item = &'a str>) -> Self {
        let mut entries = Vec::new();
        let mut directories = std::collections::HashSet::new();
        for file in files {
            if file.is_empty() || file.ends_with('/') {
                continue;
            }
            let mut end = file.len();
            while let Some(slash) = file[..end].rfind('/') {
                if !directories.insert(&file[..slash]) {
                    break;
                }
                end = slash;
            }
            entries.push(IndexedPath {
                path: file.into(),
                is_dir: false,
            });
        }
        entries.extend(directories.into_iter().map(|directory| IndexedPath {
            path: directory.into(),
            is_dir: true,
        }));
        Self::from_paths(entries)
    }

    /// An index over already-enumerated paths.
    pub fn from_paths(mut entries: Vec<IndexedPath>) -> Self {
        entries.par_sort_unstable();
        entries.dedup();
        Self { entries }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn paths(&self) -> impl Iterator<Item = &IndexedPath> {
        self.entries.iter()
    }

    /// Entries beneath `scope` and within `directories`, with their
    /// scope-relative paths.
    fn scoped<'a>(
        &'a self,
        scope: &'a str,
        directories: &'a DirectoryScopes,
    ) -> impl Iterator<Item = (usize, &'a str, bool)> + 'a {
        let (start, prefix_length) = if scope.is_empty() {
            (0, 0)
        } else {
            let prefix = format!("{scope}/");
            (
                self.entries
                    .partition_point(|entry| entry.path.as_ref() < prefix.as_str()),
                prefix.len(),
            )
        };
        self.entries[start..]
            .iter()
            .enumerate()
            .take_while(move |(_, entry)| {
                scope.is_empty()
                    || (entry.path.len() > scope.len()
                        && entry.path.starts_with(scope)
                        && entry.path.as_bytes()[scope.len()] == b'/')
            })
            .filter(move |(_, entry)| directories.admits(&entry.path))
            .map(move |(offset, entry)| {
                (start + offset, &entry.path[prefix_length..], entry.is_dir)
            })
    }

    /// Case-insensitive substring search over files and directories, sorted
    /// by path, as the runner's file search returns them. A directory whose
    /// own path does not contain the query is not returned.
    pub fn search_substring(
        &self,
        query: &str,
        scope: &str,
        filter: &PathFilter,
        directories: &DirectoryScopes,
        limit: usize,
    ) -> (Vec<SearchHit>, bool) {
        let needle = query.trim();
        if needle.is_empty() || limit == 0 {
            return (Vec::new(), false);
        }
        let matcher = regex::RegexBuilder::new(&regex::escape(needle))
            .case_insensitive(true)
            .build()
            .expect("an escaped literal is a valid regex");
        let candidates: Vec<(usize, &str, bool)> = self.scoped(scope, directories).collect();
        // Indexed parallel iterators keep path order.
        let matched: Vec<(&str, bool)> = candidates
            .par_iter()
            .filter(|(_, path, _)| matcher.is_match(path) && filter.keeps(path))
            .map(|&(_, path, is_dir)| (path, is_dir))
            .collect();
        let truncated = matched.len() > limit;
        let hits = matched
            .into_iter()
            .take(limit)
            .map(|(path, is_dir)| SearchHit {
                path: path.to_string(),
                is_dir,
                score: 0,
                matched_indices: Vec::new(),
            })
            .collect();
        (hits, truncated)
    }

    /// Ranked fuzzy search over files, best first.
    pub fn search_fuzzy(
        &self,
        query: &str,
        scope: &str,
        filter: &PathFilter,
        directories: &DirectoryScopes,
        limit: usize,
    ) -> (Vec<SearchHit>, bool) {
        if query.trim().is_empty() || limit == 0 {
            return (Vec::new(), false);
        }
        let pattern = Pattern::new(
            query,
            CaseMatching::Smart,
            Normalization::Smart,
            AtomKind::Fuzzy,
        );
        let candidates: Vec<(usize, &str)> = self
            .scoped(scope, directories)
            .filter(|(_, _, is_dir)| !is_dir)
            .map(|(index, path, _)| (index, path))
            .collect();
        let (heap, matched) = candidates
            .par_chunks(8192)
            .map(|chunk| {
                let mut matcher = Matcher::new(Config::DEFAULT.match_paths());
                let mut buffer = Vec::new();
                let mut heap: BinaryHeap<Reverse<RankKey>> = BinaryHeap::with_capacity(limit + 1);
                let mut matched = 0usize;
                for &(index, path) in chunk {
                    let Some(score) = pattern.score(Utf32Str::new(path, &mut buffer), &mut matcher)
                    else {
                        continue;
                    };
                    if !filter.keeps(path) {
                        continue;
                    }
                    // A query the file name matches on its own outranks one spread over directories.
                    let score = score.saturating_add(
                        pattern
                            .score(Utf32Str::new(basename(path), &mut buffer), &mut matcher)
                            .unwrap_or(0),
                    );
                    matched += 1;
                    heap.push(Reverse((score, Reverse(path.len()), Reverse(index))));
                    if heap.len() > limit {
                        heap.pop();
                    }
                }
                (heap, matched)
            })
            .reduce(
                || (BinaryHeap::new(), 0),
                |(mut left, left_matched), (right, right_matched)| {
                    for key in right {
                        left.push(key);
                        if left.len() > limit {
                            left.pop();
                        }
                    }
                    (left, left_matched + right_matched)
                },
            );
        let mut ranked: Vec<RankKey> = heap.into_iter().map(|Reverse(key)| key).collect();
        ranked.sort_unstable_by(|a, b| b.cmp(a));
        let prefix_length = if scope.is_empty() { 0 } else { scope.len() + 1 };
        let mut matcher = Matcher::new(Config::DEFAULT.match_paths());
        let mut buffer = Vec::new();
        let hits = ranked
            .into_iter()
            .map(|(score, _, Reverse(index))| {
                let path = &self.entries[index].path[prefix_length..];
                let name = basename(path);
                let name_start = path[..path.len() - name.len()].chars().count() as u32;
                let mut matched_indices = Vec::new();
                // Highlight the file name when it matches alone, as it was ranked.
                if pattern
                    .indices(
                        Utf32Str::new(name, &mut buffer),
                        &mut matcher,
                        &mut matched_indices,
                    )
                    .is_some()
                {
                    for matched in &mut matched_indices {
                        *matched += name_start;
                    }
                } else {
                    matched_indices.clear();
                    pattern.indices(
                        Utf32Str::new(path, &mut buffer),
                        &mut matcher,
                        &mut matched_indices,
                    );
                }
                matched_indices.sort_unstable();
                matched_indices.dedup();
                SearchHit {
                    path: path.to_string(),
                    is_dir: false,
                    score,
                    matched_indices,
                }
            })
            .collect();
        (hits, matched > limit)
    }
}

#[cfg(test)]
#[path = "file_index_test.rs"]
mod tests;
