//! Measures Go to File's file list through the helper's real gRPC lifecycle on
//! a large repository, printing one JSON object per measure.
//!
//! ```text
//! cargo run --release --example file_list_lifecycle -- generate <dir> [--scale 1.0] [--commit]
//! cargo run --release --example file_list_lifecycle -- run <dir> [--label cold] [--query NAME]
//! ```
//!
//! At scale 1.0 the generated repository matches a large monorepo checkout:
//! 1.6M tracked files in 300k directories, plus ignored build output under
//! `bazel-out/` and `node_modules/`, about 525k directories in all. `run` also
//! works on any existing checkout; `--query` names a file to look for first.

use std::{
    fs,
    io::Write as _,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};

use client::Client;
use tonic::transport::Server;
use workspace_helper::{
    proto::{
        FileSearchMode, GetInfoRequest, OpenWorkspaceRequest, SearchFilesRequest,
        SearchFilesResponse,
    },
    server::{self, WorkspaceHelperService},
    socket,
};

#[path = "../tests/common/client.rs"]
mod client;

const AREAS: usize = 20;
const MODULES_PER_AREA: usize = 50;
const TRACKED_DIRECTORIES: usize = 300_000;
const TRACKED_FILES: usize = 1_600_000;
const IGNORED_DIRECTORIES: usize = 225_000;
const IGNORED_FILES: usize = 580_000;
/// A name generated exactly once, in the last tracked directory.
const RARE_NAME: &str = "QuasarLatticeResolver";

fn main() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let option = |name: &str| {
        arguments
            .iter()
            .position(|argument| argument == name)
            .and_then(|index| arguments.get(index + 1))
            .cloned()
    };
    let flag = |name: &str| arguments.iter().any(|argument| argument == name);
    let root = PathBuf::from(arguments.get(1).expect("a directory"));
    match arguments.first().map(String::as_str) {
        Some("generate") => {
            let scale: f64 = option("--scale").map_or(1.0, |value| value.parse().unwrap());
            generate(&root, scale, flag("--commit"));
        }
        Some("run") => {
            let label = option("--label").unwrap_or_else(|| "unlabelled".into());
            let query = option("--query").unwrap_or_else(|| RARE_NAME.into());
            tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(run(&fs::canonicalize(&root).unwrap(), &label, &query));
        }
        _ => panic!("usage: file_list_lifecycle generate|run <dir> [options]"),
    }
}

fn report(fields: serde_json::Value) {
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{fields}").unwrap();
    stdout.flush().unwrap();
}

fn milliseconds(duration: Duration) -> f64 {
    (duration.as_secs_f64() * 10_000.0).round() / 10.0
}

/// The tracked directory with `index`, spread over areas and modules.
fn tracked_directory(index: usize, directories: usize) -> String {
    let modules = AREAS * MODULES_PER_AREA;
    let module = index % modules;
    let area = module / MODULES_PER_AREA;
    let leaves = directories.div_ceil(modules);
    format!(
        "area{area}/module{module}/pkg{}/sub{}",
        (index / modules) % leaves.max(1) / 10,
        index / modules
    )
}

