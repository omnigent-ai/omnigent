//! Serves the helper in-process on a temporary Unix socket and connects a
//! client to it, so tests exercise the real transport.

#![allow(dead_code)]

use std::{
    fs,
    io::BufRead,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};

use tokio_stream::StreamExt;
use tonic::transport::Server;
use workspace_helper::{
    proto::{OpenWorkspaceRequest, SearchFilesRequest, SearchFilesResponse},
    server::{self, WorkspaceHelperService},
    socket,
};

mod client;
// Not every test uses every helper.
#[allow(unused_imports)]
pub use client::{Client, connect, connect_stream};

pub struct Helper {
    pub client: Client,
    _socket_directory: tempfile::TempDir,
}

/// A helper with no global excludes file, so the user's own cannot leak in.
pub async fn start() -> Helper {
    start_with_global_excludes(None).await
}

pub async fn start_with_global_excludes(global_excludes: Option<PathBuf>) -> Helper {
    start_service(WorkspaceHelperService::new(global_excludes)).await
}

pub async fn start_service(service: WorkspaceHelperService) -> Helper {
    let socket_directory = tempfile::tempdir().unwrap();
    let path = socket_directory.path().join("helper.sock");
    let listener = socket::bind(&path).unwrap();
    tokio::spawn(
        Server::builder()
            .add_service(server::control_server(service.clone()))
            .add_service(server::workspace_server(service))
            .serve_with_incoming(socket::same_user_connections(listener)),
    );
    Helper {
        client: connect(&path).await,
        _socket_directory: socket_directory,
    }
}

/// Runs the helper executable on `socket`, exiting once its stdin closes, and
/// returns it with its ready line. `configure` adjusts the child's command.
pub fn spawn_binary(
    socket: &Path,
    configure: impl FnOnce(&mut Command),
) -> (Child, serde_json::Value) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"));
    command
        .args(["--socket", socket.to_str().unwrap(), "--exit-on-stdin-eof"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configure(&mut command);
    let mut child = command.spawn().unwrap();
    let mut line = String::new();
    std::io::BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    (
        child,
        serde_json::from_str(&line).unwrap_or(serde_json::Value::Null),
    )
}

/// The helper executable, connected; it exits when the returned child's stdin
/// is dropped, as on a failing test.
pub async fn start_binary(configure: impl FnOnce(&mut Command)) -> (Helper, Child) {
    let socket_directory = tempfile::tempdir().unwrap();
    let path = socket_directory.path().join("helper.sock");
    let (child, ready) = spawn_binary(&path, configure);
    assert_eq!(ready["ready"], true, "{ready}");
    let helper = Helper {
        client: connect(&path).await,
        _socket_directory: socket_directory,
    };
    (helper, child)
}

impl Helper {
    pub async fn open(&mut self, root: &Path) -> String {
        self.client
            .open_workspace(OpenWorkspaceRequest {
                root: root.to_string_lossy().into_owned(),
                ..Default::default()
            })
            .await
            .unwrap()
            .into_inner()
            .workspace_id
    }
}

/// Searches until the answer stops saying `refreshing`, as Go to File keeps
/// asking. A retry is sent on the first request only.
pub async fn settled(helper: &mut Helper, request: SearchFilesRequest) -> SearchFilesResponse {
    let mut request = request;
    for _ in 0..500 {
        let response = helper
            .client
            .search_files(request.clone())
            .await
            .unwrap()
            .into_inner();
        if !response.refreshing {
            return response;
        }
        request.retry_untracked = false;
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    panic!("the file list never settled");
}

pub fn write(root: &Path, relative: &str, contents: &str) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, contents).unwrap();
}

pub fn git(root: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(root)
        // Keep the user's global excludes and config out of fixtures.
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success() || output.status.code() == Some(1),
        "git {arguments:?} failed"
    );
    String::from_utf8(output.stdout).unwrap()
}

/// A repository exercising nested rules, negation, directory-only
/// patterns, anchoring, `.git/info/exclude`, and a nested repository.
pub fn ignore_fixture() -> tempfile::TempDir {
    let directory = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(directory.path()).unwrap();
    git(&root, &["init", "-q"]);
    write(
        &root,
        ".gitignore",
        "*.log\n!keep.log\nbuild/\n/anchored.txt\nnode_modules\n",
    );
    write(
        &root,
        "src/.gitignore",
        "generated/\n*.tmp\n!important.tmp\n",
    );
    write(&root, ".git/info/exclude", "secret.env\n");
    for path in [
        "README.md",
        "app.log",
        "keep.log",
        "anchored.txt",
        "src/anchored.txt",
        "src/main.rs",
        "src/scratch.tmp",
        "src/important.tmp",
        "src/generated/out.rs",
        "src/deep/generated/also.rs",
        "build/output.bin",
        "src/build/inner.txt",
        "node_modules/pkg/index.js",
        "web/node_modules/pkg/index.js",
        "config/secret.env",
        ".hidden/visible.txt",
        "vendor/lib/.gitignore",
        "vendor/lib/code.c",
    ] {
        write(&root, path, "contents\n");
    }
    write(&root, "vendor/lib/.gitignore", "code.c\n");
    // A nested repository keeps its own rules.
    write(&root, "nested/.gitignore", "local-only.txt\n");
    write(&root, "nested/local-only.txt", "x\n");
    write(&root, "nested/tracked.txt", "x\n");
    git(&root.join("nested"), &["init", "-q"]);
    directory
}

pub fn canonical(directory: &tempfile::TempDir) -> PathBuf {
    fs::canonicalize(directory.path()).unwrap()
}

pub async fn collect<T>(stream: tonic::Streaming<T>) -> Result<Vec<T>, tonic::Status> {
    stream.collect::<Result<Vec<T>, tonic::Status>>().await
}
