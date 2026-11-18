use super::*;

#[test]
fn preview_counts_utf16_units_and_marks_cuts() {
    let line = "a😀b needle c\n".as_bytes();
    let start = "a😀b ".len();
    let (column, text, match_start, match_end) = preview(line, start, start + "needle".len());
    assert_eq!(text, "a😀b needle c");
    // The emoji is two UTF-16 units.
    assert_eq!(column, 6);
    assert_eq!((match_start, match_end), (5, 11));

    let long = format!("{}needle{}", "x".repeat(100), "y".repeat(500));
    let (column, text, match_start, match_end) = preview(long.as_bytes(), 100, 106);
    assert_eq!(column, 101);
    assert!(text.starts_with('…') && text.ends_with('…'));
    assert_eq!(text.chars().count(), 402);
    assert_eq!((match_start, match_end), (81, 87));
}

#[test]
fn directory_scopes_start_at_includes_and_drop_excluded_starts() {
    let root = Path::new("/workspace");
    let (starts, excluded) = directory_scopes(root, &[], &["build".into()]);
    assert_eq!(starts, vec![root.to_path_buf()]);
    assert!(excluded.contains(&root.join("build")));
    let (starts, _) =
        directory_scopes(root, &["sql".into(), "build/gen".into()], &["build".into()]);
    assert_eq!(starts, vec![root.join("sql")]);
    // Overlapping and duplicate includes become disjoint starts.
    let (starts, _) = directory_scopes(
        root,
        &["src/sub".into(), "src".into(), "src".into(), "srcx".into()],
        &[],
    );
    assert_eq!(starts, vec![root.join("src"), root.join("srcx")]);
}

#[test]
fn validation_matches_the_runner() {
    let query = |text: &str| TextQuery {
        query: text.into(),
        limit: 10,
        ..TextQuery::default()
    };
    assert!(validate(&query("ok")).is_ok());
    assert!(validate(&query("  ")).is_err());
    assert!(validate(&query("a\nb")).is_err());
    assert!(
        validate(&TextQuery {
            limit: 501,
            ..query("x")
        })
        .is_err()
    );
    assert!(
        validate(&TextQuery {
            include: "!*.rs".into(),
            ..query("x")
        })
        .is_err()
    );
}

/// A workspace of `files`, each holding "needle".
fn workspace(files: &[&str]) -> tempfile::TempDir {
    let directory = tempfile::tempdir().unwrap();
    for file in files {
        let path = directory.path().join(file);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "needle\n").unwrap();
    }
    directory
}

fn needle(limit: usize) -> TextQuery {
    TextQuery {
        query: "needle".into(),
        limit,
        ..TextQuery::default()
    }
}

fn later() -> Deadline {
    Deadline::at(Instant::now() + TIME_LIMIT)
}

/// The order files' matches arrived in, and the summary.
fn prioritized(
    root: &Path,
    query: &TextQuery,
    focused: &[&str],
    changed: Option<Vec<&str>>,
) -> (Vec<String>, TextSearchSummary) {
    let root = root.canonicalize().unwrap();
    let arrived = Mutex::new(Vec::new());
    let changed: Option<Vec<PathBuf>> = changed.map(|directories| {
        directories
            .into_iter()
            .map(|directory| root.join(directory))
            .collect()
    });
    let mut changed = Some(changed);
    let summary = search_prioritized(
        &root,
        query,
        &Priorities {
            focused_files: focused.iter().map(|file| root.join(file)).collect(),
        },
        None,
        &AtomicBool::new(false),
        &mut || changed.take().flatten(),
        &|found| arrived.lock().unwrap().push(found.path),
        later(),
    )
    .unwrap();
    (arrived.into_inner().unwrap(), summary)
}

/// The paths a plain walk of `root` finds, sorted.
fn walked(root: &Path, query: &TextQuery) -> Vec<String> {
    let root = root.canonicalize().unwrap();
    let (matches, _) = search_all(&root, query, None).unwrap();
    matches.into_iter().map(|found| found.path).collect()
}

/// Where each pass's files fall in `order`: every file of an earlier group first.
fn assert_groups(order: &[String], groups: &[&[&str]]) {
    let mut at = 0;
    for group in groups {
        let mut found: Vec<&str> = order[at..at + group.len()]
            .iter()
            .map(String::as_str)
            .collect();
        found.sort_unstable();
        let mut expected = group.to_vec();
        expected.sort_unstable();
        assert_eq!(found, expected, "in {order:?}");
        at += group.len();
    }
    assert_eq!(at, order.len(), "extra matches in {order:?}");
}

