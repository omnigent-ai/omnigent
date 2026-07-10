//! Shared state between the supervisor and the TUI.

use std::sync::{Arc, Mutex};

use crate::logs::LogBuffer;
use crate::pod::Pod;

/// The three supervised processes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcId {
    Server,
    Host,
    Vite,
}

impl ProcId {
    pub const ALL: [ProcId; 3] = [ProcId::Server, ProcId::Host, ProcId::Vite];

    pub fn idx(self) -> usize {
        match self {
            ProcId::Server => 0,
            ProcId::Host => 1,
            ProcId::Vite => 2,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            ProcId::Server => "server",
            ProcId::Host => "host",
            ProcId::Vite => "vite",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProcStatus {
    Idle,
    Starting,
    Running(u32),
    Restarting,
    Crashed,
    Stopped,
}

impl ProcStatus {
    pub fn short(&self) -> &'static str {
        match self {
            ProcStatus::Idle => "idle",
            ProcStatus::Starting => "starting",
            ProcStatus::Running(_) => "running",
            ProcStatus::Restarting => "restarting",
            ProcStatus::Crashed => "crashed",
            ProcStatus::Stopped => "stopped",
        }
    }
}

/// State the TUI renders and the supervisor mutates. Guarded by a std mutex;
/// locks are held only for the duration of a single push/read.
pub struct Shared {
    pub status: [ProcStatus; 3],
    pub server: LogBuffer,
    pub host: LogBuffer,
    pub vite: LogBuffer,
    /// Combined, source-tagged view — also receives supervisor events.
    pub all: LogBuffer,
}

impl Shared {
    pub fn new(pod: &Pod) -> Arc<Mutex<Shared>> {
        Arc::new(Mutex::new(Shared {
            status: [ProcStatus::Idle, ProcStatus::Idle, ProcStatus::Idle],
            server: LogBuffer::new(&pod.log_file("server")),
            host: LogBuffer::new(&pod.log_file("host")),
            vite: LogBuffer::new(&pod.log_file("vite")),
            all: LogBuffer::memory(),
        }))
    }

    fn buf_mut(&mut self, id: ProcId) -> &mut LogBuffer {
        match id {
            ProcId::Server => &mut self.server,
            ProcId::Host => &mut self.host,
            ProcId::Vite => &mut self.vite,
        }
    }

    pub fn buf(&self, id: ProcId) -> &LogBuffer {
        match id {
            ProcId::Server => &self.server,
            ProcId::Host => &self.host,
            ProcId::Vite => &self.vite,
        }
    }

    /// Append a line from a process: goes to its own pane and the combined view.
    pub fn log_proc(&mut self, id: ProcId, line: String) {
        self.all.push(format!("[{}] {}", id.label(), line));
        self.buf_mut(id).push(line);
    }

    /// Append a supervisor event (starts, restarts, crashes, reloads).
    pub fn event(&mut self, line: impl Into<String>) {
        self.all.push(format!("[omnidev] {}", line.into()));
    }

    pub fn set_status(&mut self, id: ProcId, status: ProcStatus) {
        self.status[id.idx()] = status;
    }
}
