//! `omnigent-workspace-helper --connection-fd N | --socket PATH [--exit-on-stdin-eof]`
//! serves until it is signalled, its stdin closes, or (with `--connection-fd`)
//! its one connection closes. `--version` prints the build and protocol as JSON.

use std::{os::fd::RawFd, path::PathBuf, process::ExitCode};

use tokio::io::AsyncReadExt;
use tonic::transport::Server;
use workspace_helper::{build_info, server::WorkspaceHelperService, socket};

const SHUTDOWN_DRAIN: std::time::Duration = std::time::Duration::from_secs(2);
const USAGE: &str = "usage: omnigent-workspace-helper (--connection-fd N | --socket PATH) \
                     [--exit-on-stdin-eof] | --version";

fn info_json() -> serde_json::Value {
    serde_json::json!({
        "protocolVersion": build_info::PROTOCOL_VERSION,
        "version": build_info::version(),
        "commit": build_info::commit(),
        "capabilities": build_info::CAPABILITIES,
    })
}

enum Listen {
    Socket(PathBuf),
    ConnectionFd(RawFd),
}

async fn shutdown_signal(exit_on_stdin_eof: bool) {
    let stdin_closed = async {
        if !exit_on_stdin_eof {
            return std::future::pending().await;
        }
        let mut stdin = tokio::io::stdin();
        let mut buffer = [0u8; 256];
        while matches!(stdin.read(&mut buffer).await, Ok(read) if read > 0) {}
    };
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler installs");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = terminate.recv() => {}
        _ = stdin_closed => {}
    }
}

fn parse(mut arguments: impl Iterator<Item = String>) -> Result<(Listen, bool), ExitCode> {
    let mut listen = None::<Listen>;
    let mut exit_on_stdin_eof = false;
    let usage = || {
        eprintln!("{USAGE}");
        ExitCode::from(2)
    };
    while let Some(argument) = arguments.next() {
        let next = match argument.as_str() {
            "--version" => {
                println!("{}", info_json());
                return Err(ExitCode::SUCCESS);
            }
            "--exit-on-stdin-eof" => {
                exit_on_stdin_eof = true;
                continue;
            }
            "--socket" => arguments.next().map(|path| Listen::Socket(path.into())),
            "--connection-fd" => arguments
                .next()
                .and_then(|fd| fd.parse().ok())
                .filter(|fd| *fd > 2)
                .map(Listen::ConnectionFd),
            _ => None,
        };
        match (next, &listen) {
            (Some(next), None) => listen = Some(next),
            _ => return Err(usage()),
        }
    }
    listen
        .map(|listen| (listen, exit_on_stdin_eof))
        .ok_or_else(usage)
}

fn main() -> ExitCode {
    let (listen, exit_on_stdin_eof) = match parse(std::env::args().skip(1)) {
        Ok(parsed) => parsed,
        Err(code) => return code,
    };
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("the runtime starts")
        .block_on(serve(listen, exit_on_stdin_eof))
}

async fn serve(listen: Listen, exit_on_stdin_eof: bool) -> ExitCode {
    let mut ready = info_json();
    ready["ready"] = true.into();
    let (stopping, stop) = tokio::sync::watch::channel(false);
    let incoming = match &listen {
        Listen::Socket(path) => match socket::bind(path) {
            Ok(listener) => {
                ready["socket"] = path.to_string_lossy().into_owned().into();
                socket::listener_incoming(listener)
            }
            Err(error) => {
                eprintln!(
                    "omnigent-workspace-helper: could not listen on {}: {error}",
                    path.display()
                );
                return ExitCode::from(if error.kind() == std::io::ErrorKind::AddrInUse {
                    3
                } else {
                    1
                });
            }
        },
        Listen::ConnectionFd(fd) => match socket::inherited_connection(*fd, stopping.clone()) {
            Ok(incoming) => {
                ready["connectionFd"] = (*fd).into();
                incoming
            }
            Err(error) => {
                eprintln!("omnigent-workspace-helper: could not use descriptor {fd}: {error}");
                return ExitCode::from(1);
            }
        },
    };
    println!("{ready}");
    tokio::spawn(async move {
        shutdown_signal(exit_on_stdin_eof).await;
        let _ = stopping.send(true);
    });
    let mut graceful = stop.clone();
    let mut forced = stop;
    let service = WorkspaceHelperService::default();
    let server = Server::builder()
        .add_service(workspace_helper::server::control_server(service.clone()))
        .add_service(workspace_helper::server::workspace_server(service))
        .serve_with_incoming_shutdown(incoming, async move {
            let _ = graceful.wait_for(|stopping| *stopping).await;
        });
    // A request or stream still running after the drain period does not
    // keep the helper alive.
    let served = tokio::select! {
        served = server => served,
        _ = async move {
            let _ = forced.wait_for(|stopping| *stopping).await;
            tokio::time::sleep(SHUTDOWN_DRAIN).await;
        } => Ok(()),
    };
    if let Listen::Socket(path) = &listen {
        let _ = std::fs::remove_file(path);
    }
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("omnigent-workspace-helper: {error}");
            ExitCode::FAILURE
        }
    }
}