#[test]
fn a_priority_search_takes_focused_files_then_their_directories_then_changes_then_the_rest() {
    let directory = workspace(&[
        "a/one.rs",
        "z/b/focused.rs",
        "z/b/sibling.rs",
        "m/changed.rs",
        "m/deep/inner.rs",
        "top.rs",
    ]);
    let root = directory.path();
    let (order, summary) = prioritized(root, &needle(100), &["z/b/focused.rs"], Some(vec!["m"]));
    assert_groups(
        &order,
        &[
            &["z/b/focused.rs"],
            &["z/b/sibling.rs"],
            &["m/changed.rs", "m/deep/inner.rs"],
            &["a/one.rs", "top.rs"],
        ],
    );
    assert!(!summary.truncated);
}

#[test]
fn focused_files_keep_their_order_even_against_the_alphabet() {
    let directory = workspace(&["a/first.rs", "z/last.rs", "m/middle.rs"]);
    let (order, _) = prioritized(
        directory.path(),
        &needle(100),
        &["z/last.rs", "a/first.rs"],
        None,
    );
    assert_eq!(order, vec!["z/last.rs", "a/first.rs", "m/middle.rs"]);
}

#[test]
fn changed_directories_keep_their_weighted_order() {
    let directory = workspace(&["a/x.rs", "z/y.rs", "other.rs"]);
    let (order, _) = prioritized(directory.path(), &needle(100), &[], Some(vec!["z", "a"]));
    assert_eq!(order, vec!["z/y.rs", "a/x.rs", "other.rs"]);
}

#[test]
fn a_focused_root_file_never_makes_the_root_a_priority_directory() {
    let directory = workspace(&["README.md", "m/changed.rs", "a/one.rs"]);
    let (order, _) = prioritized(
        directory.path(),
        &needle(100),
        &["README.md"],
        Some(vec!["m"]),
    );
    // Without the guard, pass 2 would walk the whole root before the changes.
    assert_groups(&order, &[&["README.md"], &["m/changed.rs"], &["a/one.rs"]]);
}

#[test]
fn no_file_is_searched_twice_across_passes() {
    let directory = workspace(&["src/a.rs", "src/b.rs", "src/sub/c.rs", "lib/d.rs"]);
    let (order, _) = prioritized(
        directory.path(),
        &needle(100),
        &["src/a.rs", "src/a.rs", "src/sub/c.rs"],
        Some(vec!["src/sub", "src", "lib"]),
    );
    let mut unique = order.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(unique.len(), order.len(), "{order:?}");
    assert_eq!(order.len(), 4);
    assert_eq!(&order[..2], &["src/a.rs", "src/sub/c.rs"]);
}

#[test]
fn focused_files_follow_the_filters_and_ignore_rules() {
    let directory = workspace(&["keep.rs", "notes.md", "build/out.rs", "ignored.rs"]);
    let root = directory.path();
    crate::git_repository::init_repository(root);
    std::fs::write(root.join(".gitignore"), "ignored.rs\n").unwrap();
    let query = TextQuery {
        exclude: "*.md".into(),
        exclude_directories: vec!["build".into()],
        ..needle(100)
    };
    let (order, _) = prioritized(
        root,
        &query,
        &["notes.md", "build/out.rs", "ignored.rs", "keep.rs"],
        None,
    );
    assert_eq!(order, vec!["keep.rs"]);
}

#[test]
fn the_cap_holds_across_passes_when_reached_in_the_focused_files() {
    let directory = workspace(&["a/one.rs", "b/two.rs"]);
    std::fs::write(
        directory.path().join("a/one.rs"),
        "needle\nneedle\nneedle\n",
    )
    .unwrap();
    let (order, summary) =
        prioritized(directory.path(), &needle(2), &["a/one.rs"], Some(vec!["b"]));
    assert_eq!(order, vec!["a/one.rs", "a/one.rs"]);
    assert!(summary.truncated);
}

#[test]
fn the_deadline_holds_across_passes() {
    let directory = workspace(&["a/focused.rs", "m/changed.rs", "rest.rs"]);
    let root = directory.path().canonicalize().unwrap();
    let arrived = Mutex::new(Vec::new());
    let expired = Arc::new(AtomicBool::new(false));
    let summary = search_prioritized(
        &root,
        &needle(100),
        &Priorities {
            focused_files: vec![root.join("a/focused.rs")],
        },
        None,
        &AtomicBool::new(false),
        &mut || {
            // Git answers only after the search's time is up.
            expired.store(true, Ordering::Relaxed);
            Some(vec![root.join("m")])
        },
        &|found| arrived.lock().unwrap().push(found.path),
        Deadline::manual(expired.clone()),
    )
    .unwrap();
    assert_eq!(arrived.into_inner().unwrap(), vec!["a/focused.rs"]);
    assert!(summary.truncated);
    assert_eq!(summary.warning.as_deref(), Some(TIMEOUT_WARNING));
}

