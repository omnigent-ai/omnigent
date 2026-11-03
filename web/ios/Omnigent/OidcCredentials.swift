import AuthenticationServices
import Foundation

/// A session token minted by sign-in or refresh; it lives only in the session cookie.
struct OidcSessionToken: Equatable, Sendable {
  let token: String
  /// When the server said the token expires, measured from when it was requested.
  let expiresAt: Date?

  /// The server's session cookie carrying this token: host-only, `/`, HttpOnly, SameSite=Lax,
  /// and Secure on https. Nil when `serverURL` has no host or the cookie is malformed.
  func sessionCookie(named name: String, serverURL: URL) -> HTTPCookie? {
    guard let host = serverURL.host?.lowercased(), !host.isEmpty else { return nil }
    var properties: [HTTPCookiePropertyKey: Any] = [
      .name: name,
      .value: token,
      // No leading dot: a host-only cookie, as `__Host-` names require.
      .domain: host,
      .path: "/",
      .sameSitePolicy: HTTPCookieStringPolicy.sameSiteLax.rawValue,
      HTTPCookiePropertyKey("HttpOnly"): "TRUE",
    ]
    if serverURL.scheme?.lowercased() == "https" {
      properties[.secure] = "TRUE"
    }
    if let expiresAt {
      properties[.expires] = expiresAt
    }
    // Never hand back a session cookie that lost its protections in translation.
    guard let cookie = HTTPCookie(properties: properties), cookie.isHTTPOnly,
      cookie.isSecure || serverURL.scheme?.lowercased() != "https"
    else { return nil }
    return cookie
  }

  /// Whether `token` is a non-empty RFC 6265 cookie value: printable ASCII without spaces,
  /// double quotes, commas, semicolons or backslashes, so it can't break the `Cookie` header.
  static func isCookieSafe(_ token: String) -> Bool {
    !token.isEmpty
      && token.unicodeScalars.allSatisfy { scalar in
        let value = scalar.value
        return value >= 0x21 && value <= 0x7E && ![0x22, 0x2C, 0x3B, 0x5C].contains(value)
      }
  }
}

/// Why an OIDC sign-in, renewal or session check failed, worded for the user.
enum OidcSignInError: Error, Equatable, LocalizedError {
  /// The server URL has no http(s) origin to sign in to.
  case invalidServerURL
  /// Another sign-in sheet is already open.
  case signInInProgress
  /// The system sign-in sheet could not be shown or failed on its own.
  case browserUnavailable(host: String)
  /// The sheet returned a callback that isn't this sign-in's (wrong URI or state, no code).
  case invalidCallback(host: String)
  /// The server or IdP declined the sign-in on the callback; `description` is its reason.
  case signInRefused(host: String, description: String?)
  /// The sign-in came back, but exchanging its code for a session failed; retrying may work.
  case exchangeFailed(host: String)
  /// The stored grant is past its lifetime (`expired_token`); it has been forgotten.
  case grantExpired(host: String)
  /// The server revoked or rejected the stored grant (`invalid_grant`); it has been forgotten.
  case grantRejected(host: String)
  /// No usable grant is stored, or the server can't refresh one (404); none remains stored.
  case noStoredGrant(host: String)
  /// The server could not be reached or answered unexpectedly; any stored grant is kept.
  case network(host: String)
  /// The server refused a freshly minted session, or it could not be installed in the web view.
  case sessionRejected(host: String)

  var errorDescription: String? {
    switch self {
    case .invalidServerURL: "Enter a valid server URL."
    case .signInInProgress: "Sign-in is already in progress."
    case .browserUnavailable(let host):
      "Couldn't open the sign-in page for \(host). Try again."
    case .invalidCallback(let host): "Sign-in to \(host) didn't complete. Try again."
    case .signInRefused(let host, let description):
      description.flatMap { $0.isEmpty ? nil : $0 }
        ?? "\(host) didn't accept the sign-in. Try again."
    case .exchangeFailed(let host): "Couldn't finish signing in to \(host). Try again."
    case .grantExpired(let host):
      "Your sign-in to \(host) has expired. Sign in again to continue."
    case .grantRejected(let host): "\(host) ended your session. Sign in again to continue."
    case .noStoredGrant(let host): "Sign in to \(host) to continue."
    case .network(let host): "Couldn't reach \(host). Check your connection and try again."
    case .sessionRejected(let host):
      "\(host) didn't accept the session. Sign in again to continue."
    }
  }
}

