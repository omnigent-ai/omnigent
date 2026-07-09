//! omnidev — dev tooling for Omnigent.
//!
//! Two independent capabilities in one binary:
//! - **pod supervisor** (bare `omnidev`): manages an isolated dev instance for
//!   the current checkout — server/host/vite, restarting the backend on Python
//!   changes while Vite handles frontend HMR.
//! - **install management** (`omnidev install`/`update`/`check`/…): install and
//!   keep a git-based omnigent up to date. These need no checkout and run
//!   anywhere.

mod install;
mod lock;
mod logs;
mod paths;
mod pod;
mod ports;
mod process;
mod shellhook;
mod state;
mod supervisor;
mod tui;
mod update_check;
mod watcher;

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use clap::{Parser, Subcommand};
use tokio::sync::mpsc;

use install::InstallConfig;
use pod::Pod;
use ports::Ports;
use state::Shared;
use supervisor::{Cmd, Supervisor};

#[derive(Parser, Debug)]
#[command(name = "omnidev", about = "Dev tooling for Omnigent", version)]
struct Args {
    #[command(subcommand)]
    command: Option<Command>,

    #[command(flatten)]
    run: RunArgs,
}

/// Flags for the default (no-subcommand) pod-supervisor run.
#[derive(clap::Args, Debug)]
struct RunArgs {
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

#[derive(Subcommand, Debug)]
enum Command {
    /// Install omnigent from git (defaults to the databricks extra, main).
    Install {
        /// Git ref (branch/tag/sha) to track.
        #[arg(long, default_value = install::DEFAULT_REF)]
        r#ref: String,
        /// Extra to include (repeatable). Defaults to `databricks`.
        #[arg(long = "extra")]
        extras: Vec<String>,
        /// Omit the default databricks extra (install with no extras).
        #[arg(long)]
        no_default_extra: bool,
        /// Git repo URL.
        #[arg(long, default_value = install::DEFAULT_REPO)]
        repo: String,
    },
    /// Reinstall the latest of the tracked ref/extras.
    Update,
    /// Check for an omnigent update (the shell hook calls this).
    Check {
        /// Print nothing when already up to date.
        #[arg(long)]
        quiet: bool,
    },
    /// Refresh the update-check cache from the network (usually run detached).
    Refresh,
    /// Print a shell snippet to eval from .zshrc/.bashrc for daily checks.
    ShellHook,
}

fn main() -> Result<()> {
    let args = Args::parse();

    // Install-management subcommands manage a global tool and must work from
    // anywhere — dispatch them before any checkout discovery.
    match args.command {
        Some(Command::Install {
            r#ref,
            extras,
            no_default_extra,
            repo,
        }) => {
            let extras = if !extras.is_empty() {
                extras
            } else if no_default_extra {
                vec![]
            } else {
                vec![install::DEFAULT_EXTRA.to_string()]
            };
            let config = InstallConfig {
                repo,
                git_ref: r#ref,
                extras,
            };
            install::install(&config)
        }
        Some(Command::Update) => install::update(),
        Some(Command::Check { quiet }) => update_check::check(quiet),
        Some(Command::Refresh) => update_check::refresh(),
        Some(Command::ShellHook) => {
            shellhook::print();
            Ok(())
        }
        None => run_supervisor(args.run),
    }
}

/// Default path: the pod supervisor for the current checkout. This is the only
/// path that requires an Omnigent checkout.
#[tokio::main]
async fn run_supervisor(args: RunArgs) -> Result<()> {
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
