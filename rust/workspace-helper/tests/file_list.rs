//! Searching a Git workspace's file list over the real transport: tracked
//! files from Git, untracked files from a background pass, and hits checked
//! against the disk, with no watching.

mod common;

use std::{fs, os::unix::fs::symlink, path::Path};

use common::{canonical, git, settled, start, write};
use workspace_helper::proto::{EntryType, FileSearchMode, SearchFilesRequest};

fn repository() -> (tempfile::TempDir, std::path::PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    git(&root, &["init", "-q"]);
    (directory, root)
}

fn commit(root: &Path) {
    git(root, &["add", "-A"]);
    git(
        root,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
    );
}

fn request(query: &str, mode: FileSearchMode) -> SearchFilesRequest {
    SearchFilesRequest {
        query: query.into(),
        mode: mode as i32,
        ..Default::default()
    }
}

fn paths(response: &workspace_helper::proto::SearchFilesResponse) -> Vec<(&str, i32)> {
    response
        .results
        .iter()
        .map(|result| result.entry.as_ref().unwrap())
        .map(|entry| (entry.path.as_str(), entry.r#type))
        .collect()
}

#[tokio::test(flavor = "multi_thread")]
async fn tracked_files_come_from_git_including_ones_that_match_ignore_rules() {
    let (_directory, root) = repository();
    write(&root, "src/app/Main.kt", "");
    write(&root, "vendored.log", "");
    commit(&root);
    // Tracked before the rule existed, so Git still lists it.
    write(&root, ".gitignore", "*.log\n");
    write(&root, "fresh.log", "");
    let mut helper = start().await;
    helper.open(&root).await;

    let logs = settled(&mut helper, request("log", FileSearchMode::Substring)).await;
    assert_eq!(paths(&logs), vec![("vendored.log", EntryType::File as i32)]);
    assert!(!logs.index_incomplete && !logs.untracked_unavailable);

    // A workspace below the top level lists only its own paths, relative to it.
    helper.open(&root.join("src")).await;
    let main = settled(&mut helper, request("main", FileSearchMode::Fuzzy)).await;
    assert_eq!(paths(&main), vec![("app/Main.kt", EntryType::File as i32)]);
}

#[tokio::test(flavor = "multi_thread")]
async fn untracked_files_arrive_after_the_first_answer_says_it_is_refreshing() {
    let (_directory, root) = repository();
    write(&root, "tracked.rs", "");
    commit(&root);
    write(&root, "UntrackedFuzzy.rs", "");
    let mut helper = start().await;
    helper.open(&root).await;

    let found = settled(&mut helper, request("untrfuzzy", FileSearchMode::Fuzzy)).await;
    assert_eq!(
        paths(&found),
        vec![("UntrackedFuzzy.rs", EntryType::File as i32)]
    );

    // A file created now waits for the next pass; a retry starts it at once.
    write(&root, "ReplacementFuzzy.rs", "");
    let before = helper
        .client
        .search_files(request("replfuzzy", FileSearchMode::Fuzzy))
        .await
        .unwrap()
        .into_inner();
    assert!(before.results.is_empty());
    assert!(
        !before.refreshing,
        "the last pass just finished, so none is due"
    );
    let retried = settled(
        &mut helper,
        SearchFilesRequest {
            retry_untracked: true,
            ..request("replfuzzy", FileSearchMode::Fuzzy)
        },
    )
    .await;
    assert_eq!(
        paths(&retried),
        vec![("ReplacementFuzzy.rs", EntryType::File as i32)]
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_commit_reloads_the_tracked_list() {
    let (_directory, root) = repository();
    write(&root, "first.rs", "");
    commit(&root);
    let mut helper = start().await;
    helper.open(&root).await;
    settled(&mut helper, request("first", FileSearchMode::Substring)).await;

    // Created and added before a pass could see it: only the index change finds it.
    write(&root, "second.rs", "");
    git(&root, &["add", "second.rs"]);
    let found = settled(&mut helper, request("second", FileSearchMode::Substring)).await;
    assert_eq!(paths(&found), vec![("second.rs", EntryType::File as i32)]);
}

#[tokio::test(flavor = "multi_thread")]
async fn hits_are_checked_against_the_disk() {
    let (_directory, root) = repository();
    write(&root, "real/target.rs", "");
    write(&root, "replaced.rs", "");
    write(&root, "deleted.rs", "");
    symlink("real", root.join("link_to_directory")).unwrap();
    symlink("real/target.rs", root.join("link_to_file.rs")).unwrap();
    symlink("nowhere.rs", root.join("broken_link.rs")).unwrap();
    commit(&root);
    // A submodule is a gitlink in the index: a directory on disk.
    let head = git(&root, &["rev-parse", "HEAD"]);
    fs::create_dir(root.join("submodule")).unwrap();
    git(
        &root,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{},submodule", head.trim()),
        ],
    );
    fs::remove_file(root.join("replaced.rs")).unwrap();
    fs::create_dir(root.join("replaced.rs")).unwrap();
    fs::remove_file(root.join("deleted.rs")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;

    let mut files: Vec<String> = Vec::new();
    for query in ["link", "submodule", "replaced", "deleted", "target"] {
        let found = settled(&mut helper, request(query, FileSearchMode::Fuzzy)).await;
        files.extend(
            found
                .results
                .into_iter()
                .map(|result| result.entry.unwrap().path),
        );
    }
    files.sort();
    files.dedup();
    assert_eq!(files, vec!["link_to_file.rs", "real/target.rs"]);
}

#[tokio::test(flavor = "multi_thread")]
async fn deleted_files_that_outrank_live_ones_do_not_crowd_them_out() {
    let (_directory, root) = repository();
    for number in 0..100 {
        write(&root, &format!("aaa/needle{number:03}.rs"), "");
    }
    for number in 0..5 {
        write(&root, &format!("zzz/needle{number}.rs"), "");
    }
    commit(&root);
    fs::remove_dir_all(root.join("aaa")).unwrap();
    let mut helper = start().await;
    helper.open(&root).await;
    let found = settled(
        &mut helper,
        SearchFilesRequest {
            maximum_results: 3,
            ..request("needle", FileSearchMode::Substring)
        },
    )
    .await;
    assert_eq!(
        paths(&found)
            .into_iter()
            .map(|(path, _)| path)
            .collect::<Vec<_>>(),
        vec!["zzz/needle0.rs", "zzz/needle1.rs", "zzz/needle2.rs"]
    );
    assert!(found.truncated, "two more live matches remain");
}

/// Inotify instances this process holds, from its open descriptors.
#[cfg(target_os = "linux")]
fn inotify_instances() -> usize {
    fs::read_dir("/proc/self/fd")
        .unwrap()
        .filter_map(|entry| fs::read_link(entry.ok()?.path()).ok())
        .filter(|target| target.to_string_lossy().contains("inotify"))
        .count()
}

#[cfg(target_os = "linux")]
#[tokio::test(flavor = "multi_thread")]
async fn opening_and_searching_a_workspace_watches_nothing() {
    let (_directory, root) = repository();
    write(&root, "src/lib.rs", "");
    write(&root, "untracked/notes.md", "");
    commit(&root);
    let before = inotify_instances();
    let mut helper = start().await;
    helper.open(&root).await;
    for include_ignored in [false, true] {
        settled(
            &mut helper,
            SearchFilesRequest {
                include_ignored,
                ..request("lib", FileSearchMode::Fuzzy)
            },
        )
        .await;
    }
    assert_eq!(inotify_instances(), before);
}
