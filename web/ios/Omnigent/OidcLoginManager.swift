import Foundation
import UIKit
import os

private let authLogger = Logger(subsystem: "ai.omnigent.ios", category: "auth")

/// Log an auth-flow breadcrumb. Callers pass origins, statuses, and lengths
/// only — NEVER a full URL or token, which carry OAuth state/PKCE/session
/// material — so the message is safe to log `.public`.
func authLog(_ message: String) {
  authLogger.log("\(message, privacy: .public)")
}

/// Drives the RFC 8252 login flow for the iOS shell: authenticate in the
/// **system browser** — a real browser, so Google sign-in (which blocks embedded
/// `WKWebView`s with `disallowed_useragent`) and passkeys (which need the
/// browser / a password manager) both work — then bridge the resulting session
/// back into the `WKWebView`, whose cookie store is isolated from the browser's.
///
/// This is the iOS mirror of Android's `OidcLoginManager` (see
/// `web/android/.../OidcLoginManager.kt`, merged in #1704) and needs **no server
/// change** — it reuses the same browser-login endpoints the `omnigent login`
/// CLI uses:
///   1. `POST /auth/cli-login` -> `{ticket, login_url}`
///   2. open `origin + login_url` in the system browser; the user authenticates;
///      the OIDC callback fulfills the ticket server-side
///   3. `GET /auth/cli-poll?ticket=...` -> 202 while pending, 200 `{token}` once
///      fulfilled, 410 if expired/unknown
///
/// That `token` is exactly the session-cookie JWT (the server validates the same
/// HS256 JWT as either the session cookie or a `Bearer`), so the caller injects
/// it into the WebView's `WKHTTPCookieStore` and reloads — authenticated.
///
/// Fixes omnigent-ai/omnigent#2549 (iOS parity for the Android fix #1704/#1708).
@MainActor
final class OidcLoginManager {
  /// Backing task for an in-flight login; nil when idle. Held so `shutdown()`
  /// can cancel a poll that would otherwise run up to `pollTimeout` after the
  /// host WebView is gone.
  private var task: Task<Void, Never>?
  private var inFlight = false

  /// Begin a login against `origin` (the pinned server, e.g.
  /// `https://agents.example.com`). Opens the system browser and polls in the
  /// background; `onSession` is invoked on the main actor with the session JWT
  /// once the browser flow completes.
  ///
  /// Returns `true` if this call started a flow, or `false` if one was already
  /// in flight (a second concurrent call is ignored) — a multi-hop OIDC redirect
  /// can re-enter before the first hand-off settles, and the caller uses the
  /// result so a no-op call isn't counted against its retry budget.
  @discardableResult
  func start(origin: String, onSession: @escaping (String) -> Void) -> Bool {
    guard !inFlight else { return false }
    inFlight = true
    task = Task { [weak self] in
      var session: String?
      defer {
        self?.inFlight = false
        // Never invoke into a torn-down host: shutdown() cancels the task, and a
        // cancelled task must not deliver a session.
        if let session, !Task.isCancelled { onSession(session) }
      }
      guard let ticket = await Self.requestTicket(origin: origin) else {
        authLog("cli-login -> FAILED")
        return
      }
      authLog("cli-login -> ticket ok")
      guard let url = URL(string: origin + ticket.loginPath) else { return }
      await UIApplication.shared.open(url)
      authLog("opening login in system browser")  // URL carries a one-time ticket — not logged
      guard let token = await Self.pollForToken(origin: origin, ticket: ticket.id) else {
        authLog("poll -> no token")
        return
      }
      authLog("poll -> token (len=\(token.count))")
      session = token
    }
    return true
  }

  /// Cancel an in-flight login and release the host. Call from the coordinator's
  /// `detach()` so a poll can't outlive the WebView.
  func shutdown() {
    task?.cancel()
    task = nil
    inFlight = false
  }

  // MARK: - Ticket + poll (pure networking, testable helpers below)

  struct Ticket: Equatable {
    let id: String
    let loginPath: String
  }

  private static func requestTicket(origin: String) async -> Ticket? {
    guard let url = URL(string: origin + "/auth/cli-login") else { return nil }
    var request = URLRequest(url: url, timeoutInterval: httpTimeout)
    request.httpMethod = "POST"
    // Bodyless POST — set Content-Length explicitly; some servers/WAFs reject a
    // POST without it (411 Length Required). Mirrors the Android client.
    request.setValue("0", forHTTPHeaderField: "Content-Length")
    guard let (data, response) = try? await URLSession.shared.data(for: request),
      (response as? HTTPURLResponse)?.statusCode == 200
    else { return nil }
    return parseTicket(data)
  }

