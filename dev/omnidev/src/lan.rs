//! LAN origin discovery for device testing.
//!
//! When Vite binds to `0.0.0.0` (`--vite-host 0.0.0.0`), a phone or tablet on
//! the same network loads the UI at `http://<lan-ip>:<vite-port>`. Its browser
//! stamps that non-loopback address as the `Origin` on every request. The
//! backend runs in local single-user mode, where the origin guard
//! (`omnigent.server.ws_origin.origin_allowed`) admits only loopback origins —
//! so multipart uploads get a 403 and the WebSocket stream is refused.
//!
//! `--trust-lan-origins` closes that gap by enumerating this machine's LAN
//! IPv4 addresses and handing the server the matching `http://<ip>:<port>`
//! origins via `OMNIGENT_WS_ALLOWED_ORIGINS` — the server's own exact-match
//! allowlist. It stays exact-match (no security disable): only the origins we
//! name are trusted.

use std::net::Ipv4Addr;

/// Whether an IPv4 address is a usable LAN address to trust as an origin.
///
/// Keeps private (RFC 1918) and link-local (169.254/16) addresses — the ones a
/// device on the same network actually reaches this machine by. Drops loopback
/// (already trusted), unspecified (`0.0.0.0`), broadcast, documentation, and
/// multicast, none of which a real device browses to.
fn is_lan_ipv4(ip: &Ipv4Addr) -> bool {
    (ip.is_private() || ip.is_link_local())
        && !ip.is_loopback()
        && !ip.is_unspecified()
        && !ip.is_broadcast()
        && !ip.is_multicast()
}

/// Build the `http://<ip>:<port>` origins to trust for a given set of LAN
/// IPv4 addresses.
///
/// Split out from interface enumeration so the origin-shaping (which is all we
/// assert on) is testable without touching the host's real interfaces. The
/// input is deduplicated and the output is sorted for a stable env value.
fn origins_for_ips(ips: impl IntoIterator<Item = Ipv4Addr>, vite_port: u16) -> Vec<String> {
    let mut origins: Vec<String> = ips
        .into_iter()
        .filter(is_lan_ipv4)
        .map(|ip| format!("http://{ip}:{vite_port}"))
        .collect();
    origins.sort();
    origins.dedup();
    origins
}

/// Discover the `http://<lan-ip>:<vite-port>` origins for this machine's LAN
/// interfaces.
///
/// Returns an empty vector when no LAN interface is found (e.g. offline) — the
/// caller then simply trusts nothing extra rather than failing. Interface
/// enumeration errors are treated the same way: LAN trust is a convenience, so
/// a lookup failure must not block the pod from starting.
pub fn trusted_lan_origins(vite_port: u16) -> Vec<String> {
    let ips = match if_addrs::get_if_addrs() {
        Ok(ifaces) => ifaces
            .into_iter()
            .filter_map(|iface| match iface.addr.ip() {
                std::net::IpAddr::V4(v4) => Some(v4),
                std::net::IpAddr::V6(_) => None,
            }),
        Err(_) => return Vec::new(),
    };
    origins_for_ips(ips, vite_port)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_private_and_link_local_drops_loopback_and_public() {
        assert!(is_lan_ipv4(&Ipv4Addr::new(192, 168, 1, 42)));
        assert!(is_lan_ipv4(&Ipv4Addr::new(10, 0, 0, 5)));
        assert!(is_lan_ipv4(&Ipv4Addr::new(172, 16, 3, 9)));
        assert!(is_lan_ipv4(&Ipv4Addr::new(169, 254, 10, 1)));

        assert!(!is_lan_ipv4(&Ipv4Addr::new(127, 0, 0, 1)));
        assert!(!is_lan_ipv4(&Ipv4Addr::new(0, 0, 0, 0)));
        assert!(!is_lan_ipv4(&Ipv4Addr::new(8, 8, 8, 8)));
        assert!(!is_lan_ipv4(&Ipv4Addr::new(255, 255, 255, 255)));
    }

    #[test]
    fn builds_http_origins_with_the_vite_port() {
        let origins = origins_for_ips([Ipv4Addr::new(192, 168, 1, 42)], 5173);
        assert_eq!(origins, vec!["http://192.168.1.42:5173"]);
    }

    #[test]
    fn filters_and_sorts_and_dedups() {
        let origins = origins_for_ips(
            [
                Ipv4Addr::new(10, 0, 0, 9),
                Ipv4Addr::new(127, 0, 0, 1), // loopback dropped
                Ipv4Addr::new(8, 8, 8, 8),   // public dropped
                Ipv4Addr::new(192, 168, 1, 5),
                Ipv4Addr::new(10, 0, 0, 9), // duplicate collapsed
            ],
            8080,
        );
        assert_eq!(
            origins,
            vec!["http://10.0.0.9:8080", "http://192.168.1.5:8080"]
        );
    }

    #[test]
    fn no_lan_interfaces_yields_no_origins() {
        assert!(origins_for_ips([Ipv4Addr::new(127, 0, 0, 1)], 5173).is_empty());
    }
}
