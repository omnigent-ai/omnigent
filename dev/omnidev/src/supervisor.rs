//! Process supervision: spawn/stop/restart the three children, capture their
//! output, and recover from crashes.

use std::collections::HashSet;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::time::{sleep, timeout};

use crate::pod::Pod;
use crate::process::ProcSpec;
use crate::state::{ProcId, ProcStatus, Shared};

/// Commands the TUI (and watcher) send to the supervisor.
#[derive(Debug, Clone)]
pub enum Cmd {
    /// Restart a single process.
    Restart(ProcId),
    /// Restart the backend pair: server, then host after `/health`.
    RestartBackend,
    /// A backend reload triggered by `n` changed Python files.
    Reload(usize),
    /// Tear everything down and stop the supervisor loop.
    Shutdown,
}

/// Reported by a per-child monitor when the child exits.
struct Exit {
    id: ProcId,
    generation: u64,
    status: String,
}

struct Slot {
    /// Group id (== leader pid) of the currently-running child, if any.
    pgid: Option<i32>,
    /// Generation of the current child; bumped on each spawn.
    generation: u64,
    /// Consecutive crash count for backoff; reset after a stable run.
    crashes: u32,
    started: Instant,
}

impl Default for Slot {
    fn default() -> Self {
        Slot {
            pgid: None,
            generation: 0,
            crashes: 0,
            started: Instant::now(),
        }
    }
}

pub struct Supervisor {
    pod: Arc<Pod>,
    shared: Arc<Mutex<Shared>>,
    env: Vec<(String, String)>,
    vite_enabled: bool,
    /// Whether `--trust-lan-origins` was requested, so we can warn if it was
    /// asked for but no LAN interface turned up any origins to trust.
    trust_lan_origins: bool,
    slots: [Slot; 3],
    /// Generations we stopped on purpose — their exits are not crashes.
    expected_stops: HashSet<(usize, u64)>,
    gen_counter: u64,
    exit_tx: mpsc::UnboundedSender<Exit>,
    exit_rx: mpsc::UnboundedReceiver<Exit>,
}

impl Supervisor {
    pub fn new(
        pod: Arc<Pod>,
        shared: Arc<Mutex<Shared>>,
        vite_enabled: bool,
        trust_lan_origins: bool,
    ) -> Supervisor {
        let env = pod.env();
        let (exit_tx, exit_rx) = mpsc::unbounded_channel();
        Supervisor {
            pod,
            shared,
            env,
            vite_enabled,
            trust_lan_origins,
            slots: Default::default(),
            expected_stops: HashSet::new(),
            gen_counter: 0,
            exit_tx,
            exit_rx,
        }
    }

    fn event(&self, msg: impl Into<String>) {
        self.shared.lock().unwrap().event(msg);
    }

    fn set_status(&self, id: ProcId, status: ProcStatus) {
        self.shared.lock().unwrap().set_status(id, status);
    }

    /// Main loop: bring everything up, then service commands and child exits
    /// until `Shutdown`.
    pub async fn run(mut self, mut cmds: mpsc::UnboundedReceiver<Cmd>) {
        self.event(format!(
            "pod {} — server :{} vite :{}",
            self.pod.dir.display(),
            self.pod.ports.server,
            self.pod.ports.vite
        ));
        if !self.pod.trusted_origins.is_empty() {
            self.event(format!(
                "trusting LAN origins for device testing: {}",
                self.pod.trusted_origins.join(", ")
            ));
        } else if self.trust_lan_origins {
            self.event("--trust-lan-origins: no LAN interface found; no extra origins trusted");
        }

        self.start_backend().await;
        if self.vite_enabled {
            self.prepare_vite().await;
            self.spawn(ProcId::Vite);
        }

        loop {
            tokio::select! {
                cmd = cmds.recv() => {
                    match cmd {
                        Some(Cmd::Restart(id)) => self.restart_one(id).await,
                        Some(Cmd::RestartBackend) => {
                            self.event("manual backend restart");
                            self.start_backend_restart().await;
                        }
                        Some(Cmd::Reload(n)) => {
                            self.event(format!("reloading backend ({n} file(s) changed)"));
                            self.start_backend_restart().await;
                        }
                        Some(Cmd::Shutdown) | None => {
                            self.shutdown().await;
                            return;
                        }
                    }
                }
                Some(exit) = self.exit_rx.recv() => {
                    self.on_exit(exit).await;
                }
            }
        }
    }

    async fn start_backend(&mut self) {
        self.spawn(ProcId::Server);
        if self.wait_healthy().await {
            self.spawn(ProcId::Host);
        } else {
            self.event("server did not become healthy; host not started");
        }
    }

