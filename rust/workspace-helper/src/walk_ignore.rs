//! Which entries a recursive walk ignores, decided here rather than by the
//! ignore crate's walker so the helper owns its rules. It matches the crate's
//! own walker (ignore 0.4.33, `Ignore::matched_ignore`): each kind of ignore
//! file takes its nearest match, then `.rgignore`, `.ignore`, `.gitignore`,
//! `info/exclude` and the global excludes decide in that order. Git's files
//! stop at the nearest repository git accepts or `.jj`, while `.ignore` and
//! `.rgignore` apply from every ancestor of the walk's start.

use std::{
    cell::RefCell,
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
};

use crate::git_repository::repository_at;
use ignore::{
    DirEntry, Match,
    gitignore::{Gitignore, GitignoreBuilder},
};

/// Tells walks apart in each thread's cached directory.
static NEXT_WALK: AtomicU64 = AtomicU64::new(0);

thread_local! {
    /// The directory whose entries this thread filtered last, by walk: a
    /// walker thread filters one directory's entries in a row.
    static LAST_DIRECTORY: RefCell<Option<(u64, PathBuf, Arc<Level>)>> = const { RefCell::new(None) };
}

/// The rules one directory contributes, linked to its parent's.
struct Level {
    directory: PathBuf,
    rgignore: Gitignore,
    ignore: Gitignore,
    gitignore: Gitignore,
    exclude: Gitignore,
    /// A repository git accepts, or holds `.jj`: Git's files above it don't
    /// apply below it.
    boundary: bool,
    /// This level or one above it is a boundary, so Git's files apply at all.
    within_boundary: bool,
    /// An ancestor of the start at or above this level has rules, so matching
    /// against the start's ancestors can't be skipped.
    rules_above: bool,
    /// An ancestor of a walk's start, matched against the start's canonical path.
    above_start: bool,
    /// A start's canonical path, for matching its ancestors' rules.
    canonical_start: Option<PathBuf>,
    /// The global excludes, anchored at the nearest repository's top.
    global: Option<Arc<Gitignore>>,
    parent: Option<Arc<Level>>,
}

pub struct WalkIgnore {
    walk: u64,
    ripgrep: bool,
    /// The global excludes file, when a repository's top is known to anchor it.
    global_excludes: Option<PathBuf>,
    /// Starts' repository, where the global excludes are anchored.
    repository: Option<PathBuf>,
    /// Each nested repository anchors the global excludes at its own top.
    nested_global_anchors: bool,
    /// Levels by directory: those within the walk, and those above its starts.
    levels: Mutex<[HashMap<PathBuf, Arc<Level>>; 2]>,
    shared_lookups: AtomicUsize,
}

impl WalkIgnore {
    /// Rules for one walk whose starts belong to `repository`.
    pub fn new(ripgrep: bool, global_excludes: Option<&Path>, repository: Option<&Path>) -> Self {
        Self {
            walk: NEXT_WALK.fetch_add(1, Ordering::Relaxed),
            ripgrep,
            global_excludes: global_excludes.map(Path::to_path_buf),
            repository: repository.map(Path::to_path_buf),
            nested_global_anchors: global_excludes.is_some(),
            levels: Mutex::default(),
            shared_lookups: AtomicUsize::new(0),
        }
    }

    /// How often the shared directory map was locked.
    pub fn shared_lookups(&self) -> usize {
        self.shared_lookups.load(Ordering::Relaxed)
    }

    /// Whether the walk skips `entry`, which is beneath one of its starts.
    pub fn ignores(&self, entry: &DirEntry) -> bool {
        let Some(directory) = entry.path().parent() else {
            return false;
        };
        let level = LAST_DIRECTORY.with_borrow(|last| match last {
            Some((walk, cached, level))
                if *walk == self.walk && cached.as_os_str() == directory.as_os_str() =>
            {
                Some(level.clone())
            }
            _ => None,
        });
        let level = level.unwrap_or_else(|| {
            let level = self.level(directory, entry.depth() - 1);
            LAST_DIRECTORY.set(Some((self.walk, directory.to_path_buf(), level.clone())));
            level
        });
        let is_dir = entry.file_type().is_some_and(|kind| kind.is_dir());
        ignored(&level, entry.path(), is_dir)
    }

    fn cached(&self, directory: &Path, above_start: bool) -> Option<Arc<Level>> {
        self.shared_lookups.fetch_add(1, Ordering::Relaxed);
        let levels = self.levels.lock().unwrap();
        levels[usize::from(above_start)].get(directory).cloned()
    }

    fn remember(&self, level: Level) -> Arc<Level> {
        let level = Arc::new(level);
        self.shared_lookups.fetch_add(1, Ordering::Relaxed);
        let mut levels = self.levels.lock().unwrap();
        levels[usize::from(level.above_start)]
            .entry(level.directory.clone())
            .or_insert(level)
            .clone()
    }

