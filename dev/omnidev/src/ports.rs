//! Free-port probing and per-pod persistence.

use std::collections::HashSet;
use std::net::TcpListener;
use std::path::Path;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

pub const SERVER_PORT_BASE: u16 = 6767;
pub const VITE_PORT_BASE: u16 = 5173;

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct Ports {
    pub server: u16,
    pub vite: u16,
}

impl Ports {
    /// Resolve the pod's ports: reuse the persisted pair if still available,
    /// else probe upward from the preferred bases. Explicit overrides (from CLI
    /// flags) are honored verbatim.
    ///
    /// A port is "available" only if it both binds right now *and* isn't already
    /// claimed by another pod. The bind check alone is racy: `resolve()` runs at
    /// startup, before children spawn, so a peer pod whose server/vite hasn't
    /// bound yet would leave the base port looking free and two pods would pick
    /// it. We read sibling pods' persisted `pod.toml` to skip ports they've
    /// already claimed, which is timing-independent.
    pub fn resolve(
        pod_dir: &Path,
        server_override: Option<u16>,
        vite_override: Option<u16>,
    ) -> Result<Ports> {
        let persisted = load(pod_dir);
        let mut taken = sibling_claims(pod_dir);

        let server = match server_override {
            Some(p) => p,
            None => {
                let reuse = persisted
                    .map(|p| p.server)
                    .filter(|&p| available(p, &taken));
                reuse
                    .map(Ok)
                    .unwrap_or_else(|| probe_from(SERVER_PORT_BASE, &taken))?
            }
        };
        // The server port is now spoken for — don't hand the same number to vite.
        taken.insert(server);

        let vite = match vite_override {
            Some(p) => p,
            None => {
                let reuse = persisted.map(|p| p.vite).filter(|&p| available(p, &taken));
                reuse
                    .map(Ok)
                    .unwrap_or_else(|| probe_from(VITE_PORT_BASE, &taken))?
            }
        };

        let ports = Ports { server, vite };
        save(pod_dir, &ports)?;
        Ok(ports)
    }
}

/// A port is usable if it isn't already claimed by a sibling pod and binds now.
fn available(port: u16, taken: &HashSet<u16>) -> bool {
    !taken.contains(&port) && is_free(port)
}

/// True if the port can be bound on loopback right now.
fn is_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// First available port at or above `base`, skipping sibling-claimed ports.
fn probe_from(base: u16, taken: &HashSet<u16>) -> Result<u16> {
    for port in base..=u16::MAX {
        if available(port, taken) {
            return Ok(port);
        }
    }
    anyhow::bail!("no free port at or above {base}")
}

/// Ports claimed in other pods' `pod.toml` under the shared omnidev cache root.
/// Best-effort: unreadable/oddly-nested pod dirs just contribute nothing.
fn sibling_claims(pod_dir: &Path) -> HashSet<u16> {
    let mut claimed = HashSet::new();
    let Some(root) = pod_dir.parent() else {
        return claimed;
    };
    let Ok(entries) = std::fs::read_dir(root) else {
        return claimed;
    };
    for entry in entries.flatten() {
        let dir = entry.path();
        if dir == pod_dir || !dir.is_dir() {
            continue;
        }
        if let Some(p) = load(&dir) {
            claimed.insert(p.server);
            claimed.insert(p.vite);
        }
    }
    claimed
}

fn persist_path(pod_dir: &Path) -> std::path::PathBuf {
    pod_dir.join("pod.toml")
}

fn load(pod_dir: &Path) -> Option<Ports> {
    let text = std::fs::read_to_string(persist_path(pod_dir)).ok()?;
    toml::from_str(&text).ok()
}

fn save(pod_dir: &Path, ports: &Ports) -> Result<()> {
    let text = toml::to_string(ports).context("serializing pod.toml")?;
    std::fs::write(persist_path(pod_dir), text).context("writing pod.toml")?;
    Ok(())
}
