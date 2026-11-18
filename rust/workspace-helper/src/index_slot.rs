//! A workspace's searchable file list, without watching the disk. In a Git
//! repository it is Git's tracked files, plus untracked files from a slower
//! pass run in the background; elsewhere, and for ignored files, it is a
//! capped walk. Searches start refreshes and keep answering from the last
//! published list while one runs.

use std::{
    os::unix::fs::MetadataExt,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use tokio::{sync::watch, task::AbortHandle, time::Instant};

use crate::{
    file_index::{FileIndex, WalkLimits, Walked},
    git_runner::{CallLimits, GitCommand, GitError, GitRepository},
    ignore_rules::IgnoreRules,
    walk::IgnoreMode,
};

/// How long after an untracked pass or a walk finishes before a search starts another.
pub const REFRESH_COOLDOWN: Duration = Duration::from_secs(60);
/// A walk keeps at most this many entries, as large trees outside Git would otherwise take minutes.
pub const WALK_MAXIMUM: usize = 500_000;
const DISCOVER_DEADLINE: Duration = Duration::from_secs(10);
const TRACKED_DEADLINE: Duration = Duration::from_secs(60);
/// Untracked discovery walks the worktree, which takes minutes on a cold, large checkout.
const UNTRACKED_DEADLINE: Duration = Duration::from_secs(5 * 60);
const TRACKED_OUTPUT_BYTES: usize = 1 << 30;
const UNTRACKED_OUTPUT_BYTES: usize = 256 << 20;

/// Identifies one version of the Git index file, so a commit, checkout, or
/// `git add` is noticed with one `stat`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IndexStamp {
    modified_nanoseconds: i128,
    size: u64,
    inode: u64,
}

impl IndexStamp {
    fn of(metadata: &std::fs::Metadata) -> Self {
        Self {
            modified_nanoseconds: metadata.mtime() as i128 * 1_000_000_000
                + metadata.mtime_nsec() as i128,
            size: metadata.size(),
            inode: metadata.ino(),
        }
    }
}

/// Where a slot's paths come from. The workspace's real implementation runs
/// Git and walks; tests substitute their own.
#[tonic::async_trait]
pub trait FileLister: Send + Sync + 'static {
    /// Looks for the repository again; false outside one Git can read.
    async fn discover_repository(&self) -> bool;
    /// The Git index file's current stamp, when there is a repository.
    fn tracked_stamp(&self) -> Option<IndexStamp>;
    /// Tracked paths, NUL-separated.
    async fn list_tracked(&self) -> Result<Vec<u8>, GitError>;
    /// Untracked, unignored paths, NUL-separated.
    async fn list_untracked(&self) -> Result<Vec<u8>, GitError>;
    /// Walks the tree; runs on a blocking thread.
    fn walk(&self, include_ignored: bool, limits: &WalkLimits) -> Walked;
}

