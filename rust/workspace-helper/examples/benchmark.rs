//! Times the helper's hot paths on a real checkout:
//! `cargo run --release --example benchmark -- /path/to/checkout [query...]`

use std::{
    path::PathBuf,
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};

use workspace_helper::{
    file_index::{DirectoryScopes, FileIndex},
    globs::PathFilter,
    ignore_rules::{IgnoreRules, system_global_excludes},
    listing,
    text_search::{self, TextQuery},
    walk::IgnoreMode,
};

fn milliseconds(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

fn main() {
    let mut arguments = std::env::args().skip(1);
    let root =
        std::fs::canonicalize(PathBuf::from(arguments.next().expect("a checkout path"))).unwrap();
    let mut queries: Vec<String> = arguments.collect();
    if queries.is_empty() {
        queries = ["readme", "srvmain", "FolderTree", "test_utils.py", "BUILD"]
            .map(String::from)
            .to_vec();
    }
    let rules = IgnoreRules::new(&root, system_global_excludes());

    let start = Instant::now();
    let entries = listing::list(&rules, &root, "", false, true).unwrap();
    println!(
        "list root: {} entries in {:.1} ms",
        entries.len(),
        milliseconds(start)
    );

    let start = Instant::now();
    let index = FileIndex::build(&root, IgnoreMode::Git, &rules);
    println!(
        "index build: {} paths in {:.0} ms",
        index.len(),
        milliseconds(start)
    );

    let filter = PathFilter::default();
    for query in &queries {
        let start = Instant::now();
        let (hits, _) = index.search_fuzzy(query, "", &filter, &DirectoryScopes::default(), 100);
        let fuzzy = milliseconds(start);
        let start = Instant::now();
        let (substring, _) =
            index.search_substring(query, "", &filter, &DirectoryScopes::default(), 500);
        println!(
            "query {query:?}: fuzzy {fuzzy:.1} ms (top {:?}), substring {:.1} ms ({} hits)",
            hits.first().map(|hit| hit.path.as_str()),
            milliseconds(start),
            substring.len()
        );
    }

    for query in &queries {
        let first = Mutex::new(None::<f64>);
        let cancelled = AtomicBool::new(false);
        let start = Instant::now();
        let text = TextQuery {
            query: query.clone(),
            limit: 500,
            ..TextQuery::default()
        };
        let summary = text_search::search(
            &root,
            &text,
            rules.global_excludes(),
            &cancelled,
            &|_| {
                first
                    .lock()
                    .unwrap()
                    .get_or_insert_with(|| milliseconds(start));
            },
            text_search::Deadline::at(Instant::now() + text_search::TIME_LIMIT),
        )
        .unwrap();
        cancelled.store(true, Ordering::Relaxed);
        println!(
            "text {query:?}: first match {:?} ms, done {:.0} ms, truncated {}",
            first.lock().unwrap().map(|value| value.round()),
            milliseconds(start),
            summary.truncated
        );
    }
}
