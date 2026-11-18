use std::collections::VecDeque;

use tokio::sync::oneshot;

use super::*;

type Answer = Result<Vec<u8>, GitError>;

/// A lister whose Git calls wait for the test to answer them, one at a time.
#[derive(Default)]
struct FakeLister {
    repository: AtomicBool,
    stamp: Mutex<Option<IndexStamp>>,
    tracked: Mutex<VecDeque<oneshot::Sender<Answer>>>,
    untracked: Mutex<VecDeque<oneshot::Sender<Answer>>>,
    walk_paths: Mutex<Vec<&'static str>>,
    /// When set, a walk waits for a message on it.
    walk_gate: Mutex<Option<std::sync::mpsc::Receiver<()>>>,
    walks: std::sync::atomic::AtomicUsize,
    discoveries: std::sync::atomic::AtomicUsize,
}

impl FakeLister {
    fn git() -> Arc<Self> {
        let lister = Self::default();
        lister.repository.store(true, Ordering::Relaxed);
        lister.set_stamp(1);
        Arc::new(lister)
    }

    fn set_stamp(&self, size: u64) {
        *self.stamp.lock().unwrap() = Some(IndexStamp {
            modified_nanoseconds: 0,
            size,
            inode: 1,
        });
    }

    async fn waiting(queue: &Mutex<VecDeque<oneshot::Sender<Answer>>>) -> Answer {
        let (sender, receiver) = oneshot::channel();
        queue.lock().unwrap().push_back(sender);
        receiver.await.unwrap_or(Err(GitError::Unavailable))
    }

    /// Answers the oldest waiting call in `queue`, once it is waiting.
    async fn answer(queue: &Mutex<VecDeque<oneshot::Sender<Answer>>>, answer: Answer) {
        loop {
            if let Some(sender) = queue.lock().unwrap().pop_front() {
                let _ = sender.send(answer);
                return;
            }
            tokio::task::yield_now().await;
        }
    }

    async fn answer_tracked(&self, paths: &[&str]) {
        Self::answer(&self.tracked, Ok(nul(paths))).await;
    }

    async fn answer_untracked(&self, answer: Answer) {
        Self::answer(&self.untracked, answer).await;
    }

    fn untracked_waiting(&self) -> usize {
        self.untracked.lock().unwrap().len()
    }
}

fn nul(paths: &[&str]) -> Vec<u8> {
    paths
        .iter()
        .flat_map(|path| [path.as_bytes(), b"\0"].concat())
        .collect()
}

#[tonic::async_trait]
impl FileLister for FakeLister {
    async fn discover_repository(&self) -> bool {
        self.discoveries.fetch_add(1, Ordering::Relaxed);
        self.repository.load(Ordering::Relaxed)
    }

    fn tracked_stamp(&self) -> Option<IndexStamp> {
        *self.stamp.lock().unwrap()
    }

    async fn list_tracked(&self) -> Answer {
        Self::waiting(&self.tracked).await
    }

    async fn list_untracked(&self) -> Answer {
        Self::waiting(&self.untracked).await
    }

    fn walk(&self, _include_ignored: bool, limits: &WalkLimits) -> Walked {
        self.walks.fetch_add(1, Ordering::Relaxed);
        if let Some(gate) = self.walk_gate.lock().unwrap().take() {
            let _ = gate.recv();
        }
        let paths = self.walk_paths.lock().unwrap().clone();
        let capped = paths.len() > limits.maximum;
        Walked {
            index: FileIndex::from_files(paths.into_iter().take(limits.maximum)),
            capped,
        }
    }
}

fn files(generation: &Generation) -> Vec<String> {
    generation
        .index
        .paths()
        .filter(|entry| !entry.is_dir)
        .map(|entry| entry.path.to_string())
        .collect()
}

