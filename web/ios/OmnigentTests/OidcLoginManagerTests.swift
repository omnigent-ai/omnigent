import XCTest

@testable import Omnigent

final class OidcLoginManagerTests: XCTestCase {
  func testTicketAndPollSendMatchingPKCEProof() async throws {
    let verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    let challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    XCTAssertEqual(OidcLoginManager.deriveCodeChallenge(verifier), challenge)
    let server = try MockHTTPServer(requestHandler: { request in
      switch request.path {
      case "/auth/cli-login":
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.headers["content-type"], "application/json")
        let body = try? JSONSerialization.jsonObject(with: request.body) as? [String: String]
        XCTAssertEqual(body, ["code_challenge": challenge, "code_challenge_method": "S256"])
        return (
          200, [:], Data(#"{"ticket":"t-1","login_url":"/auth/cli-consent?ticket=t-1"}"#.utf8)
        )
      case "/auth/cli-poll?ticket=t-1":
        XCTAssertEqual(request.method, "GET")
        XCTAssertEqual(request.headers["x-omnigent-code-verifier"], verifier)
        XCTAssertTrue(request.body.isEmpty)
        return (200, [:], Data(#"{"token":"aaa.bbb.ccc"}"#.utf8))
      default:
        XCTFail("Unexpected login request: \(request.path)")
        return (400, [:], Data())
      }
    })
    defer { withExtendedLifetime(server) {} }
    let origin = try XCTUnwrap(URL(string: "http://localhost:\(server.port)"))
    let ticket = try await OidcLoginManager.requestTicket(
      origin: origin, codeChallenge: OidcLoginManager.deriveCodeChallenge(verifier))
    let token = try await OidcLoginManager.pollForToken(
      origin: origin, ticket: ticket.id, codeVerifier: verifier)
    XCTAssertEqual(token, "aaa.bbb.ccc")
  }

  func testDeniedPollReturnsNoToken() async throws {
    for status in [400, 403, 410] {
      var requestCount = 0
      let server = try MockHTTPServer { method, path in
        XCTAssertEqual(method, "GET")
        XCTAssertEqual(path, "/auth/cli-poll?ticket=denied")
        requestCount += 1
        // A retry would accept this token and fail the terminal-response assertion.
        return (
          requestCount == 1 ? status : 200, [:], Data(#"{"token":"aaa.bbb.ccc"}"#.utf8)
        )
      }
      defer { withExtendedLifetime(server) {} }
      let origin = try XCTUnwrap(URL(string: "http://localhost:\(server.port)"))
      let token = try await OidcLoginManager.pollForToken(
        origin: origin, ticket: "denied", codeVerifier: OidcLoginManager.generateCodeVerifier())
      XCTAssertNil(token, "HTTP \(status) must end polling without accepting a token")
      XCTAssertEqual(requestCount, 1)
    }
  }

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
