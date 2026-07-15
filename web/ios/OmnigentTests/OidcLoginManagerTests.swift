import XCTest

@testable import Omnigent

/// Unit tests for the pure, I/O-free logic behind the RFC 8252 system-browser
/// login (#2549): ticket/poll response parsing, the same-origin relative-path
/// guard, JWT shape validation, poll-endpoint construction, and session-cookie
/// construction (name/flags/host-only domain for the `__Host-` prefix).
final class OidcLoginManagerTests: XCTestCase {

  // MARK: parseTicket

  func testParseTicketValid() {
    let data = Data(#"{"ticket":"abc123","login_url":"/auth/login?ticket=abc123"}"#.utf8)
    XCTAssertEqual(
      OidcLoginManager.parseTicket(data),
      OidcLoginManager.Ticket(id: "abc123", loginPath: "/auth/login?ticket=abc123")
    )
  }

  func testParseTicketRejectsOffOriginLoginURL() {
    // An absolute or scheme-relative login_url would send the one-time ticket
    // flow to an attacker-chosen host — must be rejected.
    XCTAssertNil(
      OidcLoginManager.parseTicket(
        Data(#"{"ticket":"a","login_url":"https://evil.example/x"}"#.utf8)))
    XCTAssertNil(
      OidcLoginManager.parseTicket(Data(#"{"ticket":"a","login_url":"//evil.example/x"}"#.utf8)))
  }

  func testParseTicketRejectsMalformed() {
    XCTAssertNil(OidcLoginManager.parseTicket(Data(#"{"ticket":"a"}"#.utf8)))
    XCTAssertNil(OidcLoginManager.parseTicket(Data(#"{"login_url":"/x"}"#.utf8)))
    XCTAssertNil(OidcLoginManager.parseTicket(Data(#"{"ticket":"","login_url":"/x"}"#.utf8)))
    XCTAssertNil(OidcLoginManager.parseTicket(Data("not json".utf8)))
  }

  // MARK: isSafeRelativeLoginPath

  func testSafeRelativeLoginPath() {
    XCTAssertTrue(OidcLoginManager.isSafeRelativeLoginPath("/auth/login?ticket=x"))
    XCTAssertFalse(OidcLoginManager.isSafeRelativeLoginPath("//evil.example"))
    XCTAssertFalse(OidcLoginManager.isSafeRelativeLoginPath("https://evil.example"))
    XCTAssertFalse(OidcLoginManager.isSafeRelativeLoginPath("auth/login"))
    XCTAssertFalse(OidcLoginManager.isSafeRelativeLoginPath(""))
  }

  // MARK: parsePoll

  func testParsePollPending() {
    XCTAssertEqual(OidcLoginManager.parsePoll(status: 202, data: Data()), .pending)
  }

  func testParsePollToken() {
    let data = Data(#"{"token":"h.p.s","user_id":"a@b.com","expires_in":28800}"#.utf8)
    XCTAssertEqual(OidcLoginManager.parsePoll(status: 200, data: data), .token("h.p.s"))
  }

  func testParsePollFailures() {
    XCTAssertEqual(OidcLoginManager.parsePoll(status: 410, data: Data()), .failed)
    XCTAssertEqual(OidcLoginManager.parsePoll(status: 500, data: Data()), .failed)
    XCTAssertEqual(
      OidcLoginManager.parsePoll(status: 200, data: Data(#"{"user_id":"x"}"#.utf8)), .failed)
    XCTAssertEqual(
      OidcLoginManager.parsePoll(status: 200, data: Data(#"{"token":""}"#.utf8)), .failed)
  }

  // MARK: pollURL

  func testPollURLConstruction() {
    let url = OidcLoginManager.pollURL(origin: "https://agents.example.com", ticket: "abc-_123")
    XCTAssertEqual(url?.path, "/auth/cli-poll")
    let items = url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.queryItems }
    XCTAssertEqual(items, [URLQueryItem(name: "ticket", value: "abc-_123")])
  }

  // MARK: isJwtShaped

  func testJwtShapeAccepts() {
    XCTAssertTrue(OidcLoginManager.isJwtShaped("aGVhZA.cGF5bG9hZA.c2ln"))
    XCTAssertTrue(OidcLoginManager.isJwtShaped("ab-C_1.d2.e3"))
  }

  func testJwtShapeRejects() {
    XCTAssertFalse(OidcLoginManager.isJwtShaped("only.two"))
    XCTAssertFalse(OidcLoginManager.isJwtShaped("a.b.c.d"))
    XCTAssertFalse(OidcLoginManager.isJwtShaped("a..c"))
    XCTAssertFalse(OidcLoginManager.isJwtShaped("a.b c.d"))  // whitespace
    XCTAssertFalse(OidcLoginManager.isJwtShaped("a.b;Domain=x.c"))  // cookie-attr injection
    XCTAssertFalse(OidcLoginManager.isJwtShaped(""))
  }

  // MARK: sessionCookie

  func testSessionCookieHTTPSIsHostPrefixed() {
    let cookie = OidcLoginManager.sessionCookie(
      forOrigin: "https://agents.example.com", token: "a.b.c")
    XCTAssertEqual(cookie?.name, "__Host-ap_session")
    XCTAssertEqual(cookie?.value, "a.b.c")
    XCTAssertEqual(cookie?.path, "/")
    XCTAssertEqual(cookie?.isSecure, true)
    // Host-only (no leading dot) — required by the __Host- prefix.
    XCTAssertEqual(cookie?.domain, "agents.example.com")
    XCTAssertEqual(cookie?.domain.hasPrefix("."), false)
  }

  func testSessionCookieHTTPIsPlain() {
    let cookie = OidcLoginManager.sessionCookie(forOrigin: "http://localhost:6767", token: "a.b.c")
    XCTAssertEqual(cookie?.name, "ap_session")
    XCTAssertEqual(cookie?.isSecure, false)
    XCTAssertEqual(cookie?.domain, "localhost")
  }

  func testSessionCookieRejectsUnsafeToken() {
    XCTAssertNil(OidcLoginManager.sessionCookie(forOrigin: "https://x.example", token: "not-a-jwt"))
    XCTAssertNil(
      OidcLoginManager.sessionCookie(forOrigin: "https://x.example", token: "a.b;Domain=evil.c"))
  }
}