/// Waits until the slot has no refresh running or wanted.
async fn settled(slot: &IndexSlot) {
    loop {
        {
            let state = slot.state.lock().unwrap();
            if !state.running
                && !state.reload_wanted
                && !state.build_wanted
                && !state.untracked_running
                && !state.untracked_wanted
            {
                return;
            }
        }
        tokio::task::yield_now().await;
    }
}

/// A Git slot whose first list is `tracked` and whose first untracked pass found `untracked`.
async fn loaded(lister: &Arc<FakeLister>, tracked: &[&str], untracked: &[&str]) -> Arc<IndexSlot> {
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    lister.answer_tracked(tracked).await;
    first.await.unwrap().unwrap();
    lister.answer_untracked(Ok(nul(untracked))).await;
    settled(&slot).await;
    slot
}

#[tokio::test(start_paused = true)]
async fn the_first_search_waits_for_tracked_files_then_untracked_files_follow() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    lister.answer_tracked(&["src/a.rs"]).await;
    let tracked_only = first.await.unwrap().unwrap();
    assert_eq!(files(&tracked_only), ["src/a.rs"]);
    assert!(slot.refreshing(&tracked_only), "an untracked pass follows");

    lister.answer_untracked(Ok(nul(&["new.rs"]))).await;
    settled(&slot).await;
    assert!(
        slot.refreshing(&tracked_only),
        "a newer generation is published"
    );
    let complete = slot.generation(false).await.unwrap();
    assert_eq!(files(&complete), ["new.rs", "src/a.rs"]);
    assert!(!slot.refreshing(&complete));
    assert!(!complete.untracked_unavailable);
}

#[tokio::test(start_paused = true)]
async fn a_long_pass_is_not_followed_by_another_until_its_cooldown_ends() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    lister.answer_tracked(&["a"]).await;
    first.await.unwrap();
    // The pass outlasts the cooldown, as a cold walk of a large checkout does.
    tokio::time::advance(Duration::from_secs(202)).await;
    lister.answer_untracked(Ok(nul(&["b"]))).await;
    settled(&slot).await;

    let polled = slot.generation(false).await.unwrap();
    assert!(
        !slot.refreshing(&polled),
        "the poll that follows completion settles"
    );
    assert_eq!(lister.untracked_waiting(), 0, "no successor pass");

    tokio::time::advance(REFRESH_COOLDOWN - Duration::from_secs(1)).await;
    slot.generation(false).await.unwrap();
    assert_eq!(lister.untracked_waiting(), 0);

    tokio::time::advance(Duration::from_secs(1)).await;
    let due = slot.generation(false).await.unwrap();
    assert!(slot.refreshing(&due));
    lister.answer_untracked(Ok(nul(&["b"]))).await;
    settled(&slot).await;
}

#[tokio::test(start_paused = true)]
async fn a_timed_out_pass_reports_untracked_files_unavailable_and_keeps_the_previous_ones() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &["old-untracked"]).await;

    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    tokio::time::advance(Duration::from_secs(300)).await;
    lister.answer_untracked(Err(GitError::Timeout)).await;
    settled(&slot).await;

    let failed = slot.generation(false).await.unwrap();
    assert!(failed.untracked_unavailable);
    assert!(
        !slot.refreshing(&failed),
        "a failure is terminal until a retry"
    );
    assert_eq!(files(&failed), ["a", "old-untracked"]);
    assert_eq!(
        lister.untracked_waiting(),
        0,
        "no successor scan after a timeout"
    );

    // An explicit retry skips the cooldown and recovers.
    let retried = slot.generation(true).await.unwrap();
    assert!(slot.refreshing(&retried));
    lister.answer_untracked(Ok(nul(&["fresh"]))).await;
    settled(&slot).await;
    let recovered = slot.generation(false).await.unwrap();
    assert!(!recovered.untracked_unavailable);
    assert_eq!(files(&recovered), ["a", "fresh"]);
}

