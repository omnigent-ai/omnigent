import XCTest

@testable import Omnigent

final class DatabricksOAuthClientTests: XCTestCase {
  func testDecodesOpaqueTokensAndUsesResponseExpiry() throws {
    let now = Date(timeIntervalSince1970: 1_000)
    let tokens = try DatabricksOAuthClient.tokens(from: OAuthTestServer.tokenData, requestedAt: now)
    XCTAssertEqual(tokens.accessToken, "opaque-access")
    XCTAssertEqual(tokens.refreshToken, "opaque-refresh")
    XCTAssertEqual(tokens.expiresAt, now.addingTimeInterval(120))
  }

  func testRejectsIncompleteOrInvalidTokenResponses() throws {
    let valid: [String: Any] = [
      "access_token": "access", "refresh_token": "refresh", "token_type": "Bearer",
      "expires_in": 120,
    ]
    for key in valid.keys {
      var incomplete = valid
      incomplete.removeValue(forKey: key)
      assertInvalid(try JSONSerialization.data(withJSONObject: incomplete))
    }
    for (key, value) in [
      ("access_token", ""), ("refresh_token", " "), ("access_token", "line\nbreak"),
      ("token_type", "Basic"),
    ] {
      var invalid = valid
      invalid[key] = value
      assertInvalid(try JSONSerialization.data(withJSONObject: invalid))
    }
    for expiry in [0, -1] {
      var invalid = valid
      invalid["expires_in"] = expiry
      assertInvalid(try JSONSerialization.data(withJSONObject: invalid))
    }
    assertInvalid(Data("not json".utf8))
  }

  func testExchangeUsesIsolatedSession() async throws {
    let server = OAuthTestServer { request in
      XCTAssertEqual(request.httpMethod, "POST")
      XCTAssertEqual(request.url?.path, "/oidc/v1/token")
      XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
      return .init(data: OAuthTestServer.tokenData)
    }
    let sessionConfig = server.session.configuration
    XCTAssertNil(sessionConfig.httpCookieStorage)
    XCTAssertNil(sessionConfig.urlCredentialStorage)
    XCTAssertNil(sessionConfig.urlCache)
    XCTAssertFalse(sessionConfig.httpShouldSetCookies)
    let attempt = try server.attempt()
    let tokens = try await DatabricksOAuthClient(session: server.session).exchange(
      code: "code", for: attempt)
    XCTAssertEqual(tokens.accessToken, "opaque-access")
  }

