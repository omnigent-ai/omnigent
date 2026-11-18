use super::*;

fn index(paths: &[(&str, bool)]) -> FileIndex {
    let mut entries: Vec<IndexedPath> = paths
        .iter()
        .map(|(path, is_dir)| IndexedPath {
            path: (*path).into(),
            is_dir: *is_dir,
        })
        .collect();
    entries.sort();
    FileIndex { entries }
}

fn paths(hits: &[SearchHit]) -> Vec<&str> {
    hits.iter().map(|hit| hit.path.as_str()).collect()
}

#[test]
fn substring_returns_matching_files_and_directories_in_path_order() {
    let index = index(&[
        ("src", true),
        ("src/Main.rs", false),
        ("src/lib.rs", false),
        ("docs", true),
        ("docs/main.md", false),
    ]);
    let (hits, truncated) = index.search_substring(
        "MAIN",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        10,
    );
    assert_eq!(paths(&hits), vec!["docs/main.md", "src/Main.rs"]);
    assert!(!truncated);
    let (hits, _) = index.search_substring(
        "src",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        10,
    );
    assert_eq!(paths(&hits), vec!["src", "src/Main.rs", "src/lib.rs"]);
}

#[test]
fn substring_is_scoped_and_reports_truncation() {
    let index = index(&[
        ("a", true),
        ("a/x1", false),
        ("a/x2", false),
        ("a-x", false),
        ("b/x3", false),
    ]);
    let (hits, truncated) = index.search_substring(
        "x",
        "a",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        1,
    );
    assert_eq!(paths(&hits), vec!["x1"]);
    assert!(truncated);
    let (hits, truncated) = index.search_substring(
        "x",
        "a",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        5,
    );
    assert_eq!(paths(&hits), vec!["x1", "x2"]);
    assert!(!truncated);
}

#[test]
fn substring_applies_include_and_exclude_filters() {
    let index = index(&[
        ("web/node_modules/x.ts", false),
        ("web/src/x.ts", false),
        ("web/src/x.md", false),
    ]);
    let filter = PathFilter::new(&["*.ts".into()], &["node_modules".into()]);
    let (hits, _) = index.search_substring("x", "", &filter, &DirectoryScopes::default(), 10);
    assert_eq!(paths(&hits), vec!["web/src/x.ts"]);
}

#[test]
fn fuzzy_ranks_files_and_reports_matched_characters() {
    let index = index(&[
        ("web/src/shell/FolderTree.tsx", false),
        ("web/src/shell/FolderTree.test.tsx", false),
        ("docs/folder-tree-notes.md", false),
        ("web/src/shell", true),
    ]);
    let (hits, truncated) = index.search_fuzzy(
        "foldertree",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        2,
    );
    assert_eq!(hits.len(), 2);
    assert!(truncated);
    assert_eq!(hits[0].path, "web/src/shell/FolderTree.tsx");
    assert!(hits[0].score >= hits[1].score);
    assert_eq!(hits[0].matched_indices.len(), "foldertree".len());
    assert!(hits.iter().all(|hit| !hit.is_dir));
    let (none, _) = index.search_fuzzy(
        "   ",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        2,
    );
    assert!(none.is_empty());
}

#[test]
fn fuzzy_prefers_a_file_name_match_and_highlights_it() {
    let index = index(&[
        ("query/parser/main.rs", false),
        ("src/query_parser.rs", false),
        ("emoji/😀/query_parser.rs", false),
    ]);
    let (hits, _) = index.search_fuzzy(
        "queryparser",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        10,
    );
    assert_eq!(paths(&hits)[2], "query/parser/main.rs");
    let named = hits
        .iter()
        .find(|hit| hit.path == "src/query_parser.rs")
        .unwrap();
    assert_eq!(named.matched_indices.first(), Some(&4));
    // Indices count characters, so the emoji directory shifts them by one each.
    let emoji = hits
        .iter()
        .find(|hit| hit.path.starts_with("emoji"))
        .unwrap();
    assert_eq!(emoji.matched_indices.first(), Some(&8));
}