#[test]
fn a_slow_git_listing_never_holds_back_the_focused_files() {
    let directory = workspace(&["a/focused.rs", "m/changed.rs"]);
    let root = directory.path().canonicalize().unwrap();
    let events = Mutex::new(Vec::new());
    search_prioritized(
        &root,
        &needle(100),
        &Priorities {
            focused_files: vec![root.join("a/focused.rs")],
        },
        None,
        &AtomicBool::new(false),
        &mut || {
            events.lock().unwrap().push("git asked".to_string());
            None
        },
        &|found| events.lock().unwrap().push(found.path),
        later(),
    )
    .unwrap();
    let events = events.into_inner().unwrap();
    assert_eq!(&events[..2], &["a/focused.rs", "git asked"]);
    // Without Git's answer the changed directory is still searched, last.
    assert_eq!(events[2], "m/changed.rs");
}

#[test]
fn a_cancelled_search_searches_nothing_more() {
    let directory = workspace(&["a/one.rs", "b/two.rs"]);
    let cancelled = AtomicBool::new(true);
    let arrived = Mutex::new(Vec::new());
    search(
        directory.path(),
        &needle(100),
        None,
        &cancelled,
        &|found| arrived.lock().unwrap().push(found.path),
        later(),
    )
    .unwrap();
    assert!(arrived.into_inner().unwrap().is_empty());
}

#[test]
fn weighted_changes_drop_the_root_and_directories_beneath_a_listed_one() {
    let changes = vec![
        ("top.rs".to_string(), 500),
        ("src/a.rs".to_string(), 10),
        ("src/b.rs".to_string(), 0),
        ("src/deep/c.rs".to_string(), 3),
        ("docs/d.md".to_string(), 40),
    ];
    // docs weighs 41, src 12, src/deep 4: beneath src, so left out.
    assert_eq!(weighted_change_directories(&changes), vec!["docs", "src"]);
    let heavy_child = vec![
        ("src/deep/c.rs".to_string(), 99),
        ("src/a.rs".to_string(), 0),
    ];
    assert_eq!(
        weighted_change_directories(&heavy_child),
        vec!["src/deep", "src"]
    );
}

fn found(path: &str) -> TextMatch {
    TextMatch {
        path: path.into(),
        line: 1,
        column: 1,
        text: "needle".into(),
        match_start: 0,
        match_end: 6,
    }
}

#[test]
fn a_batch_goes_out_soon_after_its_first_match_while_the_search_continues() {
    let (send, matches) = mpsc::channel();
    let (sent, batches) = mpsc::channel();
    let batcher =
        std::thread::spawn(move || batch_matches(matches, |batch| sent.send(batch).unwrap()));
    send.send(found("a")).unwrap();
    let started = Instant::now();
    // The sender stays open, as while a large file is still being searched.
    let first = batches.recv_timeout(Duration::from_secs(1)).unwrap();
    assert_eq!(first.len(), 1);
    assert!(started.elapsed() < Duration::from_millis(500));
    for _ in 0..BATCH_SIZE {
        send.send(found("b")).unwrap();
    }
    assert_eq!(
        batches.recv_timeout(Duration::from_secs(1)).unwrap().len(),
        BATCH_SIZE
    );
    send.send(found("c")).unwrap();
    drop(send);
    assert_eq!(
        batches.recv_timeout(Duration::from_secs(1)).unwrap().len(),
        1
    );
    batcher.join().unwrap();
    assert!(batches.recv().is_err(), "nothing after the last batch");
}

/// A repository whose `.gitignore` ignores the directory `ignored/`.
fn ignoring_workspace(files: &[&str]) -> tempfile::TempDir {
    let directory = workspace(files);
    crate::git_repository::init_repository(directory.path());
    std::fs::write(directory.path().join(".gitignore"), "ignored/\n").unwrap();
    directory
}

#[test]
fn a_focused_file_beneath_an_ignored_directory_is_left_out_as_a_walk_leaves_it_out() {
    let directory = ignoring_workspace(&["keep/file.txt", "ignored/deep/file.txt"]);
    let query = needle(100);
    let (mut order, _) = prioritized(directory.path(), &query, &["ignored/deep/file.txt"], None);
    order.sort();
    assert_eq!(order, walked(directory.path(), &query));
    assert_eq!(order, vec!["keep/file.txt"]);
}

#[test]
fn a_changed_directory_beneath_an_ignored_one_is_left_out_too() {
    let directory = ignoring_workspace(&["keep/file.txt", "ignored/deep/file.txt"]);
    let (order, _) = prioritized(
        directory.path(),
        &needle(100),
        &[],
        Some(vec!["ignored/deep"]),
    );
    assert_eq!(order, vec!["keep/file.txt"]);
}

