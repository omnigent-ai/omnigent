//! Times working-copy comparisons on a real or synthetic checkout:
//! `cargo run --release --example working_copy_benchmark -- /path/to/checkout`
//! `cargo run --release --example working_copy_benchmark -- --synthesize DIR [FILES]`
//!
//! The synthetic repository is committed and indexed: FILES tracked files
//! (default 200,000), a branch 2,000 commits ahead of `origin/main`, 500
//! edited files, and 200 untracked ones. DIR must not exist yet.

use std::{
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

use workspace_helper::working_copy::{self, ComparisonKind};

fn git(root: &Path, arguments: &[&str]) {
    let status = Command::new("git")
        .args(arguments)
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .stdout(Stdio::null())
        .status()
        .unwrap();
    assert!(status.success(), "git {arguments:?}");
}

fn file_path(index: usize) -> String {
    format!(
        "d{:02}/d{:02}/d{:02}/f{index}.txt",
        index % 97,
        (index / 97) % 89,
        (index / 8633) % 83
    )
}

fn synthesize(root: &Path, files: usize) {
    // Never import over an existing checkout: the destination must be new.
    if let Err(error) = std::fs::create_dir(root) {
        eprintln!(
            "--synthesize needs a directory that doesn't exist yet: {}: {error}",
            root.display()
        );
        std::process::exit(2);
    }
    git(root, &["init", "-q", "-b", "main"]);
    let mut import = Command::new("git")
        .args(["fast-import", "--quiet"])
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .stdin(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stream = std::io::BufWriter::new(import.stdin.take().unwrap());
    let mut mark = 0;
    writeln!(
        stream,
        "commit refs/heads/main\nmark :{}\ncommitter T <t@e> 0 +0000\ndata 4\nbase",
        {
            mark += 1;
            mark
        }
    )
    .unwrap();
    for index in 0..files {
        let body = format!("line one of {index}\nline two\nline three\n");
        writeln!(
            stream,
            "M 100644 inline {}\ndata {}\n{body}",
            file_path(index),
            body.len()
        )
        .unwrap();
    }
    let base_mark = mark;
    writeln!(stream, "reset refs/remotes/origin/main\nfrom :{base_mark}").unwrap();
    let mut parent = base_mark;
    for commit in 0..2_000 {
        mark += 1;
        let path = file_path(commit * 37 % files);
        let body = format!("branch change {commit}\n");
        writeln!(
            stream,
            "commit refs/heads/feature\nmark :{mark}\ncommitter T <t@e> {commit} +0000\ndata 6\nchange\nfrom :{parent}\nM 100644 inline {path}\ndata {}\n{body}",
            body.len()
        )
        .unwrap();
        parent = mark;
    }
    drop(stream);
    assert!(import.wait().unwrap().success());
    git(
        root,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
    );
    git(
        root,
        &[
            "config",
            "remote.origin.url",
            "https://example.invalid/repo.git",
        ],
    );
    git(
        root,
        &[
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
    );
    git(root, &["checkout", "-q", "feature"]);
    for index in 0..500 {
        let path = root.join(file_path(index * 101 % files));
        let mut text = std::fs::read_to_string(&path).unwrap();
        text.push_str("edited in the working copy\n");
        std::fs::write(path, text).unwrap();
    }
    for index in 0..200 {
        let path = root.join(format!("untracked/u{index}.txt"));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "new\n").unwrap();
    }
}

fn milliseconds(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

#[tokio::main]
async fn main() {
    let mut arguments = std::env::args().skip(1);
    let first = arguments
        .next()
        .expect("a checkout path, or --synthesize DIR [FILES]");
    let root = if first == "--synthesize" {
        let root = PathBuf::from(arguments.next().expect("a directory to create"));
        let files = arguments
            .next()
            .map_or(200_000, |count| count.parse().unwrap());
        let started = Instant::now();
        synthesize(&root, files);
        println!(
            "synthesized {files} files in {:.0} ms",
            milliseconds(started.elapsed())
        );
        root
    } else {
        PathBuf::from(first)
    };
    let root = std::fs::canonicalize(root).unwrap();
    for kind in [ComparisonKind::LastCommit, ComparisonKind::BranchPoint] {
        for run in ["cold", "warm"] {
            let started = Instant::now();
            let deadline = tokio::time::Instant::now() + working_copy::LIST_DEADLINE;
            let repository = working_copy::open_repository(&root, deadline)
                .await
                .unwrap();
            let base = working_copy::resolve_base(&repository, kind, deadline)
                .await
                .unwrap();
            let resolved = started.elapsed();
            let changes = match working_copy::list_changes(&repository, base, deadline).await {
                Ok(changes) => changes,
                Err(unavailable) => {
                    println!(
                        "{kind:?} {run}: unavailable after {:.0} ms: {unavailable:?}",
                        milliseconds(started.elapsed())
                    );
                    continue;
                }
            };
            println!(
                "{kind:?} {run}: resolve {:.0} ms, list {:.0} ms, {} files",
                milliseconds(resolved),
                milliseconds(started.elapsed()),
                changes.entries.len()
            );
            if run == "warm" {
                let readable: Vec<_> = changes
                    .entries
                    .iter()
                    .filter(|entry| entry.base_object.is_some() && entry.problem.is_none())
                    .collect();
                let started = Instant::now();
                let mut bytes = 0;
                for entry in &readable {
                    let text = working_copy::read_base_blob(
                        &repository,
                        &changes.base.base,
                        &entry.path,
                        entry.base_object.as_ref().unwrap(),
                        10 * 1024 * 1024,
                        tokio::time::Instant::now() + working_copy::READ_DEADLINE,
                    )
                    .await
                    .unwrap();
                    bytes += text.bytes.len();
                }
                let total = started.elapsed();
                println!(
                    "{kind:?}: read {} base files ({bytes} bytes) in {:.0} ms, {:.1} ms each",
                    readable.len(),
                    milliseconds(total),
                    milliseconds(total) / readable.len().max(1) as f64
                );
            }
        }
    }
}
