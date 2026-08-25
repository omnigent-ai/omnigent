import Foundation

enum ServerURLError: LocalizedError, Equatable {
  case empty
  case invalid(String)
  case unsupportedScheme(String)
  case insecureHTTPNotAllowed

  var errorDescription: String? {
    switch self {
    case .empty:
      "Server URL is empty."
    case .invalid(let message):
      "Invalid URL: \(message)"
    case .unsupportedScheme(let scheme):
      "Unsupported scheme '\(scheme)'. Use https."
    case .insecureHTTPNotAllowed:
      "iOS release builds require https:// server URLs."
    }
  }
}

enum ServerURL {
  static func normalize(_ raw: String, allowsInsecureHTTP: Bool) throws -> URL {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { throw ServerURLError.empty }

    let withScheme: String
    if trimmed.contains("://") {
      withScheme = trimmed
    } else {
      withScheme =
        "\(defaultScheme(for: trimmed, allowsInsecureHTTP: allowsInsecureHTTP))://\(trimmed)"
    }

    guard let url = URL(string: withScheme), let scheme = url.scheme?.lowercased() else {
      throw ServerURLError.invalid(withScheme)
    }
    guard scheme == "http" || scheme == "https" else {
      throw ServerURLError.unsupportedScheme(scheme)
    }
    if scheme == "http" && !allowsInsecureHTTP {
      throw ServerURLError.insecureHTTPNotAllowed
    }
    return url
  }

  /// Loopback hosts where plain http is the norm (a local dev server). Mirrors the
  /// desktop's `LOCAL_HOSTS` in `web/electron/src/url.js`.
  private static let localHosts: Set<String> = ["localhost", "127.0.0.1", "[::1]", "::1"]

  /// The scheme a schemeless input defaults to: http for a loopback host, https for
  /// everything else.
  ///
  /// A remote host defaults to https even when `allowsInsecureHTTP` is set — that
  /// flag exists so a debug build can reach a local dev server over http, not to
  /// silently downgrade a pasted remote host. Pinning `http://` for a server that
  /// redirects to https breaks every pinned-origin check in the web view (bridge
  /// trust, the workspace-chrome overlay, load-success recording), because the
  /// pinned and live origins then differ by scheme.
  private static func defaultScheme(for trimmed: String, allowsInsecureHTTP: Bool) -> String {
    guard allowsInsecureHTTP else { return "https" }
    let host = URL(string: "https://\(trimmed)")?.host?.lowercased() ?? ""
    return localHosts.contains(host) ? "http" : "https"
  }
}
