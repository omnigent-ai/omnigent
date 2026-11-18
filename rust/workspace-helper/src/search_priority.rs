//! Which directories a priority text search looks at before the rest: those
//! with the most changes against a comparison base, from the working copy's
//! own change list, remembered briefly per workspace.

use std::{
    path::Path,
    sync::Mutex,
    time::{Duration, SystemTime},
};

use tokio::time::Instant;

use crate::{
    text_search::weighted_change_directories,
    working_copy::{self, ComparisonKind},
};

/// How long a workspace's changed directories are reused while nothing moved.
const REUSE_FOR: Duration = Duration::from_secs(30);

/// What the changed directories were computed from: the comparison, its base
/// and head commits, and when Git's index last changed.
#[derive(Clone, Debug, PartialEq)]
struct ChangesKey {
    kind: ComparisonKind,
    base: String,
    head: String,
    index_modified: Option<SystemTime>,
}

/// One workspace's most recent changed directories.
#[derive(Default)]
pub struct ChangedDirectoriesCache {
    latest: Mutex<Option<(ChangesKey, std::time::Instant, Vec<String>)>>,
}

impl ChangedDirectoriesCache {
    fn reuse(&self, key: &ChangesKey) -> Option<Vec<String>> {
        let latest = self.latest.lock().unwrap();
        let (cached, at, directories) = latest.as_ref()?;
        (cached == key && at.elapsed() < REUSE_FOR).then(|| directories.clone())
    }

    fn remember(&self, key: ChangesKey, directories: &[String]) {
        *self.latest.lock().unwrap() = Some((key, std::time::Instant::now(), directories.to_vec()));
    }
}

/// The workspace's changed directories against `kind`, heaviest first, or
/// `None` when Git can't say by `deadline` (no repository, no commits, or a
/// slow or failed listing). Dropping the future stops any Git it started.
pub async fn changed_directories(
    workspace_root: &Path,
    kind: ComparisonKind,
    cache: &ChangedDirectoriesCache,
    deadline: Instant,
) -> Option<Vec<String>> {
    let repository = working_copy::open_repository(workspace_root, deadline)
        .await
        .ok()?;
    let base = working_copy::resolve_base(&repository, kind, deadline)
        .await
        .ok()?;
    let key = ChangesKey {
        kind,
        base: base.base.clone(),
        head: base.head.clone(),
        index_modified: std::fs::metadata(repository.git_directory.join("index"))
            .and_then(|metadata| metadata.modified())
            .ok(),
    };
    if let Some(directories) = cache.reuse(&key) {
        return Some(directories);
    }
    let changes = working_copy::list_changes(&repository, base, deadline)
        .await
        .ok()?;
    let weighted: Vec<(String, u64)> = changes
        .entries
        .iter()
        .map(|entry| {
            let lines = entry.lines_added.unwrap_or(0) + entry.lines_removed.unwrap_or(0);
            (entry.path.clone(), lines)
        })
        .collect();
    let directories = weighted_change_directories(&weighted);
    cache.remember(key, &directories);
    Some(directories)
}