/// One published list. A search uses one generation from start to finish.
#[derive(Debug)]
pub struct Generation {
    pub id: u64,
    pub index: Arc<FileIndex>,
    /// A walk stopped at [`WALK_MAXIMUM`], so some paths are missing.
    pub incomplete: bool,
    /// The last untracked pass failed, so new files not yet in Git may be missing.
    pub untracked_unavailable: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Source {
    Unknown,
    Git,
    Walk,
}

enum Step {
    Reload,
    Build,
}

struct State {
    source: Source,
    current: Option<Arc<Generation>>,
    tracked: Arc<Vec<Box<str>>>,
    tracked_stamp: Option<IndexStamp>,
    /// The stamp a running reload read before listing, so searches meanwhile don't ask again.
    reloading_stamp: Option<IndexStamp>,
    untracked: Arc<Vec<Box<str>>>,
    untracked_failed: bool,
    /// Bumped whenever `tracked` or `untracked` changes, so a build from older lists publishes nothing.
    paths_version: u64,
    /// The reload-and-build job, which never waits for an untracked pass.
    running: bool,
    reload_wanted: bool,
    build_wanted: bool,
    /// The untracked pass runs on its own, beside reloads.
    untracked_running: bool,
    untracked_wanted: bool,
    untracked_finished: Option<Instant>,
    walk_finished: Option<Instant>,
    /// Opened ahead of any search: a walk instead of Git waits for one.
    walk_deferred: bool,
    /// A reload skipped its walk for that, so the first search reloads again.
    walk_skipped: bool,
    next_id: u64,
    job: Option<AbortHandle>,
    untracked_job: Option<AbortHandle>,
}

/// One workspace's list for one ignored-files choice.
pub struct IndexSlot {
    include_ignored: bool,
    lister: Arc<dyn FileLister>,
    cancelled: Arc<AtomicBool>,
    state: Mutex<State>,
    published: watch::Sender<Option<Arc<Generation>>>,
    /// Holds the next search that waited for the first list, after it reads that list.
    #[cfg(test)]
    first_list_pause: Mutex<Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
}

fn parse_paths(bytes: &[u8]) -> Vec<Box<str>> {
    bytes
        .split(|&byte| byte == 0)
        .filter(|path| !path.is_empty())
        .map(|path| String::from_utf8_lossy(path).into())
        .collect()
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    tokio::task::spawn_blocking(work).await.ok()
}

impl IndexSlot {
    pub fn new(include_ignored: bool, lister: Arc<dyn FileLister>) -> Arc<Self> {
        Arc::new(Self {
            include_ignored,
            lister,
            cancelled: Arc::new(AtomicBool::new(false)),
            state: Mutex::new(State {
                source: Source::Unknown,
                current: None,
                tracked: Arc::default(),
                tracked_stamp: None,
                reloading_stamp: None,
                untracked: Arc::default(),
                untracked_failed: false,
                paths_version: 0,
                running: false,
                reload_wanted: false,
                build_wanted: false,
                untracked_running: false,
                untracked_wanted: false,
                untracked_finished: None,
                walk_finished: None,
                walk_deferred: false,
                walk_skipped: false,
                next_id: 1,
                job: None,
                untracked_job: None,
            }),
            published: watch::channel(None).0,
            #[cfg(test)]
            first_list_pause: Mutex::default(),
        })
    }

    /// Starts the first list without looking for untracked files, so opening
    /// a workspace never pays for that walk; the first search does.
    pub fn prepare(self: &Arc<Self>) {
        self.prepare_list(false);
    }

    /// Lists only Git's tracked files, ahead of any search: outside Git, or
    /// when Git fails, the walk waits for the first search.
    pub fn prepare_tracked(self: &Arc<Self>) {
        self.prepare_list(true);
    }

    fn prepare_list(self: &Arc<Self>, tracked_only: bool) {
        let mut state = self.state.lock().unwrap();
        if state.current.is_none() && !state.running && !self.cancelled.load(Ordering::Relaxed) {
            state.walk_deferred = tracked_only;
            state.reload_wanted = true;
            self.start_due(&mut state);
        }
    }

    /// The generation to search now, starting whatever refresh is due. Waits
    /// only for the first list; `None` once the workspace has closed.
    pub async fn generation(self: &Arc<Self>, retry_untracked: bool) -> Option<Arc<Generation>> {
        let stamp = (!self.include_ignored)
            .then(|| self.lister.tracked_stamp())
            .flatten();
        let mut published = self.published.subscribe();
        {
            let mut state = self.state.lock().unwrap();
            if self.cancelled.load(Ordering::Relaxed) {
                return None;
            }
            // A search may walk, whatever an open ahead of it deferred.
            state.walk_deferred = false;
            if state.current.is_none() {
                // One whose first list skipped its walk lists again.
                if std::mem::take(&mut state.walk_skipped) {
                    state.reload_wanted = true;
                }
                // Later searches during the first load wait for it rather than queue another.
                state.reload_wanted |= !state.running;
            } else {
                self.request_due(&mut state, stamp, retry_untracked);
            }
            self.start_due(&mut state);
            if let Some(current) = &state.current {
                return Some(current.clone());
            }
        }
        loop {
            if self.cancelled.load(Ordering::Relaxed) {
                return None;
            }
            // Released before taking the state lock, which `publish` holds while it sends.
            let first = published.borrow_and_update().clone();
            if let Some(current) = first {
                #[cfg(test)]
                self.pause_after_first_list();
                // The search that waited for the first list also starts untracked discovery.
                let mut state = self.state.lock().unwrap();
                self.request_due(&mut state, stamp, retry_untracked);
                self.start_due(&mut state);
                return Some(current);
            }
            published.changed().await.ok()?;
        }
    }