    /// Restart server then host, gated on `/health`. Used by manual restart and
    /// by the reload path.
    async fn start_backend_restart(&mut self) {
        self.stop(ProcId::Host).await;
        self.stop(ProcId::Server).await;
        self.set_status(ProcId::Server, ProcStatus::Restarting);
        self.set_status(ProcId::Host, ProcStatus::Restarting);
        self.spawn(ProcId::Server);
        if self.wait_healthy().await {
            self.spawn(ProcId::Host);
        } else {
            self.event("server did not become healthy after restart");
        }
    }

    async fn restart_one(&mut self, id: ProcId) {
        match id {
            // Restarting the server alone would strand the host on a dead
            // backend, so treat it as a backend restart.
            ProcId::Server | ProcId::Host => self.start_backend_restart().await,
            ProcId::Vite => {
                if self.vite_enabled {
                    self.event("restarting vite");
                    self.stop(ProcId::Vite).await;
                    self.prepare_vite().await;
                    self.spawn(ProcId::Vite);
                }
            }
        }
    }

    fn spec(&self, id: ProcId) -> ProcSpec {
        match id {
            ProcId::Server => ProcSpec::server(&self.pod),
            ProcId::Host => ProcSpec::host(&self.pod),
            ProcId::Vite => ProcSpec::vite(&self.pod),
        }
    }