  func testHTTPFailuresAndUnexpectedResponseOriginsAreRejected() async throws {
    for status in [302, 400, 401, 403, 500] {
      let server = OAuthTestServer { _ in
        .init(status: status, data: Data("sensitive response".utf8))
      }
      do {
        _ = try await DatabricksOAuthClient(session: server.session).exchange(
          code: "code", for: server.attempt())
        XCTFail("Expected HTTP failure")
      } catch {
        XCTAssertEqual(error as? DatabricksOAuthError, .tokenExchangeFailed)
        XCTAssertFalse(error.localizedDescription.contains("sensitive"))
      }
    }
    let server = OAuthTestServer { _ in
      .init(data: OAuthTestServer.tokenData, url: URL(string: "https://example.org/token"))
    }
    do {
      _ = try await DatabricksOAuthClient(session: server.session).exchange(
        code: "code", for: server.attempt())
      XCTFail("Expected unexpected response URL to be rejected")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .tokenExchangeFailed)
    }
  }

  func testNetworkErrorsAreSanitized() async throws {
    let server = OAuthTestServer { _ in throw URLError(.notConnectedToInternet) }
    do {
      _ = try await DatabricksOAuthClient(session: server.session).exchange(
        code: "code", for: server.attempt())
      XCTFail("Expected network failure")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .networkUnavailable)
    }
  }

  func testRefusesRealSameAndCrossOriginRedirects() async throws {
    let destination = try MockHTTPServer { _, _ in
      XCTFail("Token transport must not follow a redirect")
      return (200, [:], OAuthTestServer.tokenData)
    }
    let origin = try MockHTTPServer { _, path in
      if path == "/same" { return (307, ["location": "/unexpected"], Data()) }
      if path == "/cross" {
        return (307, ["location": "http://localhost:\(destination.port)/token"], Data())
      }
      XCTFail("Token transport must not follow a same-origin redirect")
      return (200, [:], OAuthTestServer.tokenData)
    }
    let session = DatabricksOAuthClient.makeSession()
    defer { session.invalidateAndCancel() }
    for path in ["same", "cross"] {
      var request = URLRequest(url: URL(string: "http://localhost:\(origin.port)/\(path)")!)
      request.httpMethod = "POST"
      request.httpBody = Data("synthetic-test-body".utf8)
      let (_, response) = try await session.data(for: request)
      XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 307)
      XCTAssertEqual(response.url, request.url)
    }
  }

  func testRefreshFormAndRotatedResponse() async throws {
    let server = OAuthTestServer { request in
      XCTAssertEqual(request.httpMethod, "POST")
      XCTAssertEqual(request.url?.path, "/oidc/v1/token")
      XCTAssertNil(request.url?.query)
      XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
      let form = try Self.form(request)
      XCTAssertEqual(
        form,
        [
          "grant_type": "refresh_token", "client_id": "test-client",
          "refresh_token": "old+/=&refresh",
        ])
      return .init(data: OAuthTestServer.tokenData)
    }
    let scope = try server.attempt().credentialScope
    let tokens = try await DatabricksOAuthClient(session: server.session).refresh(
      "old+/=&refresh", for: scope)
    XCTAssertEqual(tokens.refreshToken, "opaque-refresh")
  }

  func testRefreshRetainsOmittedTokenButRejectsMalformedReplacement() throws {
    let data = Data(#"{"access_token":"new-access","token_type":"Bearer","expires_in":120}"#.utf8)
    let tokens = try DatabricksOAuthClient.tokens(
      from: data, requestedAt: Date(), previousRefreshToken: "old-refresh")
    XCTAssertEqual(tokens.refreshToken, "old-refresh")
    assertInvalid(data)
    for invalid: Any in ["", " ", NSNull(), 42] {
      let response: [String: Any] = [
        "access_token": "new-access", "token_type": "Bearer", "expires_in": 120,
        "refresh_token": invalid,
      ]
      let data = try JSONSerialization.data(withJSONObject: response)
      XCTAssertThrowsError(
        try DatabricksOAuthClient.tokens(
          from: data, requestedAt: Date(), previousRefreshToken: "old-refresh")
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidTokenResponse)
      }
    }
  }

  func testOnlyValidatedInvalidGrantResponseInvalidatesRefresh() async throws {
    for (status, body, expected): (Int, String, DatabricksOAuthError) in [
      (400, #"{"error":"invalid_grant","error_description":"private"}"#, .invalidRefreshGrant),
      (400, #"{"error":"invalid_client"}"#, .tokenExchangeFailed),
      (401, #"{"error":"invalid_client"}"#, .tokenExchangeFailed),
      (403, "denied", .tokenExchangeFailed),
      (429, "rate limited", .tokenExchangeFailed),
      (500, #"{"error":"invalid_grant"}"#, .tokenExchangeFailed),
      (400, "not json", .tokenExchangeFailed),
    ] {
      let server = OAuthTestServer { _ in .init(status: status, data: Data(body.utf8)) }
      do {
        _ = try await DatabricksOAuthClient(session: server.session).refresh(
          "old-refresh", for: server.attempt().credentialScope)
        XCTFail("Expected refresh error")
      } catch {
        XCTAssertEqual(error as? DatabricksOAuthError, expected)
        XCTAssertFalse(error.localizedDescription.contains("private"))
      }
    }
  }

  private static func form(_ request: URLRequest) throws -> [String: String] {
    var data = request.httpBody ?? Data()
    if let stream = request.httpBodyStream {
      stream.open()
      defer { stream.close() }
      var buffer = [UInt8](repeating: 0, count: 1024)
      while true {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count <= 0 { break }
        data.append(buffer, count: count)
      }
    }
    let body = try XCTUnwrap(String(data: data, encoding: .utf8))
    return Dictionary(
      uniqueKeysWithValues: body.split(separator: "&").map {
        let pair = $0.split(separator: "=", maxSplits: 1).map(String.init)
        return (pair[0], pair[1].removingPercentEncoding!)
      })
  }

  private func assertInvalid(_ data: Data, file: StaticString = #filePath, line: UInt = #line) {
    XCTAssertThrowsError(
      try DatabricksOAuthClient.tokens(from: data, requestedAt: Date()), file: file, line: line
    ) {
      XCTAssertEqual($0 as? DatabricksOAuthError, .invalidTokenResponse, file: file, line: line)
    }
  }
}

/// Each fixture has a unique host; the stub never falls through to live networking.
final class OAuthTestServer {
  struct Response {
    var status = 200
    var data = Data()
    var url: URL?
  }
  typealias Handler = (URLRequest) throws -> Response?
  static let tokenData = Data(
    #"{"access_token":"opaque-access","refresh_token":"opaque-refresh","token_type":"Bearer","expires_in":120}"#
      .utf8)
  let workspaceURL: URL
  let session: URLSession

  init(handler: @escaping Handler = { _ in Response(data: tokenData) }) {
    workspaceURL = URL(
      string: "https://test-\(UUID().uuidString.lowercased()).cloud.databricks.com")!
    OAuthURLProtocol.setHandler(handler, for: workspaceURL.host!)
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [OAuthURLProtocol.self]
    session = DatabricksOAuthClient.makeSession(configuration: configuration)
  }

  deinit {
    session.invalidateAndCancel()
    OAuthURLProtocol.setHandler(nil, for: workspaceURL.host!)
  }

  func attempt() throws -> DatabricksOAuthAttempt {
    try DatabricksOAuthAttempt(
      workspaceURL: workspaceURL,
      configuration: DatabricksOAuthConfiguration(
        clientID: "test-client", redirectURL: "https://login.databricks.com/mobile-redirect"))
  }
}

private final class OAuthURLProtocol: URLProtocol {
  private static let lock = NSLock()
  private static var handlers: [String: OAuthTestServer.Handler] = [:]

  static func setHandler(_ handler: OAuthTestServer.Handler?, for host: String) {
    lock.lock()
    defer { lock.unlock() }
    handlers[host] = handler
  }

  override class func canInit(with request: URLRequest) -> Bool { true }
  override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

  override func startLoading() {
    Self.lock.lock()
    let handler = request.url?.host.flatMap { Self.handlers[$0] }
    Self.lock.unlock()
    do {
      guard let handler else { throw URLError(.unsupportedURL) }
      guard let result = try handler(request) else { return }
      let response = HTTPURLResponse(
        url: result.url ?? request.url!, statusCode: result.status,
        httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
      client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
      client?.urlProtocol(self, didLoad: result.data)
      client?.urlProtocolDidFinishLoading(self)
    } catch {
      client?.urlProtocol(self, didFailWithError: error)
    }
  }

  override func stopLoading() {}
}