    #[cfg(test)]
    fn pause_after_first_list(&self) {
        let pause = self.first_list_pause.lock().unwrap().take();
        if let Some((reached, release)) = pause {
            let _ = reached.send(());
            let _ = release.recv();
        }
    }

    /// Marks what a search finds due: a reload after the index changed, an
    /// untracked pass after its cooldown or on a retry, or a walk after its cooldown.
    fn request_due(&self, state: &mut State, stamp: Option<IndexStamp>, retry_untracked: bool) {
        let now = Instant::now();
        let cooled = |finished: Option<Instant>| {
            finished.is_none_or(|at| now.duration_since(at) >= REFRESH_COOLDOWN)
        };
        match state.source {
            Source::Git => {
                if stamp != state.tracked_stamp
                    && (!state.running || stamp != state.reloading_stamp)
                {
                    state.reload_wanted = true;
                }
                if !state.untracked_running && (retry_untracked || cooled(state.untracked_finished))
                {
                    state.untracked_wanted = true;
                }
            }
            Source::Walk => {
                if !state.running && cooled(state.walk_finished) {
                    state.reload_wanted = true;
                }
            }
            Source::Unknown => {}
        }
    }

    /// Starts the reload job and the untracked pass when they are wanted and idle.
    fn start_due(self: &Arc<Self>, state: &mut State) {
        if self.cancelled.load(Ordering::Relaxed) {
            return;
        }
        if !state.running && (state.reload_wanted || state.build_wanted) {
            state.running = true;
            state.job = Some(tokio::spawn(self.clone().run_jobs()).abort_handle());
        }
        if !state.untracked_running && state.untracked_wanted && state.source == Source::Git {
            state.untracked_wanted = false;
            state.untracked_running = true;
            state.untracked_job = Some(tokio::spawn(self.clone().untracked_pass()).abort_handle());
        }
    }

    /// Whether a newer list than `searched` is published or on its way, so
    /// the caller should ask again.
    pub fn refreshing(&self, searched: &Generation) -> bool {
        let state = self.state.lock().unwrap();
        state.running
            || state.reload_wanted
            || state.build_wanted
            || state.untracked_running
            || state.untracked_wanted
            || state.current.as_ref().map(|current| current.id) != Some(searched.id)
    }

    /// Stops every refresh; nothing is published afterwards.
    pub fn shutdown(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
        let jobs = {
            let mut state = self.state.lock().unwrap();
            [state.job.take(), state.untracked_job.take()]
        };
        for job in jobs.into_iter().flatten() {
            job.abort();
        }
        // Wakes searches waiting for a first list.
        self.published.send_modify(|_| {});
    }

    fn publish(&self, state: &mut State, index: Arc<FileIndex>, incomplete: bool) {
        if self.cancelled.load(Ordering::Relaxed) {
            return;
        }
        let generation = Arc::new(Generation {
            id: state.next_id,
            index,
            incomplete,
            untracked_unavailable: state.untracked_failed,
        });
        state.next_id += 1;
        state.current = Some(generation.clone());
        self.published.send_replace(Some(generation));
    }

    /// Republishes the current paths with a changed untracked status.
    fn publish_status(&self, state: &mut State) {
        if let Some(current) = state.current.clone() {
            self.publish(state, current.index.clone(), current.incomplete);
        }
    }

    async fn run_jobs(self: Arc<Self>) {
        loop {
            let step = {
                let mut state = self.state.lock().unwrap();
                if self.cancelled.load(Ordering::Relaxed) {
                    state.running = false;
                    return;
                }
                // A built list publishes before another reload starts.
                if state.build_wanted {
                    state.build_wanted = false;
                    Step::Build
                } else if state.reload_wanted {
                    state.reload_wanted = false;
                    Step::Reload
                } else {
                    state.running = false;
                    state.job = None;
                    return;
                }
            };
            match step {
                Step::Reload => self.reload().await,
                Step::Build => self.build().await,
            }
        }
    }