#[tokio::test(start_paused = true)]
async fn a_pass_that_finds_nothing_new_keeps_the_published_index() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &["u"]).await;
    let before = slot.generation(false).await.unwrap();
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    lister.answer_untracked(Ok(nul(&["u"]))).await;
    settled(&slot).await;
    let after = slot.generation(false).await.unwrap();
    assert!(
        Arc::ptr_eq(&before, &after),
        "no rebuild and no new generation"
    );
    assert!(!slot.refreshing(&after));

    // After a failure, the same set still clears the flag.
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    lister.answer_untracked(Err(GitError::Timeout)).await;
    settled(&slot).await;
    assert!(slot.generation(false).await.unwrap().untracked_unavailable);
    slot.generation(true).await.unwrap();
    lister.answer_untracked(Ok(nul(&["u"]))).await;
    settled(&slot).await;
    let recovered = slot.generation(false).await.unwrap();
    assert!(!recovered.untracked_unavailable);
    assert!(Arc::ptr_eq(&recovered.index, &after.index));
}

#[tokio::test(start_paused = true)]
async fn a_retry_while_a_pass_runs_starts_no_second_pass() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &[]).await;
    slot.generation(true).await.unwrap();
    tokio::task::yield_now().await;
    slot.generation(true).await.unwrap();
    for _ in 0..10 {
        tokio::task::yield_now().await;
    }
    assert_eq!(lister.untracked_waiting(), 1);
    lister.answer_untracked(Ok(Vec::new())).await;
    settled(&slot).await;
}

#[tokio::test(start_paused = true)]
async fn an_index_change_reloads_while_searches_keep_the_previous_list() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &[]).await;

    lister.set_stamp(2);
    let during = slot.generation(false).await.unwrap();
    assert_eq!(files(&during), ["a"]);
    assert!(slot.refreshing(&during));
    lister.answer_tracked(&["a", "added"]).await;
    // The tracked set changed, so untracked discovery runs again at once.
    lister.answer_untracked(Ok(Vec::new())).await;
    settled(&slot).await;
    let after = slot.generation(false).await.unwrap();
    assert_eq!(files(&after), ["a", "added"]);
    assert!(!slot.refreshing(&after));
}

#[tokio::test(start_paused = true)]
async fn an_index_rewrite_with_the_same_paths_leaves_untracked_discovery_on_cooldown() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &[]).await;
    lister.set_stamp(2);
    slot.generation(false).await.unwrap();
    lister.answer_tracked(&["a"]).await;
    settled(&slot).await;
    assert_eq!(lister.untracked_waiting(), 0);
}

#[tokio::test(start_paused = true)]
async fn a_reload_publishes_while_a_long_pass_is_still_running() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &[]).await;
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    while lister.untracked_waiting() == 0 {
        tokio::task::yield_now().await;
    }
    // While that pass runs, a checkout rewrites the index.
    lister.set_stamp(2);
    slot.generation(false).await.unwrap();
    lister.answer_tracked(&["b"]).await;
    loop {
        let current = slot.generation(false).await.unwrap();
        if files(&current) == ["b"] {
            assert!(slot.refreshing(&current), "the old pass is still running");
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(lister.untracked_waiting(), 1, "the old pass is still held");

    // The old pass merges with the new tracked list, then a fresh pass follows.
    lister.answer_untracked(Ok(nul(&["stale-untracked"]))).await;
    lister.answer_untracked(Ok(nul(&["fresh-untracked"]))).await;
    settled(&slot).await;
    let current = slot.generation(false).await.unwrap();
    assert_eq!(files(&current), ["b", "fresh-untracked"]);
}

#[tokio::test(start_paused = true)]
async fn preparing_lists_tracked_files_and_leaves_untracked_discovery_to_a_search() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare();
    lister.answer_tracked(&["a"]).await;
    settled(&slot).await;
    assert_eq!(lister.untracked_waiting(), 0);
    assert!(slot.state.lock().unwrap().current.is_some());

    let searched = slot.generation(false).await.unwrap();
    assert_eq!(files(&searched), ["a"]);
    assert!(slot.refreshing(&searched));
    lister.answer_untracked(Ok(nul(&["new"]))).await;
    settled(&slot).await;
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["a", "new"]);
}