    /// The rules for entries of `directory`, `depth` levels below its start.
    fn level(&self, directory: &Path, depth: usize) -> Arc<Level> {
        if let Some(level) = self.cached(directory, false) {
            return level;
        }
        // Files are read outside the lock, so threads build levels in parallel.
        let level = if depth == 0 {
            let canonical = directory.canonicalize().ok();
            let parent = canonical.as_deref().and_then(|start| self.above(start));
            let global = self.global_anchored_at(self.repository.as_deref());
            Level {
                canonical_start: canonical,
                ..self.read(directory, false, global, parent)
            }
        } else {
            let parent = self.level(directory.parent().unwrap(), depth - 1);
            let global = if self.nested_global_anchors && repository_at(directory).is_some() {
                self.global_anchored_at(Some(directory))
            } else {
                parent.global.clone()
            };
            self.read(directory, false, global, Some(parent))
        };
        self.remember(level)
    }

    /// The rules of a start's ancestors, from its canonical path, nearest first.
    fn above(&self, start: &Path) -> Option<Arc<Level>> {
        let directory = start.parent()?;
        if let Some(level) = self.cached(directory, true) {
            return Some(level);
        }
        let parent = self.above(directory);
        Some(self.remember(self.read(directory, true, None, parent)))
    }

    fn global_anchored_at(&self, top: Option<&Path>) -> Option<Arc<Gitignore>> {
        let (file, top) = (self.global_excludes.as_deref()?, top?);
        let mut builder = GitignoreBuilder::new(top);
        let _ = builder.add(file);
        builder.build().ok().map(Arc::new)
    }

    fn read(
        &self,
        directory: &Path,
        above_start: bool,
        global: Option<Arc<Gitignore>>,
        parent: Option<Arc<Level>>,
    ) -> Level {
        // A `.git` git rejects is an ordinary entry, not a repository. Git
        // reads `info/exclude` from the common directory: a submodule's own git
        // directory, or a linked worktree's main repository.
        let repository = repository_at(directory);
        let exclude = repository
            .as_ref()
            .map(|repository| {
                ignore_file(directory, &repository.common_directory.join("info/exclude"))
            })
            .unwrap_or_else(Gitignore::empty);
        let optional = |name: &str| {
            if self.ripgrep {
                ignore_file(directory, &directory.join(name))
            } else {
                Gitignore::empty()
            }
        };
        let (rgignore, ignore) = (optional(".rgignore"), optional(".ignore"));
        let gitignore = ignore_file(directory, &directory.join(".gitignore"));
        let boundary = repository.is_some() || directory.join(".jj").exists();
        let has_rules = [&rgignore, &ignore, &gitignore, &exclude]
            .iter()
            .any(|matcher| !matcher.is_empty());
        let parent_level = parent.as_deref();
        Level {
            directory: directory.to_path_buf(),
            within_boundary: boundary || parent_level.is_some_and(|parent| parent.within_boundary),
            rules_above: above_start
                && (has_rules || parent_level.is_some_and(|parent| parent.rules_above)),
            rgignore,
            ignore,
            gitignore,
            exclude,
            boundary,
            above_start,
            canonical_start: None,
            global,
            parent,
        }
    }
}

/// An ignore file's rules anchored at `directory`, or none when it's absent.
fn ignore_file(directory: &Path, file: &Path) -> Gitignore {
    if !file.exists() {
        return Gitignore::empty();
    }
    let mut builder = GitignoreBuilder::new(directory);
    // A malformed line is skipped, as Git skips it.
    let _ = builder.add(file);
    builder.build().unwrap_or_else(|_| Gitignore::empty())
}

/// Whether the rules ignore `path`, an entry of `level`'s directory.
fn ignored(level: &Level, path: &Path, is_dir: bool) -> bool {
    let chain = || std::iter::successors(Some(level), |level| level.parent.as_deref());
    let any_boundary = level.within_boundary;
    let (mut rgignore, mut ignore, mut gitignore, mut exclude) =
        (Match::None, Match::None, Match::None, Match::None);
    let mut past_boundary = false;
    let mut start: Option<&Level> = None;
    let mut from_start: Option<PathBuf> = None;
    for level in chain() {
        let candidate = if level.above_start {
            if !level.rules_above {
                break;
            }
            let Some(start) = start else { break };
            let Some(canonical) = &start.canonical_start else {
                break;
            };
            from_start
                .get_or_insert_with(|| {
                    canonical.join(path.strip_prefix(&start.directory).unwrap_or(path))
                })
                .as_path()
        } else {
            start = Some(level);
            path
        };
        if rgignore.is_none() {
            rgignore = level.rgignore.matched(candidate, is_dir);
        }
        if ignore.is_none() {
            ignore = level.ignore.matched(candidate, is_dir);
        }
        if any_boundary && !past_boundary {
            if gitignore.is_none() {
                gitignore = level.gitignore.matched(candidate, is_dir);
            }
            if exclude.is_none() {
                exclude = level.exclude.matched(candidate, is_dir);
            }
        }
        past_boundary = past_boundary || level.boundary;
    }
    let global = level
        .global
        .as_deref()
        .map_or(Match::None, |global| global.matched(path, is_dir));
    rgignore
        .or(ignore)
        .or(gitignore)
        .or(exclude)
        .or(global)
        .is_ignore()
}

#[cfg(test)]
#[path = "walk_ignore_test.rs"]
mod tests;
