//! Answers "is this path git-ignored?" for single paths, with Git's rules:
//! deeper `.gitignore` files override shallower ones, which override
//! `.git/info/exclude`, which overrides the global excludes file; a path
//! beneath an ignored directory is ignored; and rules apply only inside a
//! repository. Recursive walks match with `walk_ignore`, which implements the
//! same rules; tests check both against `git` itself.

use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::SystemTime,
};

use crate::git_repository;
use ignore::{
    Match,
    gitignore::{Gitignore, GitignoreBuilder, gitconfig_excludes_path},
};

/// A file's identity for cache validation: modification time and length.
type Stamp = Option<(SystemTime, u64)>;

#[derive(Clone, Debug, PartialEq)]
struct Repository {
    root: PathBuf,
    exclude_file: Option<PathBuf>,
}

struct CachedMatcher {
    stamp: Stamp,
    matcher: Option<Arc<Gitignore>>,
}

#[derive(Default)]
struct Cache {
    /// By ignore file and the directory its patterns are anchored at: the
    /// global excludes file is anchored at each repository's top.
    matchers: HashMap<(PathBuf, PathBuf), CachedMatcher>,
    repositories: HashMap<PathBuf, Option<Repository>>,
}

pub struct IgnoreRules {
    /// Rules never climb above this directory: the repository containing the
    /// workspace, or the workspace itself outside a repository. An enclosing
    /// repository, such as a dotfiles repo in HOME, does not apply.
    top: PathBuf,
    global_excludes: Option<PathBuf>,
    cache: Mutex<Cache>,
}

/// The matchers that apply to entries of one directory, deepest first.
pub struct DirectoryRules {
    matchers: Vec<Arc<Gitignore>>,
    /// The directory itself is ignored, so every entry is.
    directory_ignored: bool,
    in_repository: bool,
}

impl DirectoryRules {
    pub fn ignores(&self, path: &Path, is_dir: bool) -> bool {
        if self.directory_ignored {
            return true;
        }
        if !self.in_repository || path.file_name().is_some_and(|name| name == ".git") {
            return false;
        }
        first_match(&self.matchers, path, is_dir)
    }
}

fn first_match(matchers: &[Arc<Gitignore>], path: &Path, is_dir: bool) -> bool {
    for matcher in matchers {
        match matcher.matched(path, is_dir) {
            Match::None => continue,
            Match::Ignore(_) => return true,
            Match::Whitelist(_) => return false,
        }
    }
    false
}

fn stamp(path: &Path) -> Stamp {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.modified().ok()?, metadata.len()))
}

/// The user's Git `core.excludesFile`, or its default location.
pub fn system_global_excludes() -> Option<PathBuf> {
    gitconfig_excludes_path()
}

impl IgnoreRules {
    pub fn new(workspace_root: &Path, global_excludes: Option<PathBuf>) -> Self {
        let rules = Self {
            top: workspace_root.to_path_buf(),
            global_excludes,
            cache: Mutex::new(Cache::default()),
        };
        let top = rules
            .repository_for(workspace_root)
            .map(|repository| repository.root)
            .unwrap_or_else(|| workspace_root.to_path_buf());
        Self { top, ..rules }
    }

    pub fn global_excludes(&self) -> Option<&Path> {
        self.global_excludes.as_deref()
    }

    /// The top level of the repository containing `workspace_root`, if any.
    pub fn repository_root(&self, workspace_root: &Path) -> Option<PathBuf> {
        self.repository_for(workspace_root)
            .map(|repository| repository.root)
    }

    /// Forgets cached repositories, for when a `.git` appears or disappears.
    pub fn forget_repositories(&self) {
        self.cache.lock().unwrap().repositories.clear();
    }

    fn repository_for(&self, directory: &Path) -> Option<Repository> {
        let mut cache = self.cache.lock().unwrap();
        let mut visited = Vec::new();
        let mut current = Some(directory);
        let mut found = None;
        while let Some(candidate) = current {
            if let Some(cached) = cache.repositories.get(candidate) {
                found = cached.clone();
                break;
            }
            visited.push(candidate.to_path_buf());
            if let Some(repository) = git_repository::repository_at(candidate) {
                found = Some(Repository {
                    root: repository.root,
                    exclude_file: Some(repository.common_directory.join("info/exclude")),
                });
                break;
            }
            current = candidate.parent();
        }
        for directory in visited {
            cache.repositories.insert(directory, found.clone());
        }
        found
    }

    fn matcher(&self, file: &Path, root: &Path) -> Option<Arc<Gitignore>> {
        let current = stamp(file);
        let mut cache = self.cache.lock().unwrap();
        let key = (file.to_path_buf(), root.to_path_buf());
        if let Some(cached) = cache.matchers.get(&key)
            && cached.stamp == current
        {
            return cached.matcher.clone();
        }
        let matcher = current.and_then(|_| {
            let mut builder = GitignoreBuilder::new(root);
            // A malformed line is skipped, as Git skips it.
            let _ = builder.add(file);
            builder.build().ok().map(Arc::new)
        });
        cache.matchers.insert(
            key,
            CachedMatcher {
                stamp: current,
                matcher: matcher.clone(),
            },
        );
        matcher
    }

    /// Matchers for entries of `directory` within `repository`, deepest first.
    fn matchers_for(&self, directory: &Path, repository: &Repository) -> Vec<Arc<Gitignore>> {
        let mut matchers = Vec::new();
        let mut current = Some(directory);
        while let Some(level) = current {
            if !level.starts_with(&repository.root) {
                break;
            }
            if let Some(matcher) = self.matcher(&level.join(".gitignore"), level) {
                matchers.push(matcher);
            }
            if level == repository.root {
                break;
            }
            current = level.parent();
        }
        if let Some(exclude) = &repository.exclude_file
            && let Some(matcher) = self.matcher(exclude, &repository.root)
        {
            matchers.push(matcher);
        }
        if let Some(global) = &self.global_excludes
            && let Some(matcher) = self.matcher(global, &repository.root)
        {
            matchers.push(matcher);
        }
        matchers
    }

    /// Whether `path` is ignored, including because an ancestor directory is.
    pub fn is_ignored(&self, path: &Path, is_dir: bool) -> bool {
        let mut current = path.to_path_buf();
        let mut current_is_dir = is_dir;
        while current != self.top && current.starts_with(&self.top) {
            let Some(parent) = current.parent() else {
                return false;
            };
            if let Some(repository) = self.repository_for(parent) {
                let matchers = self.matchers_for(parent, &repository);
                let is_git_directory = current.file_name().is_some_and(|name| name == ".git");
                if !is_git_directory && first_match(&matchers, &current, current_is_dir) {
                    return true;
                }
            }
            current = parent.to_path_buf();
            current_is_dir = true;
        }
        false
    }

    /// Rules for listing `directory`, resolved once for all of its entries.
    pub fn for_directory(&self, directory: &Path) -> DirectoryRules {
        let directory_ignored = self.is_ignored(directory, true);
        match self.repository_for(directory) {
            Some(repository) if directory.starts_with(&self.top) => DirectoryRules {
                matchers: self.matchers_for(directory, &repository),
                directory_ignored,
                in_repository: true,
            },
            _ => DirectoryRules {
                matchers: Vec::new(),
                directory_ignored,
                in_repository: false,
            },
        }
    }
}
