//! omnidev — dev tooling for Omnigent.
//!
//! Three surfaces in one binary:
//! - **pod supervisor** (bare `omnidev`): manages an isolated dev instance for
//!   the current checkout — server/host/vite, restarting the backend on Python
//!   changes while Vite handles frontend HMR.
//! - **install management** (`omnidev install`/`update`/`check`/…): install and
//!   keep a git-based omnigent up to date. These need no checkout and run
//!   anywhere.
//! - **omnigent passthrough** (`omnidev omnigent …`): run any omnigent command
//!   against the current checkout's pod via `uv run omnigent …`, with the pod's
//!   isolated env applied. Requires a checkout, like the supervisor.

mod install;
mod lan;
mod lock;
mod logs;
mod paths;
mod pod;
mod omnigent_cmd;
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

/// Top-level subcommands. Install management works anywhere; the `omnigent`
/// passthrough requires a checkout (like the bare supervisor default).
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
    /// Run an omnigent command against this checkout's pod (`uv run omnigent …`).
    ///
    /// Everything after the subcommand is forwarded verbatim to omnigent. The
    /// pod's isolated env (data dir, database, config, server URL) is applied,
    /// so a command talks to the same pod the supervisor runs — and coexists
    /// with a running supervisor. Use `--` to pass flags that look like
    /// omnidev's own: `omnidev omnigent -- --verbose agent run …`.
    Omnigent {
        #[arg(num_args = 0.., trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
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

    /// Vite dev-server bind host (default: 127.0.0.1; use 0.0.0.0 for LAN access).
    #[arg(long, default_value = "127.0.0.1")]
    vite_host: String,

    /// Trust this machine's LAN origins so a phone/tablet on the same network
    /// can use the UI (uploads + live stream). Pairs with `--vite-host 0.0.0.0`.
    #[arg(long)]
    trust_lan_origins: bool,

    /// Use this pod directory instead of the per-repo default.
    #[arg(long)]
    pod_dir: Option<PathBuf>,

    /// Do not start the Vite frontend (backend + host only).
    #[arg(long)]
    no_vite: bool,

    /// Wipe the pod directory before starting.
    #[arg(long)]
    clean: bool,

    /// Log every observed file change and whether it triggers a backend reload
    /// (with the skip reason otherwise).
    #[arg(long)]
    debug: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();

    // Install-management subcommands manage a global tool and must work from
    // anywhere — dispatch them before any checkout discovery. `omnigent …` is
    // the pod-wired passthrough. No subcommand runs the pod supervisor.
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
        Some(Command::Omnigent { args: passthrough }) => run_omnigent(args.run, passthrough),
        None => run_supervisor(args.run),
    }
}

/// `omnidev omnigent …` — run an arbitrary omnigent command against this
/// checkout's pod via `uv run omnigent …`, with the pod's isolated env (data
/// dir, database URI, config home, server URL) applied on top of the inherited
/// parent env. Resolves the repo root and pod dir (same as the supervisor),
/// ensures the pod tree exists, then spawns in the foreground inheriting stdio.
/// Exits with omnigent's status code. Acquires no lock — it coexists with a
/// running supervisor (the common case: server up, you run a command).
fn run_omnigent(args: RunArgs, passthrough: Vec<String>) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let repo_root = paths::find_repo_root(&cwd)?;
    let pod_dir = match &args.pod_dir {
        Some(p) => p.clone(),
        None => paths::default_pod_dir(&repo_root)?,
    };
    std::fs::create_dir_all(&pod_dir)?;

    // Read persisted ports so OMNIGENT_URL points at a running supervisor's
    // server (if any). Supervisor-only flags don't apply to the passthrough, so
    // never override — the pod stays in sync with whatever the supervisor set.
    let ports = Ports::resolve(&pod_dir, None, None)?;
    let pod = Pod::create(
        repo_root,
        pod_dir,
        ports,
        args.vite_host.clone(),
        Vec::new(),
    )?;

    let cmd = omnigent_cmd::build(&pod, &passthrough);
    omnigent_cmd::run(cmd)
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
    // LAN origins are keyed to the resolved Vite port, so compute them here
    // once the port is known. Empty unless `--trust-lan-origins` is set.
    let trusted_origins = if args.trust_lan_origins {
        lan::trusted_lan_origins(ports.vite)
    } else {
        Vec::new()
    };
    let pod = Arc::new(Pod::create(
        repo_root,
        pod_dir,
        ports,
        args.vite_host,
        trusted_origins,
    )?);

    let shared = Shared::new(&pod);
    let (cmd_tx, cmd_rx) = mpsc::unbounded_channel::<Cmd>();

    // File watcher: Python changes -> Reload commands. Keep the debouncer alive
    // for the whole session.
    let _watcher = watcher::spawn(
        &pod.repo_root,
        &pod.omnigent_dir(),
        shared.clone(),
        args.debug,
        cmd_tx.clone(),
    )?;

    // Supervisor runs on the tokio runtime; the TUI drives it via cmd_tx.
    let supervisor = Supervisor::new(
        pod.clone(),
        shared.clone(),
        !args.no_vite,
        args.trust_lan_origins,
    );
    let sup_handle = tokio::spawn(supervisor.run(cmd_rx));

    // Run the TUI (owns the terminal) until the user quits.
    let app = tui::App::new(pod.clone(), shared.clone(), cmd_tx.clone());
    let result = app.run().await;

    // Tear down children, then wait for the supervisor to finish shutdown.
    let _ = cmd_tx.send(Cmd::Shutdown);
    let _ = sup_handle.await;

    result
}