#[tokio::test(start_paused = true)]
async fn preparing_ahead_of_a_search_outside_git_leaves_the_walk_to_the_search() {
    let lister = Arc::new(FakeLister::default());
    *lister.walk_paths.lock().unwrap() = vec!["one"];
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare_tracked();
    settled(&slot).await;
    assert_eq!(
        lister.walks.load(Ordering::Relaxed),
        0,
        "nothing walked ahead"
    );
    assert!(slot.state.lock().unwrap().current.is_none());

    let searched = slot.generation(false).await.unwrap();
    assert_eq!(files(&searched), ["one"]);
    assert_eq!(lister.walks.load(Ordering::Relaxed), 1);
}

#[tokio::test(start_paused = true)]
async fn preparing_ahead_of_a_search_walks_nothing_when_git_fails_until_a_search_asks() {
    let lister = FakeLister::git();
    *lister.walk_paths.lock().unwrap() = vec!["walked"];
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare_tracked();
    FakeLister::answer(&lister.tracked, Err(GitError::Unavailable)).await;
    settled(&slot).await;
    assert_eq!(
        lister.walks.load(Ordering::Relaxed),
        0,
        "nothing walked ahead"
    );

    // The search lists again, and walks once Git fails for it too.
    let search = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    FakeLister::answer(&lister.tracked, Err(GitError::Unavailable)).await;
    assert_eq!(files(&search.await.unwrap().unwrap()), ["walked"]);
    assert_eq!(lister.walks.load(Ordering::Relaxed), 1);
}

#[tokio::test(start_paused = true)]
async fn a_search_that_arrives_while_git_fails_ahead_of_it_still_gets_its_walk() {
    let lister = FakeLister::git();
    *lister.walk_paths.lock().unwrap() = vec!["walked"];
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare_tracked();
    // The search comes while the tracked list is still running, then Git fails.
    let search = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    loop {
        if lister.tracked.lock().unwrap().len() == 1 && !slot.state.lock().unwrap().walk_deferred {
            break;
        }
        tokio::task::yield_now().await;
    }
    FakeLister::answer(&lister.tracked, Err(GitError::Unavailable)).await;
    assert_eq!(files(&search.await.unwrap().unwrap()), ["walked"]);
    assert_eq!(lister.walks.load(Ordering::Relaxed), 1);
}

#[tokio::test(start_paused = true)]
async fn a_later_reload_whose_git_fails_still_walks_after_a_list_prepared_ahead() {
    let lister = FakeLister::git();
    *lister.walk_paths.lock().unwrap() = vec!["walked"];
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare_tracked();
    lister.answer_tracked(&["a"]).await;
    settled(&slot).await;
    // The first search uses the prepared list and starts untracked discovery.
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["a"]);
    lister.answer_untracked(Ok(nul(&[]))).await;
    settled(&slot).await;

    // The index changes, and the reload a search starts finds Git failing.
    lister.set_stamp(2);
    slot.generation(false).await.unwrap();
    FakeLister::answer(&lister.tracked, Err(GitError::Unavailable)).await;
    settled(&slot).await;
    assert_eq!(
        lister.walks.load(Ordering::Relaxed),
        1,
        "walked on that attempt"
    );
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["walked"]);
}

#[tokio::test(start_paused = true)]
async fn preparing_ahead_of_a_search_in_git_lists_tracked_files_once() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    slot.prepare_tracked();
    lister.answer_tracked(&["a"]).await;
    settled(&slot).await;
    let searched = slot.generation(false).await.unwrap();
    assert_eq!(files(&searched), ["a"]);
    assert_eq!(lister.walks.load(Ordering::Relaxed), 0);
    assert_eq!(
        lister.tracked.lock().unwrap().len(),
        0,
        "no second tracked list"
    );
}

