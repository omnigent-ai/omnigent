import Foundation
import WebKit

/// A web view connection whose OIDC session the shell owns: the server and its session cookie.
struct OidcConnection: Equatable {
  /// The clean server URL (origin plus mount), never a page under it.
  let serverURL: URL
  let cookieName: String

  var origin: String { serverURL.omnigentOriginIdentity }
  var host: String { serverURL.omnigentHostLabel }
}

extension URL {
  /// The origin as a comparable string, also for hosts `omnigentOrigin` can't rebuild (IPv6
  /// literals): scheme, bracketed host and port, lowercased.
  var omnigentOriginIdentity: String {
    if let origin = omnigentOrigin { return origin }
    let components = URLComponents(url: self, resolvingAgainstBaseURL: false)
    let host = components?.percentEncodedHost ?? host ?? ""
    let port = port.map { ":\($0)" } ?? ""
    return "\(scheme ?? "")://\(host)\(port)".lowercased()
  }
}

/// The server routes the shell handles itself instead of loading them in the web view.
enum OidcAuthRoute: Equatable {
  case login
  case logout

  /// The route `url` is under `serverURL`'s mount, or nil for any other page or origin.
  init?(url: URL, serverURL: URL) {
    guard let origin = url.omnigentOrigin, origin == serverURL.omnigentOrigin,
      let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath
    else { return nil }
    let mount = OidcWebSession.mountPath(of: serverURL)
    switch path {
    case mount + "/auth/login": self = .login
    case mount + "/auth/logout": self = .logout
    default: return nil
    }
  }
}

extension ServerManifest {
  /// The session cookie to install when the server offers this app's native sign-in; nil keeps
  /// the legacy in-web-view behavior.
  var nativeSignInCookieName: String? {
    guard let auth, auth.mode == .oidc,
      auth.nativeRedirectURIs.contains(OidcCredentials.redirectURI)
    else { return nil }
    return auth.sessionCookie
  }
}

/// Pure decisions behind the web view's OIDC session lifecycle, mirroring the desktop shell.
enum OidcWebSession {
  /// A background renewal that couldn't reach the server tries again after this.
  static let retryDelay: TimeInterval = 30
  /// Longest wait before a renewal, so a far-off expiry still renews and the sleep stays in range.
  static let maxRenewalDelay: TimeInterval = 24 * 3600

  /// The server URL's path without trailing slashes; empty for a server at the origin root.
  static func mountPath(of serverURL: URL) -> String {
    var mount = Substring(
      URLComponents(url: serverURL, resolvingAgainstBaseURL: false)?.percentEncodedPath ?? "")
    while mount.hasSuffix("/") { mount = mount.dropLast() }
    return String(mount)
  }

  /// Whether `url` is an app page of the server: same origin, under its mount, not an auth route.
  static func isPage(_ url: URL, of serverURL: URL) -> Bool {
    guard let origin = url.omnigentOrigin, origin == serverURL.omnigentOrigin,
      OidcAuthRoute(url: url, serverURL: serverURL) == nil,
      let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath
    else { return false }
    let mount = mountPath(of: serverURL)
    return mount.isEmpty || path == mount || path.hasPrefix(mount + "/")
  }