#[test]
fn an_include_beneath_an_ignored_directory_reaches_it_in_neither_order() {
    // An include names files; it doesn't let a walk into an ignored directory.
    let directory = ignoring_workspace(&["keep/file.txt", "ignored/deep/file.txt"]);
    let query = TextQuery {
        include: "ignored/deep/file.txt".into(),
        ..needle(100)
    };
    let (order, _) = prioritized(directory.path(), &query, &["ignored/deep/file.txt"], None);
    assert_eq!(order, walked(directory.path(), &query));
    assert!(order.is_empty());
}

#[test]
fn an_include_that_names_an_ignored_file_lets_both_orders_search_it() {
    let directory = workspace(&["keep/hidden.txt", "keep/other.txt"]);
    crate::git_repository::init_repository(directory.path());
    std::fs::write(directory.path().join(".gitignore"), "hidden.txt\n").unwrap();
    let query = TextQuery {
        include: "**/hidden.txt".into(),
        ..needle(100)
    };
    let (order, _) = prioritized(directory.path(), &query, &["keep/hidden.txt"], None);
    assert_eq!(order, walked(directory.path(), &query));
    assert_eq!(order, vec!["keep/hidden.txt"]);
}

#[test]
fn finding_what_a_walk_would_reach_stops_with_the_search() {
    let directory = workspace(&["a/one.rs"]);
    let root = directory.path().canonicalize().unwrap();
    let query = needle(100);
    let (starts, excluded) = directory_scopes(&root, &[], &[]);
    let targets = vec![root.join("a/one.rs")];

    let cancelled = AtomicBool::new(true);
    let run = Run::new(&root, &query, None, &cancelled, &|_| {}, later()).unwrap();
    assert!(
        run.reachable(&targets, false, &starts, &excluded)
            .is_empty()
    );

    let running = AtomicBool::new(false);
    let late = Run::new(
        &root,
        &query,
        None,
        &running,
        &|_| {},
        Deadline::at(Instant::now()),
    )
    .unwrap();
    assert!(
        late.reachable(&targets, false, &starts, &excluded)
            .is_empty()
    );
    assert_eq!(late.summary().warning.as_deref(), Some(TIMEOUT_WARNING));

    let live = Run::new(&root, &query, None, &running, &|_| {}, later()).unwrap();
    assert_eq!(
        live.reachable(&targets, false, &starts, &excluded),
        targets.into_iter().collect()
    );
}

#[test]
fn a_directory_that_cant_be_read_on_the_way_is_reported() {
    use std::os::unix::fs::PermissionsExt;
    let directory = workspace(&["a/locked/one.rs"]);
    let root = directory.path().canonicalize().unwrap();
    let locked = root.join("a/locked");
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
    let (order, summary) = prioritized(&root, &needle(100), &["a/locked/one.rs"], None);
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(order.is_empty());
    assert!(summary.truncated);
    assert!(summary.warning.unwrap().contains(UNREADABLE_WARNING));
}

/// A file that never matches and never ends, read slowly.
struct Endless;

impl Read for Endless {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        std::thread::sleep(Duration::from_millis(2));
        let line = b"nothing to see here\n";
        let length = line.len().min(buffer.len());
        buffer[..length].copy_from_slice(&line[..length]);
        Ok(length)
    }
}

#[test]
fn a_long_file_without_matches_stops_reading_when_the_search_is_cancelled() {
    let directory = workspace(&[]);
    let root = directory.path().canonicalize().unwrap();
    let query = needle(100);
    let cancelled = AtomicBool::new(false);
    let run = Run::new(&root, &query, None, &cancelled, &|_| {}, later()).unwrap();
    let started = Instant::now();
    std::thread::scope(|scope| {
        scope.spawn(|| {
            std::thread::sleep(Duration::from_millis(50));
            cancelled.store(true, Ordering::Relaxed);
        });
        run.search_reader(&mut Run::searcher(), "endless.txt", Endless);
    });
    assert!(started.elapsed() < Duration::from_secs(2));
    // Stopping isn't a read failure.
    assert_eq!(run.summary(), TextSearchSummary::default());
}

#[test]
fn a_long_file_without_matches_stops_reading_at_the_deadline() {
    let directory = workspace(&[]);
    let root = directory.path().canonicalize().unwrap();
    let query = needle(100);
    let cancelled = AtomicBool::new(false);
    let run = Run::new(
        &root,
        &query,
        None,
        &cancelled,
        &|_| {},
        Deadline::at(Instant::now() + Duration::from_millis(50)),
    )
    .unwrap();
    let started = Instant::now();
    run.search_reader(&mut Run::searcher(), "endless.txt", Endless);
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(run.summary().warning.as_deref(), Some(TIMEOUT_WARNING));
}