#[tokio::test(start_paused = true)]
async fn a_search_that_ranked_an_older_generation_is_told_to_ask_again() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    lister.answer_tracked(&["a"]).await;
    // A search took the tracked-only list, then the pass published and finished.
    let searched = first.await.unwrap().unwrap();
    lister.answer_untracked(Ok(nul(&["new"]))).await;
    settled(&slot).await;
    assert!(slot.refreshing(&searched));
    let next = slot.generation(false).await.unwrap();
    assert_eq!(files(&next), ["a", "new"]);
    assert!(!slot.refreshing(&next));
}

#[tokio::test(start_paused = true)]
async fn outside_git_the_capped_walk_rebuilds_after_its_cooldown_and_finds_git_once_it_exists() {
    let lister = Arc::new(FakeLister::default());
    *lister.walk_paths.lock().unwrap() = vec!["one"];
    let slot = IndexSlot::new(false, lister.clone());
    let walked = slot.generation(false).await.unwrap();
    assert_eq!(files(&walked), ["one"]);
    assert!(!walked.incomplete);
    settled(&slot).await;

    *lister.walk_paths.lock().unwrap() = vec!["one", "renamed"];
    tokio::time::advance(REFRESH_COOLDOWN - Duration::from_secs(1)).await;
    slot.generation(false).await.unwrap();
    settled(&slot).await;
    assert_eq!(lister.walks.load(Ordering::Relaxed), 1);
    tokio::time::advance(Duration::from_secs(1)).await;
    let stale = slot.generation(false).await.unwrap();
    assert!(slot.refreshing(&stale));
    settled(&slot).await;
    let rebuilt = slot.generation(false).await.unwrap();
    assert_eq!(files(&rebuilt), ["one", "renamed"]);

    // `git init` happened: the next rebuild lists with Git.
    lister.repository.store(true, Ordering::Relaxed);
    lister.set_stamp(1);
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    lister.answer_tracked(&["tracked"]).await;
    settled(&slot).await;
    slot.generation(false).await.unwrap();
    lister.answer_untracked(Ok(Vec::new())).await;
    settled(&slot).await;
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["tracked"]);
}

#[tokio::test(start_paused = true)]
async fn an_untracked_result_during_a_fallback_walk_never_replaces_the_walk() {
    let lister = FakeLister::git();
    let slot = loaded(&lister, &["a"], &[]).await;
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    while lister.untracked_waiting() == 0 {
        tokio::task::yield_now().await;
    }
    // The index changes, Git then fails, and the slot walks instead, held.
    *lister.walk_paths.lock().unwrap() = vec!["walked"];
    let (release_walk, gate) = std::sync::mpsc::channel();
    *lister.walk_gate.lock().unwrap() = Some(gate);
    lister.set_stamp(2);
    slot.generation(false).await.unwrap();
    FakeLister::answer(&lister.tracked, Err(GitError::Failed("broken".into()))).await;
    while lister.walks.load(Ordering::Relaxed) == 0 {
        tokio::task::yield_now().await;
    }
    // Meanwhile the pass finds a new untracked file.
    lister.answer_untracked(Ok(nul(&["u"]))).await;
    while slot.state.lock().unwrap().untracked_running {
        tokio::task::yield_now().await;
    }
    assert!(
        slot.state.lock().unwrap().build_wanted,
        "the pass queued a Git build"
    );
    release_walk.send(()).unwrap();
    settled(&slot).await;
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["walked"]);
}

