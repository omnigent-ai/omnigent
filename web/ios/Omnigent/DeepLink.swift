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
///
/// ## Custom-scheme vs Universal Links
///
/// `omnigent://` is a **custom URL scheme** registered via `CFBundleURLSchemes`
/// in the Info plist. iOS does NOT verify single ownership of a custom scheme:
/// any other installed app can also declare `omnigent` and win the launch race,
/// receiving the raw link — and with it the server host and conversation id
/// (metadata disclosure) — before this app ever sees it. Treat the custom
/// scheme as untrusted for that reason.
///
/// For **managed Databricks domains** (known, HTTPS, operator-controlled hosts
/// that CAN serve an `apple-app-site-association` file), prefer **Universal
/// Links** (`applinks:<host>` in the app's Associated Domains Entitlement,
/// backed by a verified `apple-app-site-association` on the domain). Universal
/// Links are cryptographically pinned to the domain, cannot be hijacked by a
/// co-installed app, and open from a plain `https://…/c/<id>` URL with no custom
/// scheme at all — eliminating the interception risk entirely. Wiring that
/// up is out of scope for this parser (it concerns the SPA's link emission +
/// the app's entitlements/AASA serving, not parsing), but any link source that
/// CAN use a Universal Link should.
///
/// The custom scheme is retained for **BYO / OSS self-hosted servers** whose
/// operators cannot host an `apple-app-site-association` file — loopback hosts,
/// LAN IPs, and personal domains without verified HTTPS. For those, document
/// the interception risk to users: assume a malicious co-installed app may read
/// the server host and conversation id from a clicked `omnigent://` link. The
/// id carries no secret (it is a server-assigned UUID; access is still gated by
/// the server's own auth), but the host does reveal which server the user runs.
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

  /// Characters/sequences that must NEVER appear in a conversation id segment
  /// — because they smuggle URL structure past the intended "/c/<id> only"
  /// shape, enable path traversal, or signal a malformed percent-escape. This
  /// is a DENYLIST, not a grammar: it deliberately does NOT assume any specific
  /// id format (the server's ids are bare 32-hex uuids today, but the SPA's own
  /// `/c/:id` route accepts any non-slash segment, and a future id scheme —
  /// ULID, nanoid, base64 — must not be silently rejected by this parser). The
  /// SPA stays the authority on what a valid id IS; this parser only stops a
  /// malformed link from reaching it with smuggled structure.
  ///
  /// `URL.path` is percent-DECODED, so an encoded separator reappears here as a
  /// literal and is caught: `%3F`→`?`, `%23`→`#`, `%2F`→`/`, `%2E`→`.`,
  /// `%00`/`%0A`/`%7F`→control chars. A malformed escape (`%zz`) leaves a stray
  /// `%` literal, also caught.
  private static let blockedIdCharacters: Set<Character> = [
    "?",  // smuggles a query string
    "#",  // smuggles a fragment
    "/",  // nested path / encoded path separator
    ".",  // "."/".." path traversal; not in any current id format
    "%",  // stray percent from a malformed escape or residual encoding
  ]

  /// True iff `id` contains no character that smuggles URL structure or
  /// enables traversal. See `blockedIdCharacters` for why this is a denylist
  /// (not a format assumption) and `parse` for the smuggling rationale.
  private static func isValidConversationId(_ id: Substring) -> Bool {
    // Control characters (U+0000–U+001F, U+007F DEL) — never in an id; an
    // encoded one (e.g. `%00`, `%0A`) decodes into a literal here.
    for scalar in id.unicodeScalars {
      let v = scalar.value
      if v <= 0x1F || v == 0x7F { return false }
    }
    return !id.contains { blockedIdCharacters.contains($0) }
  }

  /// Hostnames that resolve to the local machine — default to `http` for these
  /// (local dev is plain http, and ATS exempts loopback), `https` for everything
  /// else. Mirrors the desktop shell's `LOCAL_HOSTS` / `defaultSchemeFor` so the
  /// two shells never disagree on what a deep link to `localhost` means.
  private static let localHosts: Set<String> = ["localhost", "127.0.0.1", "::1", "[::1]"]

  /// Parse an `omnigent://` URL. Returns nil for anything that isn't a valid
  /// `omnigent://<host>/c/<id>` link (wrong scheme, no host, non-`/c/` path,
  /// empty/nested id, an id that isn't a real conversation id, or unparseable
  /// input) — an unrecognized deep link must never crash or mis-navigate.
  static func parse(_ raw: URL) -> DeepLink? {
    guard raw.scheme?.lowercased() == "omnigent" else { return nil }
    guard let host = raw.host, !host.isEmpty else { return nil }

    // v1 accepts only `/c/<id>`. CRUCIALLY, `URL.path` returns the
    // PERCENT-DECODED path, so a link like `omnigent://host/c/id%3Fview=terminal`
    // exposes `?` as a LITERAL character in `path` (and `%23` → `#`,
    // `%2F` → `/`, `%2E` → `.`, `%00`/`%0A`/`%7F` → control chars). The old
    // check rejected only a literal `/`, so an encoded `?` or `#` rode inside
    // the id and then acted as a real query/fragment separator once the
    // rebuilt `/c/<id>` was forwarded to the SPA — smuggling an attacker-chosen
    // query/fragment past the intended "/c/<id> only" shape.
    //
    // The fix is a DENYLIST (see `blockedIdCharacters`), not a grammar: it
    // rejects characters that smuggle structure (`?`, `#`), enable traversal
    // (`.`, `..`), or signal a malformed escape (`%`) — plus any control char
    // — so an encoded separator that `URL.path` decoded into one of those is
    // dropped. It deliberately does NOT assume the id's exact format (the
    // server emits 32-hex uuids today, but the SPA's `/c/:id` route accepts any
    // non-slash segment, and a future id scheme must not be silently rejected);
    // the SPA's own router stays the authority on what a valid id IS. This
    // parser only stops a malformed link from reaching it with smuggled
    // structure.
    let path = raw.path
    guard path.hasPrefix("/c/") else { return nil }
    var id = path.dropFirst(3)
    if id.hasSuffix("/") { id = id.dropLast() }
    guard !id.isEmpty, !id.contains("/") else { return nil }
    guard isValidConversationId(id) else { return nil }

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