    async fn reload(self: &Arc<Self>) {
        let source = self.state.lock().unwrap().source;
        let use_git = !self.include_ignored
            && (source == Source::Git || self.lister.discover_repository().await);
        if use_git && self.reload_tracked().await {
            return;
        }
        {
            // Decided under the lock a search takes, so one arriving now still gets its walk.
            let mut state = self.state.lock().unwrap();
            if std::mem::take(&mut state.walk_deferred) {
                state.walk_skipped = true;
                return;
            }
        }
        self.reload_walk().await;
    }

    /// Lists tracked files, leaving the build to the next step. False when
    /// Git failed, so the caller walks instead.
    async fn reload_tracked(self: &Arc<Self>) -> bool {
        // Stamped before listing, so a change during the listing reloads again.
        let stamp = self.lister.tracked_stamp();
        self.state.lock().unwrap().reloading_stamp = stamp;
        let listed = match self.lister.list_tracked().await {
            Ok(listed) => listed,
            Err(error) => {
                eprintln!(
                    "workspace-helper: listing tracked files failed, walking instead: {error}"
                );
                return false;
            }
        };
        let previous = self.state.lock().unwrap().tracked.clone();
        let Some(tracked) = blocking(move || parse_paths(&listed)).await else {
            return true;
        };
        let mut state = self.state.lock().unwrap();
        // Only the first list was opened ahead; later reloads walk when Git fails.
        state.walk_deferred = false;
        // The published list came from a walk until now, so replace it even when the paths match.
        let switched = state.source != Source::Git;
        state.source = Source::Git;
        state.tracked_stamp = stamp;
        if *previous != tracked || switched || state.current.is_none() {
            state.tracked = Arc::new(tracked);
            state.paths_version += 1;
            state.build_wanted = true;
            // Paths moved between tracked and untracked; look again once a
            // search has looked before. An index rewrite that changed no
            // paths, as `git status` refreshing stat data does, doesn't.
            if state.untracked_finished.is_some() || state.untracked_running {
                state.untracked_wanted = true;
            }
        }
        self.start_due(&mut state);
        true
    }

    /// Builds and publishes the index from the current lists, unless either
    /// changed meanwhile; then the next build does.
    async fn build(&self) {
        let (tracked, untracked, version) = {
            let state = self.state.lock().unwrap();
            (
                state.tracked.clone(),
                state.untracked.clone(),
                state.paths_version,
            )
        };
        let Some(index) = blocking(move || {
            FileIndex::from_files(
                tracked
                    .iter()
                    .chain(untracked.iter())
                    .map(|path| path.as_ref()),
            )
        })
        .await
        else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        // A walk published since, and owns the list now.
        if state.source != Source::Git {
            return;
        }
        if state.paths_version != version {
            state.build_wanted = true;
            return;
        }
        self.publish(&mut state, Arc::new(index), false);
    }

    async fn reload_walk(&self) {
        let lister = self.lister.clone();
        let cancelled = self.cancelled.clone();
        let include_ignored = self.include_ignored;
        let Some(walked) = blocking(move || {
            lister.walk(
                include_ignored,
                &WalkLimits {
                    cancelled: &cancelled,
                    maximum: WALK_MAXIMUM,
                },
            )
        })
        .await
        else {
            return;
        };
        let mut state = self.state.lock().unwrap();
        state.source = Source::Walk;
        state.paths_version += 1;
        state.walk_finished = Some(Instant::now());
        state.tracked = Arc::default();
        state.tracked_stamp = None;
        state.untracked = Arc::default();
        state.untracked_failed = false;
        state.untracked_wanted = false;
        // A build queued from Git's lists before this walk would replace it.
        state.build_wanted = false;
        self.publish(&mut state, Arc::new(walked.index), walked.capped);
    }