fn generate(root: &Path, scale: f64, commit: bool) {
    let scaled = |count: usize| ((count as f64 * scale).round() as usize).max(1);
    let (tracked_directories, tracked_files) = (scaled(TRACKED_DIRECTORIES), scaled(TRACKED_FILES));
    let (ignored_directories, ignored_files) = (scaled(IGNORED_DIRECTORIES), scaled(IGNORED_FILES));
    let started = Instant::now();
    fs::create_dir_all(root).unwrap();
    git(root, &["init", "-q"]);
    fs::write(root.join(".gitignore"), "bazel-out/\nnode_modules/\n*.o\n").unwrap();
    let names = [
        "Handler", "Service", "Client", "Utils", "Config", "Reader", "Writer", "Planner",
    ];
    let extensions = ["scala", "py", "tsx", "proto", "rs", "go"];
    let threads = std::thread::available_parallelism().map_or(4, |count| count.get());
    std::thread::scope(|scope| {
        for thread in 0..threads {
            scope.spawn(move || {
                for directory in (thread..tracked_directories).step_by(threads) {
                    let relative = tracked_directory(directory, tracked_directories);
                    let path = root.join(&relative);
                    fs::create_dir_all(&path).unwrap();
                    let first = directory * tracked_files / tracked_directories;
                    let last = (directory + 1) * tracked_files / tracked_directories;
                    for file in first..last {
                        let name = if directory + 1 == tracked_directories && file + 1 == last {
                            format!("{RARE_NAME}.scala")
                        } else {
                            format!(
                                "{}{file}.{}",
                                names[file % names.len()],
                                extensions[file % extensions.len()]
                            )
                        };
                        fs::File::create(path.join(name)).unwrap();
                    }
                    // A nested rule file per module, as large monorepos have.
                    if directory < AREAS * MODULES_PER_AREA {
                        let module = path.parent().unwrap().parent().unwrap();
                        fs::write(module.join(".gitignore"), "*.generated\n").unwrap();
                    }
                }
                for directory in (thread..ignored_directories).step_by(threads) {
                    // Most build output is under bazel-out; a tenth is in node_modules.
                    let relative = if directory % 10 == 0 {
                        format!(
                            "area{}/web/node_modules/pkg{}/lib",
                            directory % AREAS,
                            directory / 10
                        )
                    } else {
                        format!(
                            "bazel-out/k8-fastbuild/bin/{}",
                            tracked_directory(directory, ignored_directories)
                        )
                    };
                    let path = root.join(relative);
                    fs::create_dir_all(&path).unwrap();
                    let first = directory * ignored_files / ignored_directories;
                    let last = (directory + 1) * ignored_files / ignored_directories;
                    for file in first..last {
                        fs::File::create(path.join(format!("out{file}.o"))).unwrap();
                    }
                }
            });
        }
    });
    let written = started.elapsed();
    let committed = commit.then(|| {
        let started = Instant::now();
        git(root, &["add", "-A"]);
        git(root, &["commit", "-q", "-m", "synthetic"]);
        started.elapsed()
    });
    report(serde_json::json!({
        "measure": "generate",
        "scale": scale,
        "tracked_files": tracked_files,
        "tracked_directories": tracked_directories,
        "ignored_files": ignored_files,
        "ignored_directories": ignored_directories,
        "write_ms": milliseconds(written),
        "commit_ms": committed.map(milliseconds),
    }));
}

fn git(root: &Path, arguments: &[&str]) {
    let status = Command::new("git")
        .args(arguments)
        .current_dir(root)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "bench")
        .env("GIT_AUTHOR_EMAIL", "bench@example.com")
        .env("GIT_COMMITTER_NAME", "bench")
        .env("GIT_COMMITTER_EMAIL", "bench@example.com")
        .status()
        .unwrap();
    assert!(status.success(), "git {arguments:?} failed");
}

/// Peak resident memory of this process so far, in bytes.
fn peak_resident_bytes() -> u64 {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::zeroed();
    // SAFETY: getrusage fills the struct it's given.
    unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) };
    let maximum = unsafe { usage.assume_init() }.ru_maxrss as u64;
    // macOS reports bytes; Linux reports kilobytes.
    if cfg!(target_os = "macos") {
        maximum
    } else {
        maximum * 1024
    }
}

fn search(query: &str) -> SearchFilesRequest {
    SearchFilesRequest {
        query: query.into(),
        mode: FileSearchMode::Fuzzy as i32,
        maximum_results: 100,
        ..Default::default()
    }
}

async fn timed(
    client: &mut Client,
    request: SearchFilesRequest,
) -> (Duration, SearchFilesResponse) {
    let started = Instant::now();
    let answer = client.search_files(request).await.unwrap().into_inner();
    (started.elapsed(), answer)
}

/// Searches every 50 ms until `found` holds and the answer has settled, or `limit` passes.
async fn until(
    client: &mut Client,
    request: SearchFilesRequest,
    limit: Duration,
    found: impl Fn(&SearchFilesResponse) -> bool,
) -> Option<Duration> {
    let started = Instant::now();
    let mut request = request;
    while started.elapsed() < limit {
        let (_, answer) = timed(client, request.clone()).await;
        if found(&answer) && !answer.refreshing {
            return Some(started.elapsed());
        }
        // A retry is sent once, as Go to File sends it.
        request.retry_untracked = false;
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    None
}

/// Inotify watches this process holds, summed over its inotify descriptors.
fn inotify_watches() -> Option<usize> {
    let descriptors = fs::read_dir("/proc/self/fdinfo").ok()?;
    Some(
        descriptors
            .filter_map(|entry| fs::read_to_string(entry.ok()?.path()).ok())
            .map(|info| {
                info.lines()
                    .filter(|line| line.starts_with("inotify wd:"))
                    .count()
            })
            .sum(),
    )
}

fn has_file(name: &str) -> impl Fn(&SearchFilesResponse) -> bool + '_ {
    move |answer| {
        answer.results.iter().any(|result| {
            result
                .entry
                .as_ref()
                .is_some_and(|entry| entry.path.ends_with(name))
        })
    }
}

