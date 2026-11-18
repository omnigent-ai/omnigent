use std::{
    fs,
    path::Path,
    sync::{Arc, Mutex},
};

use ignore::{WalkBuilder, WalkState};

use super::WalkIgnore;

fn write(path: &Path, contents: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

#[test]
fn siblings_share_one_lookup_per_directory_and_thread() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().canonicalize().unwrap();
    crate::git_repository::init_repository(&root);
    write(&root.join(".gitignore"), "*.log\n");
    let directories = 20;
    for index in 0..directories {
        for file in 0..50 {
            write(&root.join(format!("d{index}/f{file}.txt")), "");
        }
    }
    let threads = 2;
    let rules = Arc::new(WalkIgnore::new(false, None, Some(&root)));
    let filter = rules.clone();
    let visited = Mutex::new(0usize);
    WalkBuilder::new(&root)
        .standard_filters(false)
        .threads(threads)
        .filter_entry(move |entry| entry.file_name() != ".git" && !filter.ignores(entry))
        .build_parallel()
        .run(|| {
            let visited = &visited;
            Box::new(move |_| {
                *visited.lock().unwrap() += 1;
                WalkState::Continue
            })
        });
    assert_eq!(*visited.lock().unwrap(), 1 + 1 + directories * 51);
    // Each directory whose entries are filtered: the root and its children.
    // A miss locks twice: once to look the directory up, once to remember it.
    let filtered_directories = 1 + directories;
    assert!(
        rules.shared_lookups() <= 2 * filtered_directories * threads,
        "{} shared lookups for {filtered_directories} directories",
        rules.shared_lookups()
    );
}