    /// Lists untracked files and merges them with whatever tracked list is
    /// current when it finishes, so a reload meanwhile never waits for it.
    async fn untracked_pass(self: Arc<Self>) {
        let result = self.lister.list_untracked().await;
        let parsed = match result {
            Ok(listed) => blocking(move || parse_paths(&listed)).await.ok_or(None),
            Err(error) => Err(Some(error)),
        };
        let mut state = self.state.lock().unwrap();
        state.untracked_running = false;
        state.untracked_job = None;
        state.untracked_finished = Some(Instant::now());
        if self.cancelled.load(Ordering::Relaxed) || state.source != Source::Git {
            return;
        }
        match parsed {
            Ok(untracked) if *state.untracked == untracked => {
                // Nothing new, the usual case: keep the published index rather than rebuild it.
                if std::mem::replace(&mut state.untracked_failed, false) {
                    self.publish_status(&mut state);
                }
            }
            Ok(untracked) => {
                state.untracked = Arc::new(untracked);
                state.untracked_failed = false;
                state.paths_version += 1;
                state.build_wanted = true;
            }
            Err(error) => {
                if let Some(error) = error {
                    eprintln!("workspace-helper: listing untracked files failed: {error}");
                }
                // The previous untracked files stay listed, marked as possibly incomplete.
                if !std::mem::replace(&mut state.untracked_failed, true) {
                    self.publish_status(&mut state);
                }
            }
        }
        self.start_due(&mut state);
    }
}

/// Lists a workspace's paths with Git, or by walking it.
pub struct WorkspaceFiles {
    root: PathBuf,
    rules: Arc<IgnoreRules>,
    repository: Mutex<Option<Arc<GitRepository>>>,
}

impl WorkspaceFiles {
    pub fn new(root: PathBuf, rules: Arc<IgnoreRules>) -> Self {
        Self {
            root,
            rules,
            repository: Mutex::default(),
        }
    }

    fn repository(&self) -> Option<Arc<GitRepository>> {
        self.repository.lock().unwrap().clone()
    }

    async fn run(
        &self,
        command: GitCommand,
        deadline: Duration,
        stdout_bytes: usize,
    ) -> Result<Vec<u8>, GitError> {
        let repository = self.repository().ok_or(GitError::Unavailable)?;
        let limits = CallLimits {
            deadline: Instant::now() + deadline,
            stdout_bytes,
        };
        repository.run(&command, None, limits).await
    }
}

#[tonic::async_trait]
impl FileLister for WorkspaceFiles {
    async fn discover_repository(&self) -> bool {
        let found = GitRepository::discover(&self.root, Instant::now() + DISCOVER_DEADLINE)
            .await
            .ok()
            .flatten()
            .map(Arc::new);
        let usable = found.is_some();
        let had_repository =
            std::mem::replace(&mut *self.repository.lock().unwrap(), found).is_some();
        if had_repository != usable {
            // A `.git` appeared or went away, so cached ignore rules are stale.
            self.rules.forget_repositories();
        }
        usable
    }

    fn tracked_stamp(&self) -> Option<IndexStamp> {
        let repository = self.repository()?;
        std::fs::metadata(repository.git_directory.join("index"))
            .ok()
            .map(|metadata| IndexStamp::of(&metadata))
    }

    async fn list_tracked(&self) -> Result<Vec<u8>, GitError> {
        self.run(
            GitCommand::ListTracked,
            TRACKED_DEADLINE,
            TRACKED_OUTPUT_BYTES,
        )
        .await
    }

    async fn list_untracked(&self) -> Result<Vec<u8>, GitError> {
        let repository = self.repository().ok_or(GitError::Unavailable)?;
        let limits = CallLimits {
            deadline: Instant::now() + UNTRACKED_DEADLINE,
            stdout_bytes: UNTRACKED_OUTPUT_BYTES,
        };
        // A long scan of the worktree mustn't hold the slots interactive requests use.
        repository
            .run_in_background(&GitCommand::ListUntracked, limits)
            .await
    }

    fn walk(&self, include_ignored: bool, limits: &WalkLimits) -> Walked {
        let mode = if include_ignored {
            IgnoreMode::None
        } else {
            IgnoreMode::Git
        };
        FileIndex::walk(&self.root, mode, &self.rules, limits)
    }
}

#[cfg(test)]
#[path = "index_slot_test.rs"]
mod tests;