#[test]
fn fuzzy_matches_a_query_spanning_a_folder_and_a_differently_cased_name() {
    let index = index(&[
        (
            "experimental/iulian.dragos/static-analysis-test-deps/PLAN.md",
            false,
        ),
        ("experimental/other/README.md", false),
    ]);
    let (hits, _) = index.search_fuzzy(
        "iulian/plan.md",
        "",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        10,
    );
    assert_eq!(
        paths(&hits),
        ["experimental/iulian.dragos/static-analysis-test-deps/PLAN.md"]
    );
    // "iulian", then "/PLAN.md": the folder's name and the slash before the file's.
    let mut expected: Vec<u32> = (13..19).collect();
    expected.extend(52..60);
    assert_eq!(hits[0].matched_indices, expected);
}

#[test]
fn fuzzy_is_scoped_relative_to_the_directory() {
    let index = index(&[("a/one.rs", false), ("b/one.rs", false)]);
    let (hits, _) = index.search_fuzzy(
        "one",
        "b",
        &PathFilter::default(),
        &DirectoryScopes::default(),
        10,
    );
    assert_eq!(paths(&hits), vec!["one.rs"]);
}

#[test]
fn directory_scopes_include_and_exclude_whole_subtrees_only() {
    let scopes = DirectoryScopes {
        include: vec!["sql".into(), "web".into()],
        exclude: vec!["sql/legacy".into()],
    };
    assert!(scopes.admits("sql/core/a.scala"));
    assert!(scopes.admits("web"));
    assert!(!scopes.admits("sql/legacy/a.scala"));
    assert!(!scopes.admits("sql/legacy"));
    assert!(
        !scopes.admits("sqlite/a.c"),
        "a prefix that is not a directory"
    );
    assert!(!scopes.admits("docs/a.md"));
    assert!(DirectoryScopes::default().admits("anything"));
}

fn entries(index: &FileIndex) -> Vec<(&str, bool)> {
    index
        .paths()
        .map(|entry| (entry.path.as_ref(), entry.is_dir))
        .collect()
}

#[test]
fn git_file_lists_gain_their_directories_and_skip_nested_repositories() {
    let index = FileIndex::from_files(["src/a/b.rs", "src/a/c.rs", "README.md", "vendor/"]);
    assert_eq!(
        entries(&index),
        vec![
            ("README.md", false),
            ("src", true),
            ("src/a", true),
            ("src/a/b.rs", false),
            ("src/a/c.rs", false),
        ]
    );
}

#[test]
fn a_walk_stops_at_its_maximum_and_says_so() {
    let root = tempfile::tempdir().unwrap();
    for name in ["a", "b", "c", "d", "e"] {
        std::fs::write(root.path().join(name), "").unwrap();
    }
    let rules = IgnoreRules::new(root.path(), None);
    let cancelled = AtomicBool::new(false);
    let limits = |maximum| WalkLimits {
        cancelled: &cancelled,
        maximum,
    };
    let capped = FileIndex::walk(root.path(), IgnoreMode::None, &rules, &limits(3));
    assert!(capped.capped);
    assert_eq!(capped.index.len(), 3);
    let whole = FileIndex::walk(root.path(), IgnoreMode::None, &rules, &limits(5));
    assert!(!whole.capped);
    assert_eq!(whole.index.len(), 5);
}

#[test]
fn a_cancelled_walk_keeps_nothing() {
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("a"), "").unwrap();
    let rules = IgnoreRules::new(root.path(), None);
    let cancelled = AtomicBool::new(true);
    let walked = FileIndex::walk(
        root.path(),
        IgnoreMode::None,
        &rules,
        &WalkLimits {
            cancelled: &cancelled,
            maximum: usize::MAX,
        },
    );
    assert!(walked.index.is_empty());
    assert!(!walked.capped);
}
