import Foundation

/// A parsed `omnigent://<hostname>/c/<session_id>` deep link — the iOS analog
/// of the desktop shell's `parseOmnigentDeepLink` (web/electron/src/deepLink.js),
/// kept pure so it unit-tests without a WKWebView. See designs/desktop-deep-link.md
/// for the shared design (URL shape, scheme inference, security rationale).
///
/// The link names a server by **host** (with port if non-default) and a
/// conversation by the SPA's own `/c/:id` route. It carries no `http`/`https` —
/// the scheme is inferred with the same rule the setup page and desktop use
/// (`http` for loopback, `https` for a remote host), so a deep link and a pasted
/// URL never disagree on scheme. The Databricks workspace mount (`/ml/omnigents`)
/// is deliberately NOT in the link; it is server-determined and discovered by
/// `WorkspaceURLExpander` (after consent, for an unknown server).
struct DeepLink: Equatable {
  /// The inferred http(s) origin with NO trailing slash, e.g. `"http://localhost:8000"`
  /// or `"https://my-workspace.cloud.databricks.com"`. Built manually (not via
  /// `URL.omnigentOrigin`, which returns nil for IPv6 hosts) so IPv6 loopback
  /// links still produce a valid `http://[::1]:<port>` origin.
  let origin: String

  /// The basename-less SPA conversation path, e.g. `"/c/conv_abc"`. Foundation
  /// strips a trailing slash from `URL.path`, so this is always `/c/<id>` with
  /// no trailing slash — the shape the SPA's react-router matches.
  let path: String

  /// Hostnames that resolve to the local machine — default to `http` for these
  /// (local dev is plain http, and ATS exempts loopback), `https` for everything
  /// else. Mirrors the desktop shell's `LOCAL_HOSTS` / `defaultSchemeFor` so the
  /// two shells never disagree on what a deep link to `localhost` means.
  private static let localHosts: Set<String> = ["localhost", "127.0.0.1", "::1", "[::1]"]

  /// Parse an `omnigent://` URL. Returns nil for anything that isn't a valid
  /// `omnigent://<host>/c/<id>` link (wrong scheme, no host, non-`/c/` path,
  /// empty/nested id, unparseable input) — an unrecognized deep link must never
  /// crash or mis-navigate.
  static func parse(_ raw: URL) -> DeepLink? {
    guard raw.scheme?.lowercased() == "omnigent" else { return nil }
    guard let host = raw.host, !host.isEmpty else { return nil }

    // v1 accepts only `/c/<id>`: a single path segment after `/c/`, with no
    // nested path. Foundation already strips a trailing slash, so `raw.path`
    // is `/c/<id>`; the manual check also tolerates a trailing slash in case a
    // future iOS version keeps it. Anything else (other routes, nested paths,
    // empty id) is dropped — the SPA's own router stays the authority on ids.
    let path = raw.path
    guard path.hasPrefix("/c/") else { return nil }
    var id = path.dropFirst(3)
    if id.hasSuffix("/") { id = id.dropLast() }
    guard !id.isEmpty, !id.contains("/") else { return nil }

    let scheme = defaultScheme(for: host)
    guard let origin = makeOrigin(scheme: scheme, host: host, port: raw.port) else { return nil }
    return DeepLink(origin: origin, path: "/c/\(id)")
  }

  /// Infer `http` for loopback hosts, `https` otherwise — matching the setup
  /// page and the desktop shell, and aligned with iOS App Transport Security
  /// (loopback http is exempt; remote http would be blocked in release anyway).
  private static func defaultScheme(for host: String) -> String {
    localHosts.contains(host.lowercased()) ? "http" : "https"
  }

  /// Build an origin string `scheme://host[:port]` with NO trailing slash,
  /// bracketing IPv6 hosts (whose `URL.host` comes back without brackets).
  private static func makeOrigin(scheme: String, host: String, port: Int?) -> String? {
    let hostPart = host.contains(":") ? "[\(host)]" : host
    if let port {
      return URL(string: "\(scheme)://\(hostPart):\(port)")?.absoluteString
        .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }
    return URL(string: "\(scheme)://\(hostPart)")?.absoluteString
      .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
  }
}
