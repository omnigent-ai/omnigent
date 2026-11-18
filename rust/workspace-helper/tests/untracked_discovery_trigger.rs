//! Opening a workspace and reading its files never looks for untracked files;
//! the first file search does. Its own test binary, because it puts a logging
//! `git` first on this process's `PATH`.

mod common;

use std::{fs, os::unix::fs::PermissionsExt, path::Path, time::Duration};

use common::{canonical, git, settled, start, write};
use workspace_helper::proto::{
    FileSearchMode, ListDirectoryRequest, ReadFileRequest, SearchFilesRequest, StatPathRequest,
};

/// Git's own binary, not a shim that would look `git` up on `PATH` again.
fn real_git() -> String {
    let output = std::process::Command::new("git")
        .arg("--exec-path")
        .output()
        .unwrap();
    format!("{}/git", String::from_utf8(output.stdout).unwrap().trim())
}

/// Puts a `git` that appends its arguments to `log` first on `PATH`.
fn log_git_calls(bin: &Path, log: &Path) {
    let wrapper = bin.join("git");
    fs::write(
        &wrapper,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexec '{}' \"$@\"\n",
            log.display(),
            real_git()
        ),
    )
    .unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    let path = format!(
        "{}:{}",
        bin.display(),
        std::env::var("PATH").unwrap_or_default()
    );
    // SAFETY: this binary's only test sets it before starting any helper.
    unsafe { std::env::set_var("PATH", path) };
}

fn untracked_scans(log: &Path) -> usize {
    fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter(|line| line.contains("ls-files --others"))
        .count()
}

#[tokio::test(flavor = "multi_thread")]
async fn only_a_file_search_starts_untracked_discovery() {
    let directory = tempfile::tempdir().unwrap();
    let root = canonical(&directory);
    git(&root, &["init", "-q"]);
    write(&root, "src/lib.rs", "fn main() {}\n");
    git(&root, &["add", "-A"]);
    write(&root, "notes.md", "new\n");
    let tools = tempfile::tempdir().unwrap();
    let log = tools.path().join("calls.log");
    log_git_calls(tools.path(), &log);

    let mut helper = start().await;
    helper.open(&root).await;
    helper
        .client
        .list_directory(ListDirectoryRequest {
            ..Default::default()
        })
        .await
        .unwrap();
    helper
        .client
        .stat_path(StatPathRequest {
            path: "src/lib.rs".into(),
        })
        .await
        .unwrap();
    helper
        .client
        .read_file(ReadFileRequest {
            path: "src/lib.rs".into(),
            ..Default::default()
        })
        .await
        .unwrap();
    // Opening lists tracked files ahead of the first search.
    for _ in 0..100 {
        if fs::read_to_string(&log)
            .unwrap_or_default()
            .contains("ls-files --cached")
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        fs::read_to_string(&log)
            .unwrap()
            .contains("ls-files --cached")
    );
    assert_eq!(
        untracked_scans(&log),
        0,
        "no untracked pass before a search"
    );

    let found = settled(
        &mut helper,
        SearchFilesRequest {
            query: "notes".into(),
            mode: FileSearchMode::Fuzzy as i32,
            ..Default::default()
        },
    )
    .await;
    assert_eq!(found.results[0].entry.as_ref().unwrap().path, "notes.md");
    let calls = fs::read_to_string(&log).unwrap();
    assert_eq!(untracked_scans(&log), 1, "{calls}");
}