    /// Spawn a child in its own process group and wire up output + exit monitor.
    fn spawn(&mut self, id: ProcId) {
        let spec = self.spec(id);
        self.set_status(id, ProcStatus::Starting);

        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args)
            .current_dir(&spec.cwd)
            .envs(self.env.iter().cloned())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(false);
        // Become a session/group leader so we can signal the whole tree
        // (uvicorn workers, npm -> vite children) via the negative pgid.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                self.shared
                    .lock()
                    .unwrap()
                    .log_proc(id, format!("failed to spawn {}: {e}", spec.program));
                self.set_status(id, ProcStatus::Crashed);
                return;
            }
        };

        let pid = child.id().map(|p| p as i32);
        self.gen_counter += 1;
        let generation = self.gen_counter;
        let slot = &mut self.slots[id.idx()];
        slot.pgid = pid;
        slot.generation = generation;
        slot.started = Instant::now();

        if let Some(p) = pid {
            self.set_status(id, ProcStatus::Running(p as u32));
        }

        // Merge stdout + stderr into this process's buffer.
        if let Some(out) = child.stdout.take() {
            self.pump(id, out);
        }
        if let Some(err) = child.stderr.take() {
            self.pump(id, err);
        }

        // Monitor: report the exit so the loop can decide crash vs expected.
        let tx = self.exit_tx.clone();
        tokio::spawn(async move {
            let status = match child.wait().await {
                Ok(s) => s.to_string(),
                Err(e) => format!("wait error: {e}"),
            };
            let _ = tx.send(Exit {
                id,
                generation,
                status,
            });
        });
    }

    /// Run `npm install` to completion before Vite starts, but only when deps
    /// are missing or stale — otherwise Vite's dependency scan fails on an
    /// unresolved import (e.g. a dep added to package.json but not installed).
    /// Output streams into the Vite pane. A failed/absent install is logged but
    /// non-fatal: we still let Vite try, so a transient npm hiccup doesn't block
    /// the whole session.
    async fn prepare_vite(&self) {
        if !self.pod.needs_npm_install() {
            return;
        }
        self.set_status(ProcId::Vite, ProcStatus::Starting);
        self.shared.lock().unwrap().log_proc(
            ProcId::Vite,
            "web deps missing or stale — running npm install".into(),
        );

        let spec = ProcSpec::npm_install(&self.pod);
        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args)
            .current_dir(&spec.cwd)
            .envs(self.env.iter().cloned())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                self.shared
                    .lock()
                    .unwrap()
                    .log_proc(ProcId::Vite, format!("failed to run npm install: {e}"));
                return;
            }
        };
        if let Some(out) = child.stdout.take() {
            self.pump(ProcId::Vite, out);
        }
        if let Some(err) = child.stderr.take() {
            self.pump(ProcId::Vite, err);
        }

        // `--loglevel http` streams a line per package fetch, but npm still
        // goes quiet during the final tree-build/link phase. A slow heartbeat
        // covers those gaps so the pane never looks frozen.
        let started = Instant::now();
        let mut heartbeat = tokio::time::interval(Duration::from_secs(5));
        heartbeat.tick().await; // the first tick fires immediately; skip it
        let status = loop {
            tokio::select! {
                result = child.wait() => break result,
                _ = heartbeat.tick() => {
                    let secs = started.elapsed().as_secs();
                    self.shared
                        .lock()
                        .unwrap()
                        .log_proc(ProcId::Vite, format!("… npm install running ({secs}s)"));
                }
            }
        };
        match status {
            Ok(s) if s.success() => self.event(format!(
                "npm install complete ({}s)",
                started.elapsed().as_secs()
            )),
            Ok(s) => self.event(format!("npm install exited {s} — starting Vite anyway")),
            Err(e) => self.event(format!("npm install wait error: {e}")),
        }
    }

    /// Spawn a task that streams one pipe into the shared buffer, line by line.
    fn pump<R>(&self, id: ProcId, reader: R)
    where
        R: tokio::io::AsyncRead + Unpin + Send + 'static,
    {
        let shared = self.shared.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                shared.lock().unwrap().log_proc(id, line);
            }
        });
    }

    /// SIGTERM the process group, wait briefly, then SIGKILL. Marks the current
    /// generation as an expected stop so its exit is not counted as a crash.
    async fn stop(&mut self, id: ProcId) {
        let (pgid, generation) = {
            let slot = &self.slots[id.idx()];
            (slot.pgid, slot.generation)
        };
        let Some(pgid) = pgid else {
            self.set_status(id, ProcStatus::Stopped);
            return;
        };
        self.expected_stops.insert((id.idx(), generation));

        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
        // Give the tree up to ~5s to exit on SIGTERM.
        for _ in 0..50 {
            if unsafe { libc::kill(-pgid, 0) } != 0 {
                break;
            }
            sleep(Duration::from_millis(100)).await;
        }
        if unsafe { libc::kill(-pgid, 0) } == 0 {
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
        }
        self.slots[id.idx()].pgid = None;
        self.set_status(id, ProcStatus::Stopped);
    }

    /// Handle a child exit: distinguish an expected stop from a crash and
    /// schedule a backoff restart for crashes.
    async fn on_exit(&mut self, exit: Exit) {
        let key = (exit.id.idx(), exit.generation);
        if self.expected_stops.remove(&key) {
            return; // we stopped it on purpose
        }
        // Ignore exits from a generation we already replaced.
        if self.slots[exit.id.idx()].generation != exit.generation {
            return;
        }

        self.slots[exit.id.idx()].pgid = None;
        self.set_status(exit.id, ProcStatus::Crashed);
        self.event(format!(
            "{} exited unexpectedly ({})",
            exit.id.label(),
            exit.status
        ));

        // Reset the crash counter if the process had been stable for a while.
        let crashes = {
            let slot = &mut self.slots[exit.id.idx()];
            if slot.started.elapsed() > Duration::from_secs(20) {
                slot.crashes = 0;
            }
            slot.crashes += 1;
            slot.crashes
        };
        let backoff = backoff_secs(crashes);
        self.event(format!(
            "restarting {} in {backoff}s (attempt {crashes})",
            exit.id.label(),
        ));
        sleep(Duration::from_secs(backoff)).await;

        // A server crash takes the host with it — restart the pair.
        match exit.id {
            ProcId::Server => self.start_backend_restart().await,
            ProcId::Host => {
                if self.wait_healthy().await {
                    self.spawn(ProcId::Host);
                } else {
                    self.start_backend_restart().await;
                }
            }
            ProcId::Vite => {
                if self.vite_enabled {
                    self.spawn(ProcId::Vite);
                }
            }
        }
    }

    /// Poll the server's `/health` until it returns 200 (up to ~30s).
    async fn wait_healthy(&self) -> bool {
        let addr = format!("127.0.0.1:{}", self.pod.ports.server);
        for _ in 0..120 {
            if health_ok(&addr).await {
                return true;
            }
            sleep(Duration::from_millis(250)).await;
        }
        false
    }

    async fn shutdown(&mut self) {
        self.event("shutting down");
        self.stop(ProcId::Host).await;
        self.stop(ProcId::Vite).await;
        self.stop(ProcId::Server).await;
    }
}

fn backoff_secs(attempt: u32) -> u64 {
    // 0.5s effectively rounds to 1s here; cap at 30s.
    match attempt {
        0 | 1 => 1,
        2 => 2,
        3 => 4,
        4 => 8,
        5 => 16,
        _ => 30,
    }
}

/// Minimal HTTP/1.0 `GET /health` returning true on a `200` status line. Avoids
/// pulling an HTTP client dependency just for a readiness probe.
async fn health_ok(addr: &str) -> bool {
    let Ok(Ok(mut stream)) = timeout(Duration::from_secs(1), TcpStream::connect(addr)).await else {
        return false;
    };
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let req = format!("GET /health HTTP/1.0\r\nHost: {addr}\r\n\r\n");
    if stream.write_all(req.as_bytes()).await.is_err() {
        return false;
    }
    let mut buf = [0u8; 128];
    let Ok(Ok(n)) = timeout(Duration::from_secs(1), stream.read(&mut buf)).await else {
        return false;
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.") && head.contains(" 200")
}
