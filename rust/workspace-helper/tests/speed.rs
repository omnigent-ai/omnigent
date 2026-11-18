//! Speed on a index the size of a large monorepo. Timing needs an optimized build:
//! `cargo test --release --test speed -- --ignored`.

use std::time::Instant;

use workspace_helper::{
    file_index::{DirectoryScopes, FileIndex, IndexedPath},
    globs::PathFilter,
};

/// 720,000 file paths shaped like a large monorepo's.
fn monorepo_paths() -> Vec<IndexedPath> {
    let areas = [
        "sql", "runtime", "webapp", "infra", "ml", "storage", "security", "tools",
    ];
    let kinds = [
        "src/main/scala",
        "src/test/scala",
        "web/src/components",
        "python/tests",
        "proto",
    ];
    let names = [
        "Handler", "Service", "Client", "Utils", "Config", "Manager", "Reader", "Writer", "Planner",
    ];
    let extensions = ["scala", "py", "tsx", "proto", "rs", "go"];
    let mut paths = Vec::with_capacity(720_000);
    for index in 0..720_000usize {
        let area = areas[index % areas.len()];
        let kind = kinds[(index / 7) % kinds.len()];
        let name = names[(index / 3) % names.len()];
        let extension = extensions[index % extensions.len()];
        paths.push(IndexedPath {
            path: format!(
                "{area}/module{}/{kind}/pkg{}/{name}{index}.{extension}",
                index % 997,
                index % 31
            )
            .into(),
            is_dir: false,
        });
    }
    paths
}

#[test]
#[ignore = "timing needs --release"]
fn fuzzy_and_substring_search_answer_within_50_ms_on_720k_paths() {
    let index = FileIndex::from_paths(monorepo_paths());
    let filter = PathFilter::default();
    // Warm the thread pool, as a long-running helper is.
    index.search_fuzzy("warm", "", &filter, &DirectoryScopes::default(), 100);
    for query in [
        "planner",
        "sqlmodhandler",
        "webcomputils42",
        "storage/module5/proto",
    ] {
        let start = Instant::now();
        let (hits, _) = index.search_fuzzy(query, "", &filter, &DirectoryScopes::default(), 100);
        let fuzzy = start.elapsed();
        let start = Instant::now();
        index.search_substring(query, "", &filter, &DirectoryScopes::default(), 500);
        let substring = start.elapsed();
        println!(
            "{query}: fuzzy {fuzzy:?}, substring {substring:?}, {} hits",
            hits.len()
        );
        assert!(!hits.is_empty());
        assert!(fuzzy.as_millis() < 50, "fuzzy {query:?} took {fuzzy:?}");
        assert!(
            substring.as_millis() < 50,
            "substring {query:?} took {substring:?}"
        );
    }
}
