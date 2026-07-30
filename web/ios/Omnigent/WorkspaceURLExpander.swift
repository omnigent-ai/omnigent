import Foundation

enum WorkspaceURLExpander {
  static let workspaceUIPath = "/ml/omnigents"

  /// Databricks Apps are served from `*.databricksapps.com` and answer with the
  /// same `server: databricks` header as a workspace, but they are NOT
  /// workspaces and have no `/ml/omnigents` mount, so expansion is skipped for
  /// these hosts.
  static let databricksAppsHostSuffix = "databricksapps.com"

  static func expandIfNeeded(
    _ url: URL,
    session: URLSession = SameOriginRedirectHandler.session
  ) async -> URL {
    guard url.scheme?.lowercased() == "https", isBareRoot(url), !isDatabricksAppsHost(url),
      let origin = originURL(for: url)
    else {
      return url
    }

    var request = URLRequest(url: origin)
    request.httpMethod = "HEAD"
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.timeoutInterval = 8

    do {
      let (_, response) = try await session.data(for: request)
      guard let http = response as? HTTPURLResponse else { return url }
      // Defense in depth: only trust responses that came from the approved
      // origin. The consent alert promises the app will only talk to the host
      // the user approved, so a response that landed on a different origin
      // (e.g. via a cross-origin redirect) must be rejected even if the
      // redirect delegate is bypassed (such as when a caller supplies a bare
      // session without a redirect policy).
      guard let responseURL = http.url, let responseOrigin = originURL(for: responseURL),
        responseOrigin == origin
      else {
        return url
      }
      guard (http.value(forHTTPHeaderField: "server") ?? "").lowercased() == "databricks" else {
        return url
      }
      return URL(
        string:
          "\(origin.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")))\(workspaceUIPath)"
      ) ?? url
    } catch {
      return url
    }
  }

  private static func isBareRoot(_ url: URL) -> Bool {
    url.path.isEmpty || url.path == "/"
  }

  private static func isDatabricksAppsHost(_ url: URL) -> Bool {
    guard let host = url.host?.lowercased() else { return false }
    return host == databricksAppsHostSuffix || host.hasSuffix(".\(databricksAppsHostSuffix)")
  }

  private static func originURL(for url: URL) -> URL? {
    guard let scheme = url.scheme, let host = url.host else { return nil }
    var components = URLComponents()
    components.scheme = scheme
    components.host = host
    components.port = url.port
    components.path = "/"
    return components.url
  }
}

/// URL session delegate that refuses to follow HTTP redirects whose
/// destination origin differs from the original request's origin.
///
/// `WorkspaceURLExpander.expandIfNeeded` probes a server the user just
/// consented to. Without a redirect policy, a malicious or misconfigured host
/// could answer the HEAD probe with a 3xx redirect to a different origin —
/// including a local-network service — causing the app to silently talk to a
/// host the user never approved. This delegate allows same-origin redirects
/// (e.g. path or trailing-slash normalization) but blocks anything that would
/// cross origins. `expandIfNeeded` additionally verifies `response.url` so a
/// cross-origin response is never trusted even if the caller supplies a
/// session without this delegate.
final class SameOriginRedirectHandler: NSObject, URLSessionTaskDelegate {
  static let session: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 8
    configuration.httpCookieAcceptPolicy = .never
    configuration.httpCookieStorage = nil
    return URLSession(
      configuration: configuration,
      delegate: SameOriginRedirectHandler(),
      delegateQueue: nil
    )
  }()

  func urlSession(
    _ session: URLSession,
    task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest,
    completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    guard
      let original = task.originalRequest?.url,
      let originalOrigin = Self.originKey(for: original),
      let redirectURL = request.url,
      let redirectOrigin = Self.originKey(for: redirectURL),
      originalOrigin == redirectOrigin
    else {
      // Block the redirect; URLSession delivers the 3xx response itself as
      // the task's final response, which `expandIfNeeded` then rejects via the
      // `response.url` origin check.
      completionHandler(nil)
      return
    }
    completionHandler(request)
  }

  private static func originKey(for url: URL) -> String? {
    guard let scheme = url.scheme?.lowercased(),
      let host = url.host?.lowercased()
    else { return nil }
    return "\(scheme)://\(host)\(url.port.map { ":\($0)" } ?? "")"
  }
}