async fn run(root: &Path, label: &str, query: &str) {
    let socket_directory = tempfile::tempdir().unwrap();
    let socket_path = socket_directory.path().join("helper.sock");
    let started = Instant::now();
    let listener = socket::bind(&socket_path).unwrap();
    tokio::spawn(
        Server::builder()
            .add_service(server::control_server(WorkspaceHelperService::new(None)))
            .add_service(server::workspace_server(WorkspaceHelperService::new(None)))
            .serve_with_incoming(socket::same_user_connections(listener)),
    );
    let mut client = client::connect(&socket_path).await;
    client.get_info(GetInfoRequest {}).await.unwrap();
    report(
        serde_json::json!({ "label": label, "measure": "helper_ready", "ms": milliseconds(started.elapsed()) }),
    );

    let started = Instant::now();
    client
        .open_workspace(OpenWorkspaceRequest {
            root: root.to_string_lossy().into_owned(),
            ..Default::default()
        })
        .await
        .unwrap();
    report(
        serde_json::json!({ "label": label, "measure": "open_workspace", "ms": milliseconds(started.elapsed()) }),
    );

    // Opening starts listing; the first query waits only for the tracked list.
    let (first, answer) = timed(&mut client, search(query)).await;
    let from_open = started.elapsed();
    report(serde_json::json!({
        "label": label,
        "measure": "first_query",
        "query": query,
        "ms_from_open": milliseconds(from_open),
        "query_ms": milliseconds(first),
        "hits": answer.results.len(),
        "indexed_paths": answer.indexed_paths,
        "refreshing": answer.refreshing,
        "peak_resident_mb": peak_resident_bytes() / 1_000_000,
    }));
    let settled = until(&mut client, search(query), Duration::from_secs(600), |_| {
        true
    })
    .await;
    report(serde_json::json!({
        "label": label,
        "measure": "untracked_pass_settled",
        "ms_after_first_query": settled.map(milliseconds),
        "peak_resident_mb": peak_resident_bytes() / 1_000_000,
    }));

    let mut durations = Vec::new();
    for round in 0..30 {
        let query = ["Handler", "srvc", "Config12", "pln.py", "area3/mod", query][round % 6];
        let (elapsed, _) = timed(&mut client, search(query)).await;
        durations.push(elapsed);
    }
    durations.sort();
    report(serde_json::json!({
        "label": label,
        "measure": "warm_queries",
        "p50_ms": milliseconds(durations[durations.len() / 2]),
        "p95_ms": milliseconds(durations[durations.len() * 95 / 100]),
        "peak_resident_mb": peak_resident_bytes() / 1_000_000,
    }));
    let (no_hit, _) = timed(&mut client, search("zzqqxxnomatch")).await;
    report(
        serde_json::json!({ "label": label, "measure": "no_hit_query", "ms": milliseconds(no_hit) }),
    );

    // A new untracked file appears after a retried untracked pass.
    let untracked = "BenchUntrackedAddition.rs";
    fs::write(root.join(untracked), "").unwrap();
    let request = SearchFilesRequest {
        retry_untracked: true,
        ..search("BenchUntrackedAddition")
    };
    let visible = until(
        &mut client,
        request,
        Duration::from_secs(600),
        has_file(untracked),
    )
    .await;
    report(
        serde_json::json!({ "label": label, "measure": "untracked_file_visible_after_retry", "ms": visible.map(milliseconds) }),
    );

    // `git add` rewrites the index, which reloads the tracked list.
    let staged = "BenchStagedAddition.rs";
    fs::write(root.join(staged), "").unwrap();
    git(root, &["add", "--", staged]);
    let started = Instant::now();
    let reloaded = until(
        &mut client,
        search("BenchStagedAddition"),
        Duration::from_secs(600),
        has_file(staged),
    )
    .await;
    report(serde_json::json!({
        "label": label,
        "measure": "staged_file_visible",
        "ms": reloaded.map(|_| milliseconds(started.elapsed())),
    }));
    git(root, &["rm", "-q", "--cached", "--", staged]);
    let _ = fs::remove_file(root.join(staged));
    let _ = fs::remove_file(root.join(untracked));

    report(serde_json::json!({
        "label": label,
        "measure": "process",
        "peak_resident_mb": peak_resident_bytes() / 1_000_000,
        "inotify_watches": inotify_watches(),
    }));
}
