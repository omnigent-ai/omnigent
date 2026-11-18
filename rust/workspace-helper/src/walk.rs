//! Parallel recursive walks, skipping what `walk_ignore` ignores.

use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use ignore::{DirEntry, WalkBuilder, WalkState, overrides::Override};

use crate::{git_repository, walk_ignore::WalkIgnore};

/// Which ignore files a walk honours.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum IgnoreMode {
    /// `.gitignore`, `.git/info/exclude`, and global excludes, as Git does.
    Git,
    /// Git's files plus `.ignore` and `.rgignore`, as ripgrep does, so text
    /// search matches the runner's `rg` search.
    Ripgrep,
    /// Nothing is ignored.
    None,
}

#[derive(Clone, Default)]
pub struct WalkOptions {
    pub mode: Option<IgnoreMode>,
    /// Git's `core.excludesFile`, lowest in precedence and anchored at each
    /// repository's top level.
    pub global_excludes: Option<PathBuf>,
    pub overrides: Option<Override>,
    /// Absolute directories whose subtrees are not walked.
    pub excluded_directories: Arc<HashSet<PathBuf>>,
    /// When set, the walk enters only these absolute paths (besides its
    /// starts), so it reaches a few targets through their ancestors under the
    /// same ignore rules as a full walk.
    pub only_paths: Option<Arc<HashSet<PathBuf>>>,
}

impl WalkOptions {
    pub fn new(mode: IgnoreMode, global_excludes: Option<&Path>) -> Self {
        Self {
            mode: Some(mode),
            global_excludes: global_excludes.map(Path::to_path_buf),
            ..Self::default()
        }
    }

    fn mode(&self) -> IgnoreMode {
        self.mode.unwrap_or(IgnoreMode::Git)
    }

    /// Global excludes that apply, when the file has rules.
    fn active_global_excludes(&self) -> Option<&Path> {
        if self.mode() == IgnoreMode::None {
            return None;
        }
        self.global_excludes
            .as_deref()
            .filter(|path| std::fs::metadata(path).is_ok_and(|metadata| metadata.len() > 0))
    }
}

/// The top level of the nearest repository at or above `path`.
pub fn repository_root(path: &Path) -> Option<PathBuf> {
    git_repository::discover(path).map(|repository| repository.root)
}

/// One walk's builder: hidden files included, `.git` and excluded
/// directories pruned, and ignored entries skipped by `WalkIgnore`, with the
/// global excludes anchored at `repository`. The crate's own ignore files
/// are all off; it applies only the overrides.
fn builder(starts: &[PathBuf], repository: Option<&Path>, options: &WalkOptions) -> WalkBuilder {
    let mode = options.mode();
    let mut builder = WalkBuilder::new(&starts[0]);
    for start in &starts[1..] {
        builder.add(start);
    }
    builder
        .hidden(false)
        .follow_links(false)
        .parents(false)
        .ignore(false)
        .git_ignore(false)
        .git_exclude(false)
        .git_global(false);
    if let Some(overrides) = &options.overrides {
        builder.overrides(overrides.clone());
    }
    let rules = (mode != IgnoreMode::None).then(|| {
        WalkIgnore::new(
            mode == IgnoreMode::Ripgrep,
            options.active_global_excludes(),
            repository,
        )
    });
    let overrides = options.overrides.clone();
    let excluded = options.excluded_directories.clone();
    let only = options.only_paths.clone();
    builder.filter_entry(move |entry| {
        entry.file_name() != ".git"
            && (excluded.is_empty() || !excluded.contains(entry.path()))
            && only.as_ref().is_none_or(|only| only.contains(entry.path()))
            && rules.as_ref().is_none_or(|rules| {
                // An override's whitelist outranks every ignore file, as in rg.
                let is_dir = entry.file_type().is_some_and(|kind| kind.is_dir());
                overrides
                    .as_ref()
                    .is_some_and(|overrides| overrides.matched(entry.path(), is_dir).is_whitelist())
                    || !rules.ignores(entry)
            })
    });
    builder
}

/// Walks `starts` in parallel, calling a visitor from `make_visitor` on each
/// thread for every entry (the starts included). A visitor returning `Quit`
/// ends every walk.
pub fn walk_parallel<'a, M>(starts: &[PathBuf], options: &WalkOptions, make_visitor: M)
where
    M: Fn() -> Box<dyn FnMut(Result<DirEntry, ignore::Error>) -> WalkState + Send + 'a> + Sync,
{
    let quit = AtomicBool::new(false);
    // The global excludes are anchored at the repository that owns each
    // start, so starts are walked in groups by that repository.
    let mut groups: Vec<(Vec<PathBuf>, Option<PathBuf>)> = Vec::new();
    for start in starts {
        let repository = repository_root(start);
        match groups.iter_mut().find(|(_, owner)| *owner == repository) {
            Some((group, _)) => group.push(start.clone()),
            None => groups.push((vec![start.clone()], repository)),
        }
    }
    for (group, repository) in groups.iter().rev() {
        if quit.load(Ordering::Relaxed) {
            return;
        }
        builder(group, repository.as_deref(), options)
            .build_parallel()
            .run(|| {
                let mut visit = make_visitor();
                let quit = &quit;
                Box::new(move |entry| {
                    if quit.load(Ordering::Relaxed) {
                        return WalkState::Quit;
                    }
                    match visit(entry) {
                        WalkState::Quit => {
                            quit.store(true, Ordering::Relaxed);
                            WalkState::Quit
                        }
                        state => state,
                    }
                })
            });
    }
}
