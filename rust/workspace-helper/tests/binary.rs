//! The `omnigent-workspace-helper` executable's lifecycle, as a supervisor sees it.

mod common;

use std::{
    io::Write,
    os::unix::fs::PermissionsExt,
    process::{Child, Command},
    time::{Duration, Instant},
};

use workspace_helper::proto::{GetInfoRequest, OpenWorkspaceRequest};

fn spawn(socket: &std::path::Path) -> (Child, serde_json::Value) {
    common::spawn_binary(socket, |_| {})
}

fn wait_for_exit(child: &mut Child) -> std::process::ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        assert!(Instant::now() < deadline, "the helper did not exit");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn version_prints_the_protocol_and_capabilities() {
    let output = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
        .arg("--version")
        .output()
        .unwrap();
    assert!(output.status.success());
    let info: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(info["protocolVersion"], 1);
    assert!(
        info["capabilities"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "ReadFile")
    );
}

#[test]
fn bad_arguments_print_usage() {
    let output = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
        .arg("--bogus")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("usage:"));
}

#[tokio::test(flavor = "multi_thread")]
async fn serves_on_a_private_socket_and_exits_when_stdin_closes() {
    let directory = tempfile::tempdir().unwrap();
    let socket = directory.path().join("helper.sock");
    // A socket left by a crashed helper is replaced.
    drop(std::os::unix::net::UnixListener::bind(&socket).unwrap());

    let (mut child, ready) = spawn(&socket);
    assert_eq!(ready["ready"], true);
    assert_eq!(ready["socket"], socket.to_str().unwrap());
    assert_eq!(
        std::fs::metadata(&socket).unwrap().permissions().mode() & 0o777,
        0o600
    );

    let mut client = common::connect(&socket).await;
    let info = client
        .get_info(GetInfoRequest {})
        .await
        .unwrap()
        .into_inner();
    assert_eq!(info.protocol_version, 1);

    // A second helper refuses a socket that is still being served.
    let second = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
        .args(["--socket", socket.to_str().unwrap()])
        .output()
        .unwrap();
    assert_eq!(second.status.code(), Some(3));

    let mut stdin = child.stdin.take().unwrap();
    stdin.flush().unwrap();
    drop(stdin);
    assert!(wait_for_exit(&mut child).success());
    assert!(!socket.exists(), "the helper removes its socket");
}

#[test]
fn refuses_to_replace_a_file_that_is_not_a_socket() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("not-a-socket");
    std::fs::write(&path, "keep me").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
        .args(["--socket", path.to_str().unwrap()])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "keep me");
}

#[tokio::test(flavor = "multi_thread")]
async fn exits_promptly_when_stdin_closes_with_a_workspace_open() {
    let directory = tempfile::tempdir().unwrap();
    let socket = directory.path().join("helper.sock");
    let workspace = tempfile::tempdir().unwrap();
    let (mut child, ready) = spawn(&socket);
    assert_eq!(ready["ready"], true);
    let mut client = common::connect(&socket).await;
    client
        .open_workspace(OpenWorkspaceRequest {
            root: workspace.path().to_string_lossy().into_owned(),
            ..Default::default()
        })
        .await
        .unwrap();

    let started = Instant::now();
    drop(child.stdin.take());
    let status = tokio::task::spawn_blocking(move || wait_for_exit(&mut child))
        .await
        .unwrap();
    assert!(status.success());
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "took {:?}",
        started.elapsed()
    );
    assert!(!socket.exists());
}

/// Runs the helper on one end of a socketpair, as the host does, and returns
/// it with a client on the other end.
async fn spawn_on_connection() -> (Child, serde_json::Value, common::Client) {
    use std::os::{fd::AsRawFd, unix::process::CommandExt};

    let (ours, theirs) = std::os::unix::net::UnixStream::pair().unwrap();
    let fd = theirs.as_raw_fd();
    let mut command = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"));
    command
        .args(["--connection-fd", &fd.to_string()])
        .stdout(std::process::Stdio::piped());
    // SAFETY: fcntl is async-signal-safe; it only lets the child keep `fd`.
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(fd, libc::F_SETFD, 0) == 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        });
    }
    let mut child = command.spawn().unwrap();
    drop(theirs);
    let mut line = String::new();
    std::io::BufRead::read_line(
        &mut std::io::BufReader::new(child.stdout.take().unwrap()),
        &mut line,
    )
    .unwrap();
    let client = common::connect_stream(ours).await;
    (child, serde_json::from_str(&line).unwrap(), client)
}

#[tokio::test(flavor = "multi_thread")]
async fn serves_one_inherited_connection_and_exits_when_it_closes() {
    let workspace = tempfile::tempdir().unwrap();
    let (mut child, ready, mut client) = spawn_on_connection().await;
    assert_eq!(ready["ready"], true, "{ready}");
    assert!(ready["connectionFd"].as_i64().unwrap() > 2);
    assert_eq!(
        client
            .get_info(GetInfoRequest {})
            .await
            .unwrap()
            .into_inner()
            .protocol_version,
        1
    );
    client
        .open_workspace(OpenWorkspaceRequest {
            root: workspace.path().to_string_lossy().into_owned(),
            ..Default::default()
        })
        .await
        .unwrap();
    #[cfg(target_os = "linux")]
    {
        // Git and other children of the helper don't inherit the connection.
        let fd = ready["connectionFd"].as_i64().unwrap();
        let flags = std::fs::read_to_string(format!("/proc/{}/fdinfo/{fd}", child.id())).unwrap();
        let flags = flags
            .lines()
            .find_map(|line| line.strip_prefix("flags:"))
            .map(|value| u32::from_str_radix(value.trim(), 8).unwrap())
            .unwrap();
        assert_ne!(flags & libc::O_CLOEXEC as u32, 0, "{flags:o}");
    }
    drop(client);
    let status = tokio::task::spawn_blocking(move || wait_for_exit(&mut child))
        .await
        .unwrap();
    assert!(status.success());
}

#[test]
fn refuses_a_descriptor_that_is_not_a_socket() {
    use std::os::{fd::AsRawFd, unix::process::CommandExt};

    let directory = tempfile::tempdir().unwrap();
    let file = std::fs::File::create(directory.path().join("plain")).unwrap();
    let fd = file.as_raw_fd();
    let mut command = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"));
    command.args(["--connection-fd", &fd.to_string()]);
    // SAFETY: fcntl is async-signal-safe; it only lets the child keep `fd`.
    unsafe {
        command.pre_exec(move || {
            libc::fcntl(fd, libc::F_SETFD, 0);
            Ok(())
        });
    }
    let output = command.output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("not a socket"));

    // A descriptor the child doesn't have is refused the same way.
    let output = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
        .args(["--connection-fd", "987"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
}

#[test]
fn needs_exactly_one_way_to_listen() {
    for arguments in [
        &[][..],
        &["--connection-fd", "1"],
        &["--connection-fd", "x"],
        &["--connection-fd", "5", "--socket", "/tmp/unused.sock"],
        &["--exit-on-stdin-eof"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_omnigent-workspace-helper"))
            .args(arguments)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{arguments:?}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("usage:"));
    }
}
