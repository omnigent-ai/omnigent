import Foundation
import OSLog
import WatchConnectivity
import WebKit

/// Keeps the paired watch signed in to the server this phone shows; sign-out clears it.
@MainActor
final class WatchBridge: NSObject, WCSessionDelegate {
  static let shared = WatchBridge()
  private static let logger = Logger(subsystem: "ai.omnigent.ios", category: "watch-credentials")
  private static let pendingSignOutKey = "omnigent.watch.pendingSignOut"
  private static let lastSentAtKey = "omnigent.watch.lastSentAt"
  private let defaults: UserDefaults
  private let transport: (any WatchSessionTransport)?
  private(set) var latest: [String: Any]?
  /// New syncs and sign-out invalidate any older cookie read still in flight.
  private var generation = 0

  init(
    defaults: UserDefaults = .standard,
    transport: (any WatchSessionTransport)? = WCSession.isSupported() ? WCSession.default : nil
  ) {
    self.defaults = defaults
    self.transport = transport
    super.init()
    if defaults.bool(forKey: Self.pendingSignOutKey) {
      latest = [WatchCredentials.contextKey: Data()]
    }
  }

  func start() {
    guard let transport else { return }
    transport.delegate = self
    transport.activate()
    flush()
  }

  @discardableResult
  func sync(
    serverURL: URL?, workspacePageURL: URL? = nil, workspaceID: String? = nil,
    cookieName: String? = nil,
    cookieStore: some WatchCookieStore
  ) -> Task<Void, Never>? {
    guard transport != nil, let serverURL else { return nil }
    generation += 1
    let started = generation
    return Task {
      let cookies = await cookieStore.allCookies()
      guard generation == started else { return }
      push(
        WatchCredentials(
          serverURL: serverURL, workspacePageURL: workspacePageURL, workspaceID: workspaceID,
          cookieName: cookieName, cookies: cookies))
    }
  }

  func signOut() {
    generation += 1
    push(nil)
  }

  private func push(_ credentials: WatchCredentials?) {
    let data = credentials.flatMap { try? JSONEncoder().encode($0) } ?? Data()
    // Defaults persist only intent; WatchConnectivity retains the credential delivery context.
    if data.isEmpty { defaults.set(true, forKey: Self.pendingSignOutKey) }
    latest = [WatchCredentials.contextKey: data]
    flush()
  }

  func flush(now: Date = .now) {
    guard let transport, transport.delegate === self,
      transport.activationState == .activated,
      transport.isWatchAppInstalled, let latest
    else { return }
    // The system drops a context equal to the last one sent, even to a reinstalled watch app.
    let previous = defaults.object(forKey: Self.lastSentAtKey) as? Date ?? .distantPast
    let sentAt = max(now, previous.addingTimeInterval(0.001))
    defaults.set(sentAt, forKey: Self.lastSentAtKey)
    do {
      try transport.updateApplicationContext(latest.merging(["sentAt": sentAt]) { $1 })
      defaults.removeObject(forKey: Self.pendingSignOutKey)
    } catch {
      Self.logger.error(
        "Watch credential handoff failed (code \((error as NSError).code, privacy: .public))")
    }
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

@MainActor
protocol WatchSessionTransport: AnyObject {
  var delegate: (any WCSessionDelegate)? { get set }
  var activationState: WCSessionActivationState { get }
  var isWatchAppInstalled: Bool { get }
  func activate()
  func updateApplicationContext(_ applicationContext: [String: Any]) throws
}

extension WCSession: WatchSessionTransport {}

@MainActor
protocol WatchCookieStore {
  func allCookies() async -> [HTTPCookie]
}

extension WKHTTPCookieStore: WatchCookieStore {}

extension WatchCredentials {
  /// The API root under the server's mount and the cookies a browser would send it.
  @MainActor init?(
    serverURL: URL, workspacePageURL: URL? = nil, workspaceID: String? = nil,
    cookieName: String? = nil,
    cookies: [HTTPCookie]
  ) {
    var mount = WorkspaceURLExpander.workspaceUIURL(forBareRoot: serverURL) ?? serverURL
    // Use only the validated workspace bootstrap URL, never an arbitrary web-view navigation.
    if let workspacePageURL {
      guard var landing = URLComponents(url: workspacePageURL, resolvingAgainstBaseURL: false),
        landing.scheme?.lowercased() == "https", landing.host != nil,
        landing.user == nil, landing.password == nil
      else { return nil }
      landing.percentEncodedPath = WorkspaceURLExpander.workspaceUIPath
      guard let landed = landing.url else { return nil }
      mount = landed
    }
    guard let endpoint = mount.omnigentEndpoint("/v1"),
      var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)
    else { return nil }
    // Shared hosts route by the `o` workspace selector, which sign-in may have learned.
    let selector = [mount, serverURL].compactMap {
      URLComponents(url: $0, resolvingAgainstBaseURL: false)?
        .queryItems?.first { $0.name == "o" }?.value
    }.first
    components.queryItems = (workspaceID ?? selector).map { [URLQueryItem(name: "o", value: $0)] }
    guard let apiURL = components.url else { return nil }
    let sent = cookies.filter {
      (cookieName == nil || $0.name == cookieName)
        && DatabricksSessionClient.cookie($0, appliesTo: apiURL)
    }
    self.init(apiURL: apiURL, cookie: HTTPCookie.requestHeaderFields(with: sent)["Cookie"] ?? "")
  }
}
