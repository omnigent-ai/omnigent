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

  func testTokenRejectsCookieInjectionCharacters() throws {
    let valid = try XCTUnwrap(#"{"token":"aaa.bbb.ccc"}"#.data(using: .utf8))
    XCTAssertEqual(try OidcLoginManager.token(from: valid), "aaa.bbb.ccc")

    for token in ["aaa.bbb.ccc; Domain=evil.example", "aaa.bbb", "aaa..ccc"] {
      let data = try XCTUnwrap(#"{"token":"\#(token)"}"#.data(using: .utf8))
      XCTAssertThrowsError(try OidcLoginManager.token(from: data))
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
}