/// Native OIDC sign-in through the system sign-in sheet (PKCE against the server's
/// `/auth/login` native parameters), plus the per-origin refresh grant that renews sessions.
///
/// Cancelling the calling task throws `CancellationError`. A refresh is shared by every caller for
/// the same origin, so one caller's cancellation surfaces only once the shared request settles.
@MainActor
final class OidcCredentials {
  /// Shared by every web view so refreshes stay one per origin across reconnects.
  static let shared = OidcCredentials()
  nonisolated static let callbackScheme = "ai.omnigent.ios"
  nonisolated static let redirectURI = "ai.omnigent.ios:/oauth/callback"
  nonisolated static let callbackPath = "/oauth/callback"
  nonisolated static let networkTimeout: TimeInterval = 20
  nonisolated static let verifyTimeout: TimeInterval = 10
  /// Longest session lifetime accepted from the server: the 400-day cookie cap of RFC 6265bis.
  nonisolated static let maxSessionLifetime: TimeInterval = 400 * 24 * 3600

  /// A cookie-less, cache-less session for the token, revoke and verification calls.
  nonisolated static let defaultSession: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpCookieAcceptPolicy = .never
    configuration.httpCookieStorage = nil
    configuration.httpShouldSetCookies = false
    configuration.urlCredentialStorage = nil
    configuration.urlCache = nil
    configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
    configuration.timeoutIntervalForRequest = networkTimeout
    configuration.timeoutIntervalForResource = networkTimeout
    return URLSession(configuration: configuration)
  }()

  private struct RefreshFlight {
    let id: UUID
    let task: Task<OidcSessionToken, Error>
  }

  private let store: any OidcCredentialStoring
  private let session: URLSession
  private let browser: WebAuthenticationBrowser
  private let now: () -> Date
  private var refreshes: [String: RefreshFlight] = [:]

  init(
    store: any OidcCredentialStoring = OidcCredentialStore(),
    session: URLSession = OidcCredentials.defaultSession,
    sessionFactory: WebAuthenticationBrowser.SessionFactory? = nil,
    now: @escaping () -> Date = Date.init
  ) {
    self.store = store
    self.session = session
    browser = WebAuthenticationBrowser(sessionFactory: sessionFactory)
    self.now = now
  }

  /// Signs in through the system sheet and returns the new session token. Stores the server's
  /// refresh grant, or forgets an older one when the server issues none.
  ///
  /// Cancellation stops the sheet, but not an exchange already sent: its grant is stored (or
  /// revoked when the Keychain refuses it) before the cancellation surfaces.
  func signIn(serverURL: URL, anchor: ASPresentationAnchor) async throws -> OidcSessionToken {
    try Task.checkCancellation()
    let server = try Server(serverURL)
    guard let verifier = OAuthSupport.randomValue(), let state = OAuthSupport.randomValue() else {
      throw OidcSignInError.browserUnavailable(host: server.host)
    }
    guard
      let loginURL = server.authorizationURL(
        state: state, challenge: OAuthSupport.challenge(for: verifier))
    else { throw OidcSignInError.invalidServerURL }

    let callback: URL
    do {
      callback = try await browser.callbackURL(
        for: loginURL, callback: .customScheme(Self.callbackScheme), anchor: anchor)
    } catch let error as WebAuthenticationBrowserError {
      switch error {
      case .inProgress: throw OidcSignInError.signInInProgress
      case .unavailable, .failed: throw OidcSignInError.browserUnavailable(host: server.host)
      case .missingCallback: throw OidcSignInError.invalidCallback(host: server.host)
      }
    }
    try Task.checkCancellation()
    let code = try Self.authorizationCode(from: callback, state: state, host: server.host)

    let requestedAt = now()
    // Unstructured, so the caller's cancellation can't drop a grant the server already issued.
    let exchange = Task {
      try await self.post(
        server.endpoint("/auth/native-token"),
        fields: [("code", code), ("code_verifier", verifier), ("redirect_uri", Self.redirectURI)],
        host: server.host)
    }
    let (data, response) = try await exchange.value
    let body = Self.jsonObject(data)
    guard response.statusCode == 200,
      let minted = Self.sessionToken(from: body, key: "token", requestedAt: requestedAt)
    else {
      try Task.checkCancellation()
      throw OidcSignInError.exchangeFailed(host: server.host)
    }

    if let refreshToken = body?["refresh_token"] as? String, !refreshToken.isEmpty {
      let grant = OidcRefreshGrant(refreshToken: refreshToken, userID: body?["user_id"] as? String)
      do {
        try store.save(grant, origin: server.origin)
      } catch {
        // A grant that can't be kept must not stay live on the server.
        revoke(grant, server: server)
        throw error
      }
    } else {
      // A server without refresh grants: an older grant for this origin is stale. Forgetting it
      // is best effort; the new session works either way.
      try? store.delete(origin: server.origin)
    }
    try Task.checkCancellation()
    return minted
  }

  /// Mints a session token from the stored grant, with one request in flight per origin.
  ///
  /// `invalid_grant`, `expired_token` and a 404 forget the grant; any other failure keeps it and
  /// throws ``OidcSignInError/network(host:)``.
  func refresh(serverURL: URL) async throws -> OidcSessionToken {
    try Task.checkCancellation()
    let server = try Server(serverURL)
    let flight: RefreshFlight
    if let existing = refreshes[server.origin] {
      flight = existing
    } else {
      let id = UUID()
      flight = RefreshFlight(
        id: id,
        task: Task {
          defer {
            if self.refreshes[server.origin]?.id == id { self.refreshes[server.origin] = nil }
          }
          return try await self.performRefresh(server)
        })
      refreshes[server.origin] = flight
    }
    let minted = try await flight.task.value
    try Task.checkCancellation()
    return minted
  }

  /// Forgets the stored grant before any network call, then revokes it as a best effort. The
  /// returned task finishes once the revoke settles; it never fails. A refresh in flight for the
  /// origin is cancelled, so a later caller can't join one minted from the revoked grant.
  @discardableResult
  func signOut(serverURL: URL) throws -> Task<Void, Never> {
    let server = try Server(serverURL)
    refreshes[server.origin]?.task.cancel()
    refreshes[server.origin] = nil
    // An unreadable grant can't be revoked, but it is still deleted.
    let grant = try? store.load(origin: server.origin)
    var deleteError: Error?
    do {
      try store.delete(origin: server.origin)
    } catch {
      deleteError = error
    }
    let revocation = grant.map { revoke($0, server: server) } ?? Task {}
    if let deleteError { throw deleteError }
    return revocation
  }

  /// Best-effort `POST /oauth/revoke`; the task never fails.
  @discardableResult
  private func revoke(_ grant: OidcRefreshGrant, server: Server) -> Task<Void, Never> {
    Task {
      guard let url = server.endpoint("/oauth/revoke") else { return }
      _ = try? await self.post(
        url, fields: [("refresh_token", grant.refreshToken)], host: server.host)
    }
  }

  #if DEBUG
    /// Forgets the stored grant without revoking it, so a tester can watch renewal fail.
    func forgetGrantForTesting(serverURL: URL) throws {
      try store.delete(origin: Server(serverURL).origin)
    }
  #endif

  /// Whether a readable refresh grant is stored for the server's origin.
  func hasStoredGrant(serverURL: URL) -> Bool {
    guard let origin = serverURL.omnigentOrigin else { return false }
    return (try? store.load(origin: origin)) != nil
  }

  /// Whether the server accepts `token` as its session cookie: `GET /v1/me` answers 200. A 401,
  /// 403 or redirect is a rejection; anything else throws ``OidcSignInError/network(host:)``.
  func isAccepted(token: String, cookieName: String, serverURL: URL) async throws -> Bool {
    try Task.checkCancellation()
    let server = try Server(serverURL)
    guard let url = server.endpoint("/v1/me") else { throw OidcSignInError.invalidServerURL }
    var request = URLRequest(
      url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: Self.verifyTimeout)
    request.httpShouldHandleCookies = false
    request.setValue("\(cookieName)=\(token)", forHTTPHeaderField: "Cookie")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    let (_, response) = try await send(request, host: server.host)
    switch response.statusCode {
    case 200: return true
    case 401, 403, 300..<400: return false
    default: throw OidcSignInError.network(host: server.host)
    }
  }

  private func performRefresh(_ server: Server) async throws -> OidcSessionToken {
    let grant: OidcRefreshGrant
    do {
      guard let stored = try store.load(origin: server.origin) else {
        throw OidcSignInError.noStoredGrant(host: server.host)
      }
      grant = stored
    } catch OidcCredentialStoreError.invalidData {
      try? store.delete(origin: server.origin)
      throw OidcSignInError.noStoredGrant(host: server.host)
    }
    let requestedAt = now()
    let (data, response) = try await post(
      server.endpoint("/oauth/token"),
      fields: [("grant_type", "refresh_token"), ("refresh_token", grant.refreshToken)],
      host: server.host)
    let body = Self.jsonObject(data)
    if response.statusCode == 200,
      let minted = Self.sessionToken(from: body, key: "access_token", requestedAt: requestedAt)
    {
      return minted
    }
    let deadGrantError: OidcSignInError
    switch (body?["error"] as? String, response.statusCode) {
    case ("invalid_grant", _): deadGrantError = .grantRejected(host: server.host)
    case ("expired_token", _): deadGrantError = .grantExpired(host: server.host)
    case (_, 404): deadGrantError = .noStoredGrant(host: server.host)
    default: throw OidcSignInError.network(host: server.host)
    }
    // A dead grant: forget it so the next connect signs in through the sheet.
    try? store.delete(origin: server.origin)
    throw deadGrantError
  }

  /// POSTs a form without following redirects, so a 3xx can't carry secrets elsewhere.
  private func post(_ url: URL?, fields: [(String, String)], host: String) async throws -> (
    Data, HTTPURLResponse
  ) {
    guard let url else { throw OidcSignInError.invalidServerURL }
    var request = URLRequest(
      url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: Self.networkTimeout)
    request.httpMethod = "POST"
    request.httpShouldHandleCookies = false
    request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.httpBody = OAuthSupport.formBody(fields)
    return try await send(request, host: host)
  }

  private func send(_ request: URLRequest, host: String) async throws -> (Data, HTTPURLResponse) {
    try Task.checkCancellation()
    let data: Data
    let response: URLResponse
    do {
      (data, response) = try await session.data(for: request, delegate: OidcRedirectBlocker())
    } catch {
      // Only the caller's cancellation propagates; a transport-level cancel is a network failure.
      try Task.checkCancellation()
      throw OidcSignInError.network(host: host)
    }
    try Task.checkCancellation()
    guard let http = response as? HTTPURLResponse else {
      throw OidcSignInError.network(host: host)
    }
    return (data, http)
  }

  /// The code in a callback for this sign-in's redirect URI and exact `state`.
  static func authorizationCode(from callback: URL, state: String, host: String) throws -> String {
    guard let components = URLComponents(url: callback, resolvingAgainstBaseURL: false),
      components.scheme?.lowercased() == callbackScheme, components.host == nil,
      components.user == nil, components.password == nil, components.port == nil,
      components.path == callbackPath
    else { throw OidcSignInError.invalidCallback(host: host) }
    guard let items = formItems(components.percentEncodedQuery) else {
      throw OidcSignInError.invalidCallback(host: host)
    }
    let states = items.filter { $0.name == "state" }
    guard states.count == 1, states.first?.value == state else {
      throw OidcSignInError.invalidCallback(host: host)
    }
    if items.contains(where: { $0.name == "error" }) {
      let description = items.first { $0.name == "error_description" }?.value
      throw OidcSignInError.signInRefused(host: host, description: description)
    }
    let codes = items.filter { $0.name == "code" }
    guard codes.count == 1, let code = codes.first?.value, !code.isEmpty else {
      throw OidcSignInError.invalidCallback(host: host)
    }
    return code
  }

  /// A query decoded as `application/x-www-form-urlencoded`, as RFC 6749 §4.1.2 responses are
  /// built: `+` is a space, then percent-escapes decode. Nil when an escape is malformed.
  static func formItems(_ percentEncodedQuery: String?) -> [URLQueryItem]? {
    guard let query = percentEncodedQuery, !query.isEmpty else { return [] }
    var items: [URLQueryItem] = []
    for pair in query.split(separator: "&", omittingEmptySubsequences: true) {
      let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
      let decoded = parts.map {
        $0.replacingOccurrences(of: "+", with: " ").removingPercentEncoding
      }
      guard let name = decoded[0] else { return nil }
      if decoded.count > 1 {
        guard let value = decoded[1] else { return nil }
        items.append(URLQueryItem(name: name, value: value))
      } else {
        items.append(URLQueryItem(name: name, value: nil))
      }
    }
    return items
  }

  private static func jsonObject(_ data: Data) -> [String: Any]? {
    (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
  }

  private static func sessionToken(from body: [String: Any]?, key: String, requestedAt: Date)
    -> OidcSessionToken?
  {
    guard let token = body?[key] as? String, OidcSessionToken.isCookieSafe(token) else {
      return nil
    }
    var expiresAt: Date?
    if let number = body?["expires_in"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
      number.doubleValue.isFinite, number.doubleValue > 0
    {
      expiresAt = requestedAt.addingTimeInterval(min(number.doubleValue, maxSessionLifetime))
    }
    return OidcSessionToken(token: token, expiresAt: expiresAt)
  }

  /// A server URL resolved to its credential origin, host label and mount-aware endpoints.
  private struct Server: Sendable {
    let url: URL
    let origin: String
    let host: String

    init(_ url: URL) throws {
      guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
        let origin = url.omnigentOrigin
      else { throw OidcSignInError.invalidServerURL }
      self.url = url
      self.origin = origin
      host = url.omnigentHostLabel
    }

    func endpoint(_ routePath: String) -> URL? {
      url.omnigentEndpoint(routePath)
    }

    func authorizationURL(state: String, challenge: String) -> URL? {
      guard let login = endpoint("/auth/login"),
        var components = URLComponents(url: login, resolvingAgainstBaseURL: false)
      else { return nil }
      components.queryItems = [
        URLQueryItem(name: "native_redirect_uri", value: OidcCredentials.redirectURI),
        URLQueryItem(name: "native_state", value: state),
        URLQueryItem(name: "code_challenge", value: challenge),
        URLQueryItem(name: "code_challenge_method", value: "S256"),
      ]
      return components.url
    }
  }
}

/// The token, revoke and verification endpoints answer directly; a 3xx is the final response.
private final class OidcRedirectBlocker: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
