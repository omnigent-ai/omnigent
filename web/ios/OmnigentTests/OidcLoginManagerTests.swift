import XCTest

@testable import Omnigent

final class OidcLoginManagerTests: XCTestCase {
  func testTicketAcceptsRootedSameOriginLoginPath() throws {
    let origin = try XCTUnwrap(URL(string: "https://example.com"))
    let data = try XCTUnwrap(
      #"{"ticket":"abc","login_url":"/auth/login?ticket=abc"}"#.data(using: .utf8))

    let ticket = try OidcLoginManager.ticket(from: data, origin: origin)

    XCTAssertEqual(ticket.id, "abc")
    XCTAssertEqual(ticket.loginURL.absoluteString, "https://example.com/auth/login?ticket=abc")
  }

  func testTicketRejectsAbsoluteAndSchemeRelativeLoginURLs() throws {
    let origin = try XCTUnwrap(URL(string: "https://example.com"))
    for loginURL in ["https://evil.example/auth", "//evil.example/auth", "auth/login"] {
      let data = try XCTUnwrap(
        #"{"ticket":"abc","login_url":"\#(loginURL)"}"#.data(using: .utf8))
      XCTAssertThrowsError(try OidcLoginManager.ticket(from: data, origin: origin))
    }
  }

  func testSessionDecodesTokenAndServerExpiry() throws {
    let data = try XCTUnwrap(
      #"{"token":"aaa.bbb.ccc","user_id":"u","expires_in":2592000}"#.data(using: .utf8))
    let session = try OidcLoginManager.session(from: data)

    XCTAssertEqual(session.token, "aaa.bbb.ccc")
    XCTAssertEqual(session.expiresIn, 2_592_000)

    let withoutExpiry = try XCTUnwrap(#"{"token":"aaa.bbb.ccc"}"#.data(using: .utf8))
    XCTAssertNil(try OidcLoginManager.session(from: withoutExpiry).expiresIn)
  }

  func testTokenRejectsCookieInjectionCharacters() throws {
    for token in ["aaa.bbb.ccc; Domain=evil.example", "aaa.bbb", "aaa..ccc"] {
      let data = try XCTUnwrap(#"{"token":"\#(token)"}"#.data(using: .utf8))
      XCTAssertThrowsError(try OidcLoginManager.session(from: data))
    }
  }

  func testHTTPSCookieUsesHostPrefixAndSecureFlag() throws {
    let origin = try XCTUnwrap(URL(string: "https://example.com"))
    let cookie = try OidcLoginManager.sessionCookie(origin: origin, token: "aaa.bbb.ccc")

    XCTAssertEqual(cookie.name, "__Host-ap_session")
    XCTAssertEqual(cookie.domain, "example.com")
    XCTAssertEqual(cookie.path, "/")
    XCTAssertTrue(cookie.isSecure)
  }

  func testDebugHTTPCookieUsesUnprefixedName() throws {
    let origin = try XCTUnwrap(URL(string: "http://localhost:6767"))
    let cookie = try OidcLoginManager.sessionCookie(origin: origin, token: "aaa.bbb.ccc")

    XCTAssertEqual(cookie.name, "ap_session")
    XCTAssertFalse(cookie.isSecure)
  }

  func testSessionCookieOutlivesAppTermination() throws {
    // A cookie without an expiry is session-only and is dropped with the process,
    // so every cold start would re-run the browser login.
    for origin in ["https://example.com", "http://localhost:6767"] {
      let cookie = try OidcLoginManager.sessionCookie(
        origin: try XCTUnwrap(URL(string: origin)), token: "aaa.bbb.ccc")

      XCTAssertFalse(cookie.isSessionOnly, origin)
      let expiresDate = try XCTUnwrap(cookie.expiresDate, origin)
      XCTAssertGreaterThan(expiresDate, Date().addingTimeInterval(60 * 60), origin)
    }
  }

  func testSessionCookieExpiresWithServerSession() throws {
    let origin = try XCTUnwrap(URL(string: "https://example.com"))
    let expiresIn: TimeInterval = 30 * 24 * 60 * 60
    let issuedAt = Date()
    let cookie = try OidcLoginManager.sessionCookie(
      origin: origin, token: "aaa.bbb.ccc", expiresIn: expiresIn)

    let expiresDate = try XCTUnwrap(cookie.expiresDate)
    XCTAssertEqual(expiresDate.timeIntervalSince(issuedAt), expiresIn, accuracy: 5)

    // Without expires_in the cookie lasts the server's default 8h session TTL.
    let fallback = try OidcLoginManager.sessionCookie(origin: origin, token: "aaa.bbb.ccc")
    XCTAssertEqual(
      try XCTUnwrap(fallback.expiresDate).timeIntervalSince(issuedAt), 8 * 60 * 60, accuracy: 5)

    for expired: TimeInterval in [0, -60] {
      XCTAssertThrowsError(
        try OidcLoginManager.sessionCookie(origin: origin, token: "aaa.bbb.ccc", expiresIn: expired)
      )
    }
  }
}
