import Foundation

/// A server's `/.well-known/omnigent.json`, read before loading so the shell can adapt to it.
///
/// Mirrors the desktop's `fetchServerManifest`: anything short of a well-formed manifest (404 from
/// an older server, unreachable host, HTML, malformed JSON, wrong types) is the ``baseline``,
/// meaning "keep the existing behavior". Gate on `manifestVersion >= N`, never `== N`.
struct ServerManifest: Equatable {
  struct Auth: Equatable {
    enum Mode: String {
      case oidc
      case accounts
      case header
      case custom
      /// The server's `"none"`; named apart from `Optional.none` so `auth?.mode` compares safely.
      case unauthenticated = "none"
    }

    let mode: Mode
    /// `__Host-ap_session` or `ap_session`; never the `__Host-` name for an http server.
    let sessionCookie: String?
    /// Redirect URIs the server accepts for native sign-in; empty when it names none.
    let nativeRedirectURIs: [String]
  }

  let manifestVersion: Double
  /// The `auth` block, or nil when absent or untrustworthy.
  let auth: Auth?

  /// What a server without the manifest route implies.
  static let baseline = ServerManifest(manifestVersion: 0, auth: nil)

  static let path = "/.well-known/omnigent.json"
  static let fetchTimeout: TimeInterval = 5

  private static let sessionCookieNames: Set<String> = ["__Host-ap_session", "ap_session"]

  /// A cookie-less, cache-less session whose whole fetch is bounded by ``fetchTimeout``.
  static let defaultSession: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpCookieAcceptPolicy = .never
    configuration.httpCookieStorage = nil
    configuration.httpShouldSetCookies = false
    configuration.urlCredentialStorage = nil
    configuration.urlCache = nil
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    configuration.timeoutIntervalForRequest = fetchTimeout
    configuration.timeoutIntervalForResource = fetchTimeout
    return URLSession(configuration: configuration)
  }()

  /// Reads the manifest at `serverURL`'s origin (its path is ignored), without following
  /// redirects. Throws only when the calling task is cancelled; every failure yields ``baseline``.
  static func fetch(
    for serverURL: URL, session: URLSession = ServerManifest.defaultSession
  ) async throws -> ServerManifest {
    try Task.checkCancellation()
    guard let scheme = serverURL.scheme?.lowercased(), scheme == "http" || scheme == "https",
      let origin = serverURL.omnigentOrigin, let url = URL(string: origin + path)
    else { return baseline }
    var request = URLRequest(
      url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: fetchTimeout)
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    let data: Data
    let response: URLResponse
    do {
      (data, response) = try await session.data(
        for: request, delegate: ServerManifestRedirectBlocker())
    } catch {
      // Only the caller's cancellation propagates; a transport-level cancel is a failed fetch.
      try Task.checkCancellation()
      return baseline
    }
    guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
      let contentType = http.value(forHTTPHeaderField: "Content-Type"),
      contentType.lowercased().contains("json")
    else { return baseline }
    return parse(data, serverURL: serverURL)
  }

  /// The manifest in a response body, or ``baseline`` when it is not one.
  static func parse(_ data: Data, serverURL: URL) -> ServerManifest {
    guard let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let version = number(body["manifest_version"])
    else { return baseline }
    return ServerManifest(
      manifestVersion: version, auth: auth(body["auth"], serverURL: serverURL))
  }

  /// Only known modes and the two real cookie names pass, and `__Host-` only for https.
  static func auth(_ raw: Any?, serverURL: URL) -> Auth? {
    guard let raw = raw as? [String: Any], let modeName = raw["mode"] as? String,
      let mode = Auth.Mode(rawValue: modeName)
    else { return nil }
    var sessionCookie = (raw["session_cookie"] as? String).flatMap {
      sessionCookieNames.contains($0) ? $0 : nil
    }
    if sessionCookie?.hasPrefix("__Host-") == true, serverURL.scheme?.lowercased() != "https" {
      sessionCookie = nil
    }
    let nativeRedirectURIs = (raw["native_redirect_uris"] as? [Any])?.compactMap { $0 as? String }
    return Auth(
      mode: mode, sessionCookie: sessionCookie, nativeRedirectURIs: nativeRedirectURIs ?? [])
  }

  /// A finite JSON number; JSON booleans also bridge to `NSNumber`, so they are excluded.
  private static func number(_ raw: Any?) -> Double? {
    guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
      return nil
    }
    let value = number.doubleValue
    return value.isFinite ? value : nil
  }
}

/// A redirect to a login page is not a manifest, so the 3xx itself is the final response.
private final class ServerManifestRedirectBlocker: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