  /// Whether a browser would send `cookie` as the server's session cookie (RFC 6265): the
  /// name, the server's host, Secure only to https, a path covering the server's API, and
  /// unexpired. Another port or scheme on the same host must never receive it.
  static func isSessionCookie(
    _ cookie: HTTPCookie, named name: String, serverURL: URL, now: Date
  ) -> Bool {
    guard cookie.name == name, !cookie.value.isEmpty,
      let host = serverURL.host?.lowercased(),
      cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) == host,
      !cookie.isSecure || serverURL.scheme?.lowercased() == "https",
      let requestPath = serverURL.omnigentEndpoint("/v1/me").flatMap({
        URLComponents(url: $0, resolvingAgainstBaseURL: false)?.percentEncodedPath
      }),
      pathMatches(cookiePath: cookie.path, requestPath: requestPath)
    else { return false }
    return cookie.expiresDate.map { $0 > now } ?? true
  }

  /// RFC 6265 §5.1.4 path-match.
  static func pathMatches(cookiePath: String, requestPath: String) -> Bool {
    let cookiePath = cookiePath.isEmpty ? "/" : cookiePath
    if requestPath == cookiePath { return true }
    guard requestPath.hasPrefix(cookiePath) else { return false }
    return cookiePath.hasSuffix("/")
      || requestPath.dropFirst(cookiePath.count).first == "/"
  }

  /// Every cookie in `cookies` that the server would receive as its session cookie.
  static func sessionCookies(
    in cookies: [HTTPCookie], named name: String, serverURL: URL, now: Date
  ) -> [HTTPCookie] {
    cookies.filter { isSessionCookie($0, named: name, serverURL: serverURL, now: now) }
  }

  /// The server's session cookie, if the store holds one it would receive.
  static func liveSessionCookie(
    in cookies: [HTTPCookie], named name: String, serverURL: URL, now: Date
  ) -> HTTPCookie? {
    sessionCookies(in: cookies, named: name, serverURL: serverURL, now: now).first
  }

  /// A renewal failure worth remembering: it deleted the grant, so a later prompt would
  /// otherwise only see "no stored grant".
  static func rememberedRenewalCause(_ error: Error) -> OidcSignInError? {
    switch error as? OidcSignInError {
    case .grantExpired?, .grantRejected?: error as? OidcSignInError
    default: nil
    }
  }

  /// Whether a failed connect should keep relaunch from reopening the server: only when the
  /// server refused the sign-in, as reopening would show the same refusal again.
  static func stopsAutoOpening(after error: Error) -> Bool {
    if case .signInRefused? = error as? OidcSignInError { return true }
    return false
  }

  /// The error to explain at the next prompt: a remembered cause replaces "no stored grant".
  static func reauthenticationCause(for error: Error, remembered: OidcSignInError?) -> Error {
    if let remembered, case .noStoredGrant? = error as? OidcSignInError { return remembered }
    return error
  }

  /// How long to wait before renewing a cookie that expires at `expiresAt`: a fifth of the
  /// remaining lifetime early, at most a minute, and never more than `maxRenewalDelay`; zero once
  /// it is due.
  static func renewalDelay(expiresAt: Date, now: Date) -> TimeInterval {
    let remaining = expiresAt.timeIntervalSince(now)
    guard remaining > 0 else { return 0 }
    return min(maxRenewalDelay, max(0, remaining - min(60, remaining / 5)))
  }

  /// An unreachable server is a connection error, never a reason to sign in again.
  static func isNetworkFailure(_ error: Error) -> Bool {
    if case .network = error as? OidcSignInError { return true }
    return false
  }

  /// The reason to keep when the user closes a sign-in sheet that a failed renewal opened: an
  /// expired, ended or refused session. Nil when there was simply no sign-in to renew.
  static func cancelledSignInCause(_ renewalError: Error) -> OidcSignInError? {
    switch renewalError as? OidcSignInError {
    case .grantExpired?, .grantRejected?, .sessionRejected?: renewalError as? OidcSignInError
    default: nil
    }
  }

  /// The "Sign in again?" message for a session that can't be renewed.
  static func reauthenticationMessage(for error: Error, host: String) -> String {
    if let error = error as? OidcSignInError, !isNetworkFailure(error),
      let message = error.errorDescription
    {
      return message
    }
    return OidcSignInError.noStoredGrant(host: host).errorDescription ?? ""
  }

  /// The Connect screen message after a sign-out; `complete` is false when the grant survived.
  static func signedOutMessage(host: String, complete: Bool) -> String {
    complete
      ? "You're signed out of \(host)."
      : "Couldn't finish signing out of \(host); its saved sign-in may still be used. "
        + "Connect, then sign out again."
  }
}

/// The web view's cookie jar, as the OIDC connect path reads it.
@MainActor
protocol OidcCookieJar {
  /// Makes WebKit load the persisted cookies. Until then, a freshly launched app reads none.
  func loadPersistedCookies() async
  func allCookies() async -> [HTTPCookie]
}

extension WKWebsiteDataStore: OidcCookieJar {
  func loadPersistedCookies() async {
    _ = await dataRecords(ofTypes: [WKWebsiteDataTypeCookies])
  }

  func allCookies() async -> [HTTPCookie] { await httpCookieStore.allCookies() }
}

extension OidcWebSession {
  /// Every cookie in the jar, including those persisted by an earlier launch.
  @MainActor
  static func persistedCookies(in jar: some OidcCookieJar) async -> [HTTPCookie] {
    await jar.loadPersistedCookies()
    return await jar.allCookies()
  }
}

/// Stops renew-and-reload loops: the web app asking to sign in again this soon after a renewal
/// means the server rejected the renewed session.
struct OidcRenewalGuard {
  static let rejectedRenewalWindow: TimeInterval = 15

  private var lastRecoveryAt: Date?

  /// Records a sign-in request and whether to answer it with a renewal. A request that joins a
  /// renewal still in flight is always answered by it.
  mutating func shouldRenew(now: Date, renewalPending: Bool) -> Bool {
    if !renewalPending, let lastRecoveryAt,
      now.timeIntervalSince(lastRecoveryAt) < Self.rejectedRenewalWindow
    {
      return false
    }
    lastRecoveryAt = now
    return true
  }
}

/// Per-origin sign-out count, so a renewal or sign-in that started before a sign-out can't
/// reinstall the session afterwards.
@MainActor
final class OidcSignOutGenerations {
  static let shared = OidcSignOutGenerations()

  private var generations: [String: Int] = [:]

  func current(origin: String) -> Int { generations[origin, default: 0] }

  func signOut(origin: String) { generations[origin, default: 0] += 1 }

  func isCurrent(_ generation: Int, origin: String) -> Bool {
    current(origin: origin) == generation
  }
}

#if DEBUG
  /// One broken piece of a live OIDC session, offered by the debug menu.
  enum OidcDebugFault: String, CaseIterable, Identifiable, Sendable {
    case sessionCookie, refreshToken

    var id: String { rawValue }

    var title: String {
      switch self {
      case .sessionCookie: "Clear Session Cookie"
      case .refreshToken: "Clear Refresh Token"
      }
    }

    var systemImage: String {
      switch self {
      case .sessionCookie: "trash"
      case .refreshToken: "arrow.triangle.2.circlepath"
      }
    }

    var expectation: String {
      switch self {
      case .sessionCookie:
        "Cleared the session cookie. Expect a silent renewal after leaving and reopening the app, "
          + "or when the page next asks to sign in."
      case .refreshToken:
        "Forgot the refresh token. Expect a Sign In prompt the next time the session needs renewal."
      }
    }
  }
#endif