  private static func pollForToken(origin: String, ticket: String) async -> String? {
    let deadline = Date().addingTimeInterval(pollTimeout)
    guard let url = pollURL(origin: origin, ticket: ticket) else { return nil }
    while Date() < deadline {
      // Throws CancellationError on shutdown() — the loop then exits via the
      // catch, delivering no token.
      do { try await Task.sleep(nanoseconds: pollInterval) } catch { return nil }
      var request = URLRequest(url: url, timeoutInterval: httpTimeout)
      request.httpMethod = "GET"
      guard let (data, response) = try? await URLSession.shared.data(for: request),
        let http = response as? HTTPURLResponse
      else {
        continue  // transient network error — keep polling until the deadline
      }
      switch parsePoll(status: http.statusCode, data: data) {
      case .pending: continue
      case .token(let token): return token
      case .failed: return nil
      }
    }
    return nil
  }

  // MARK: - Pure helpers (unit-tested standalone; no I/O)

  enum PollResult: Equatable {
    case pending
    case token(String)
    case failed
  }

  /// Parse a `POST /auth/cli-login` body into a `Ticket`, or nil if malformed.
  ///
  /// The `login_url` MUST be a server-relative path (`start` concatenates it onto
  /// the pinned origin): a scheme-relative `//host` or an absolute URL would send
  /// the one-time ticket flow to an attacker-chosen destination, so both are
  /// rejected here — the browser hand-off can never leave the pinned origin.
  static func parseTicket(_ data: Data) -> Ticket? {
    guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let id = json["ticket"] as? String, !id.isEmpty,
      let loginPath = json["login_url"] as? String,
      isSafeRelativeLoginPath(loginPath)
    else { return nil }
    return Ticket(id: id, loginPath: loginPath)
  }

  /// True for a same-origin relative path: starts with a single `/` (not `//`,
  /// which is scheme-relative and points at a foreign host).
  static func isSafeRelativeLoginPath(_ path: String) -> Bool {
    path.hasPrefix("/") && !path.hasPrefix("//")
  }

  /// Map a `GET /auth/cli-poll` response to a `PollResult`: 202 pending, 200 with
  /// a non-empty `token` -> token, anything else (410 expired/unknown, or a 200
  /// without a token) -> failed.
  static func parsePoll(status: Int, data: Data) -> PollResult {
    switch status {
    case 202: return .pending
    case 200:
      guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
        let token = json["token"] as? String, !token.isEmpty
      else { return .failed }
      return .token(token)
    default: return .failed
    }
  }

  static func pollURL(origin: String, ticket: String) -> URL? {
    var components = URLComponents(string: origin + "/auth/cli-poll")
    components?.queryItems = [URLQueryItem(name: "ticket", value: ticket)]
    return components?.url
  }

  /// True if `token` is shaped like a JWT — three non-empty base64url
  /// (`[A-Za-z0-9_-]`) segments. A real session token always is, and this shape
  /// can never carry the `;`, whitespace, or control chars that would let the
  /// value break out of a cookie and smuggle in attributes (e.g. `Domain=`,
  /// defeating the `__Host-` prefix). Rejects only a malformed/hostile value.
  static func isJwtShaped(_ token: String) -> Bool {
    let parts = token.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { return false }
    let base64url = CharacterSet(charactersIn: "-_").union(.alphanumerics)
    return parts.allSatisfy { part in
      !part.isEmpty && CharacterSet(charactersIn: String(part)).isSubset(of: base64url)
    }
  }

  /// Build the session cookie to inject into the WebView so a reload lands
  /// authenticated. Mirrors the server's `session_cookie_name`: the `__Host-`
  /// prefix on HTTPS (host-only + `Secure` + `Path=/`, which the properties
  /// below satisfy), a plain name on HTTP (local/debug). Returns nil if `origin`
  /// has no host or the token isn't JWT-shaped.
  static func sessionCookie(forOrigin origin: String, token: String) -> HTTPCookie? {
    guard isJwtShaped(token),
      let url = URL(string: origin), let host = url.host
    else { return nil }
    let secure = url.scheme?.lowercased() == "https"
    let name = secure ? "__Host-ap_session" : "ap_session"
    var properties: [HTTPCookiePropertyKey: Any] = [
      .name: name,
      .value: token,
      // Host-only (no leading dot) — required by the `__Host-` prefix, which
      // forbids a Domain attribute.
      .domain: host,
      .path: "/",
    ]
    if secure { properties[.secure] = "TRUE" }
    return HTTPCookie(properties: properties)
  }

  // MARK: - Tunables (mirror the Android client / the CLI's 5-minute window)

  private static let pollInterval: UInt64 = 2_000_000_000  // 2s
  private static let pollTimeout: TimeInterval = 5 * 60  // 5 min
  private static let httpTimeout: TimeInterval = 10  // connect + read
}