#[tokio::test(start_paused = true)]
async fn finding_git_replaces_the_walk_even_with_no_tracked_files() {
    let lister = Arc::new(FakeLister::default());
    *lister.walk_paths.lock().unwrap() = vec!["old"];
    let slot = IndexSlot::new(false, lister.clone());
    assert_eq!(files(&slot.generation(false).await.unwrap()), ["old"]);
    settled(&slot).await;

    // `git init` with nothing added; the walked file is now untracked but ignored.
    lister.repository.store(true, Ordering::Relaxed);
    lister.set_stamp(1);
    tokio::time::advance(REFRESH_COOLDOWN).await;
    slot.generation(false).await.unwrap();
    lister.answer_tracked(&[]).await;
    settled(&slot).await;
    let listed = slot.generation(false).await.unwrap();
    assert!(files(&listed).is_empty(), "{:?}", files(&listed));
    lister.answer_untracked(Ok(Vec::new())).await;
    settled(&slot).await;
    assert!(files(&slot.generation(false).await.unwrap()).is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_waiter_held_after_the_first_list_never_blocks_a_publication() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    let (reached, paused) = std::sync::mpsc::channel();
    let (release, held) = std::sync::mpsc::channel();
    *slot.first_list_pause.lock().unwrap() = Some((reached, held));
    let searches: Vec<_> = (0..2)
        .map(|_| {
            let slot = slot.clone();
            tokio::spawn(async move { slot.generation(false).await })
        })
        .collect();
    lister.answer_tracked(&["a"]).await;
    tokio::task::spawn_blocking(move || paused.recv().unwrap())
        .await
        .unwrap();
    // The other search starts untracked discovery; its failure publishes a status.
    while lister.untracked_waiting() == 0 {
        tokio::task::yield_now().await;
    }
    lister.answer_untracked(Err(GitError::Timeout)).await;
    let published = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(state) = slot.state.try_lock()
                && !state.untracked_running
                && state.untracked_failed
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    });
    assert!(
        published.await.is_ok(),
        "the publication was blocked by the held waiter"
    );
    release.send(()).unwrap();
    for search in searches {
        let found = tokio::time::timeout(Duration::from_secs(5), search)
            .await
            .expect("the held search finishes");
        assert!(found.unwrap().is_some());
    }
}

#[tokio::test(start_paused = true)]
async fn a_git_failure_falls_back_to_the_walk() {
    let lister = FakeLister::git();
    *lister.walk_paths.lock().unwrap() = vec!["walked"];
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    FakeLister::answer(&lister.tracked, Err(GitError::Failed("broken".into()))).await;
    assert_eq!(files(&first.await.unwrap().unwrap()), ["walked"]);
}

#[tokio::test(start_paused = true)]
async fn a_capped_walk_marks_its_generation_incomplete() {
    let lister = Arc::new(FakeLister::default());
    let many: Vec<&'static str> = (0..=WALK_MAXIMUM)
        .map(|number| &*Box::leak(format!("f{number}").into_boxed_str()))
        .collect();
    *lister.walk_paths.lock().unwrap() = many;
    let slot = IndexSlot::new(true, lister.clone());
    let walked = slot.generation(false).await.unwrap();
    assert!(walked.incomplete);
    assert_eq!(
        lister.discoveries.load(Ordering::Relaxed),
        0,
        "ignored files never use Git"
    );
}

#[tokio::test(start_paused = true)]
async fn shutting_down_during_a_load_publishes_nothing_and_releases_waiters() {
    let lister = FakeLister::git();
    let slot = IndexSlot::new(false, lister.clone());
    let first = tokio::spawn({
        let slot = slot.clone();
        async move { slot.generation(false).await }
    });
    while lister.tracked.lock().unwrap().is_empty() {
        tokio::task::yield_now().await;
    }
    slot.shutdown();
    assert!(first.await.unwrap().is_none());
    // The aborted job dropped its Git call, which kills a real Git child.
    let sender = lister.tracked.lock().unwrap().pop_front().unwrap();
    for _ in 0..100 {
        if sender.is_closed() {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert!(sender.is_closed());
    assert!(slot.state.lock().unwrap().current.is_none());
    assert!(slot.generation(false).await.is_none());
}
