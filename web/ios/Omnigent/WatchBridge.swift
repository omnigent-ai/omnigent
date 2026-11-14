import Foundation
import WatchConnectivity
import WebKit

/// Keeps the paired watch signed in to the server this phone shows; sign-out clears it.
@MainActor
final class WatchBridge: NSObject, WCSessionDelegate {
  static let shared = WatchBridge()
  private var latest: [String: Any]?
  /// Bumped by sign-out, so a sync still reading cookies can't sign the watch back in.
  private var generation = 0

  func start() {
    guard WCSession.isSupported() else { return }
    WCSession.default.delegate = self
    WCSession.default.activate()
  }

  func sync(serverURL: URL?, workspaceID: String? = nil, cookieStore: WKHTTPCookieStore) {
    guard WCSession.isSupported(), let serverURL else { return }
    let started = generation
    Task {
      let cookies = await cookieStore.allCookies()
      guard generation == started else { return }
      push(WatchCredentials(serverURL: serverURL, workspaceID: workspaceID, cookies: cookies))
    }
  }

  func signOut() {
    generation += 1
    push(nil)
  }

  private func push(_ credentials: WatchCredentials?) {
    let data = credentials.flatMap { try? JSONEncoder().encode($0) } ?? Data()
    latest = [WatchCredentials.contextKey: data]
    flush()
  }

  private func flush() {
    guard WCSession.isSupported(), WCSession.default.activationState == .activated,
      WCSession.default.isWatchAppInstalled, let latest
    else { return }
    // The system drops a context equal to the last one sent, even to a reinstalled watch app.
    try? WCSession.default.updateApplicationContext(latest.merging(["sentAt": Date.now]) { $1 })
  }

  nonisolated func session(
    _ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?
  ) {
    Task { @MainActor in flush() }
  }

  /// The watch app can be installed after the page loaded; hand it the latest credentials then.
  nonisolated func sessionWatchStateDidChange(_ session: WCSession) {
    Task { @MainActor in flush() }
  }

  nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
  nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }
}

extension WatchCredentials {
  /// The API root under the server's mount and the cookies a browser would send it.
  @MainActor init?(serverURL: URL, workspaceID: String? = nil, cookies: [HTTPCookie]) {
    let mount = WorkspaceURLExpander.workspaceUIURL(forBareRoot: serverURL) ?? serverURL
    guard let endpoint = mount.omnigentEndpoint("/v1"),
      var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)
    else { return nil }
    // Shared hosts route by the `o` workspace selector, which sign-in may have learned.
    let selector = URLComponents(url: serverURL, resolvingAgainstBaseURL: false)?
      .queryItems?.first { $0.name == "o" }?.value
    components.queryItems = (workspaceID ?? selector).map { [URLQueryItem(name: "o", value: $0)] }
    guard let apiURL = components.url else { return nil }
    let sent = cookies.filter { DatabricksSessionClient.cookie($0, appliesTo: apiURL) }
    self.init(apiURL: apiURL, cookie: HTTPCookie.requestHeaderFields(with: sent)["Cookie"] ?? "")
  }
}
