//! omnidev — a per-repo dev pod supervisor TUI for the Omnigent repo.
//!
//! Manages one isolated dev instance (its own state dir + ports) and its three
//! processes (server, host, vite), restarting the backend on Python changes
//! while Vite handles frontend HMR itself.

mod lock;
mod logs;
mod paths;
mod pod;
mod ports;
mod process;
mod state;
mod supervisor;
mod tui;
mod watcher;

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use clap::Parser;
use tokio::sync::mpsc;

use pod::Pod;
use ports::Ports;
use state::Shared;
use supervisor::{Cmd, Supervisor};

#[derive(Parser, Debug)]
#[command(
    name = "omnidev",
    about = "Isolated dev pod supervisor for the Omnigent repo"
)]
struct Args {
    /// Force the backend server port (default: probe from 6767).
    #[arg(long)]
    server_port: Option<u16>,

    /// Force the Vite dev-server port (default: probe from 5173).
    #[arg(long)]
    vite_port: Option<u16>,

    /// Use this pod directory instead of the per-repo default.
    #[arg(long)]
    pod_dir: Option<PathBuf>,

    /// Do not start the Vite frontend (backend + host only).
    #[arg(long)]
    no_vite: bool,

    /// Wipe the pod directory before starting.
    #[arg(long)]
    clean: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    let cwd = std::env::current_dir()?;
    let repo_root = paths::find_repo_root(&cwd)?;
    let pod_dir = match &args.pod_dir {
        Some(p) => p.clone(),
        None => paths::default_pod_dir(&repo_root)?,
    };

    if args.clean {
        pod::clean(&pod_dir)?;
    }
    std::fs::create_dir_all(&pod_dir)?;

    // Only one omnidev per pod — same-checkout runs share this dir and would
    // otherwise fight over ports and state. Held until the process exits.
    let _lock = lock::acquire(&pod_dir)?;

    let ports = Ports::resolve(&pod_dir, args.server_port, args.vite_port)?;
    let pod = Arc::new(Pod::create(repo_root, pod_dir, ports)?);

    let shared = Shared::new(&pod);
    let (cmd_tx, cmd_rx) = mpsc::unbounded_channel::<Cmd>();

    // File watcher: Python changes -> Reload commands. Keep the debouncer alive
    // for the whole session.
    let _watcher = watcher::spawn(&pod.omnigent_dir(), cmd_tx.clone())?;

    // Supervisor runs on the tokio runtime; the TUI drives it via cmd_tx.
    let supervisor = Supervisor::new(pod.clone(), shared.clone(), !args.no_vite);
    let sup_handle = tokio::spawn(supervisor.run(cmd_rx));

    // Run the TUI (owns the terminal) until the user quits.
    let app = tui::App::new(pod.clone(), shared.clone(), cmd_tx.clone());
    let result = app.run().await;

    // Tear down children, then wait for the supervisor to finish shutdown.
    let _ = cmd_tx.send(Cmd::Shutdown);
    let _ = sup_handle.await;

    result
}
