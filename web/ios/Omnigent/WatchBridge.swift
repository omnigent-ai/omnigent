import Foundation
import WatchConnectivity
import WebKit

final class WatchBridge: NSObject, WCSessionDelegate {
  static let shared = WatchBridge()

  private override init() { super.init() }

  func start() {
    guard WCSession.isSupported() else { return }
    WCSession.default.delegate = self
    WCSession.default.activate()
  }

  func sync(serverURL: URL, cookieStore: WKHTTPCookieStore) {
    guard WCSession.isSupported() else { return }
    cookieStore.getAllCookies { cookies in
      let apiBaseURL = WatchEndpoint.apiBaseURL(
        for: serverURL,
        workspaceUIPath: WorkspaceURLExpander.workspaceUIPath
      )
      let cookieScopeURL = WatchEndpoint.url(baseURL: apiBaseURL, path: "/v1")
      let applicableCookies = cookies.filter { Self.cookie($0, appliesTo: cookieScopeURL) }
      let cookieHeader = HTTPCookie.requestHeaderFields(with: applicableCookies)["Cookie"] ?? ""
      let credentials = WatchCredentials(
        serverURL: apiBaseURL,
        cookieHeader: cookieHeader,
        syncedAt: Date()
      )

      do {
        let data = try JSONEncoder().encode(credentials)
        try WCSession.default.updateApplicationContext(["credentials": data])
      } catch {
        NSLog("[omnigent] failed to sync watch credentials: \(String(describing: error))")
      }
    }
  }

  static func cookie(_ cookie: HTTPCookie, appliesTo url: URL) -> Bool {
    guard let host = url.host?.lowercased() else { return false }
    let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
    let domainMatches = host == domain || host.hasSuffix(".\(domain)")
    let cookiePath = cookie.path.isEmpty ? "/" : cookie.path
    let pathMatches =
      url.path == cookiePath
      || url.path.hasPrefix(cookiePath.hasSuffix("/") ? cookiePath : "\(cookiePath)/")
    let schemeMatches = !cookie.isSecure || url.scheme?.lowercased() == "https"
    return domainMatches && pathMatches && schemeMatches
  }

  func session(
    _ session: WCSession,
    activationDidCompleteWith activationState: WCSessionActivationState,
    error: Error?
  ) {}

  func sessionDidBecomeInactive(_ session: WCSession) {}

  func sessionDidDeactivate(_ session: WCSession) {
    session.activate()
  }
}
