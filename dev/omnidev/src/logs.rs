//! Per-process bounded log buffers with write-through to disk.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;

const MAX_LINES: usize = 5000;

/// A bounded ring buffer of log lines for one channel, mirrored to a file so
/// the full session output survives for later inspection (`tail`, editor).
pub struct LogBuffer {
    lines: VecDeque<String>,
    file: Option<File>,
    /// Monotonic count of lines ever appended — lets panes detect growth for
    /// follow-tail without diffing the buffer.
    pub total: u64,
}

impl LogBuffer {
    pub fn new(path: &Path) -> Self {
        let file = OpenOptions::new().create(true).append(true).open(path).ok();
        LogBuffer {
            lines: VecDeque::with_capacity(MAX_LINES),
            file,
            total: 0,
        }
    }

    /// In-memory only channel (e.g. the synthetic "omnidev" event log).
    pub fn memory() -> Self {
        LogBuffer {
            lines: VecDeque::with_capacity(256),
            file: None,
            total: 0,
        }
    }

    pub fn push(&mut self, line: impl Into<String>) {
        let line = line.into();
        if let Some(f) = self.file.as_mut() {
            let _ = writeln!(f, "{line}");
        }
        if self.lines.len() == MAX_LINES {
            self.lines.pop_front();
        }
        self.lines.push_back(line);
        self.total = self.total.saturating_add(1);
    }

    pub fn clear(&mut self) {
        self.lines.clear();
    }

    pub fn iter(&self) -> impl Iterator<Item = &String> {
        self.lines.iter()
    }
}
