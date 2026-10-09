import XCTest

@testable import Omnigent

final class DatabricksOAuthIssuerTests: XCTestCase {
  func testPreservesWorkspaceAndAccountIssuerPaths() throws {
    for raw in [
      "https://workspace.cloud.databricks.com/oidc",
      "https://accounts.cloud.databricks.com/oidc/accounts/test-account",
      "https://accounts.azuredatabricks.net/oidc/accounts/test-account",
    ] {
      let issuer = try DatabricksOAuthIssuer(URL(string: raw)!)
      XCTAssertEqual(issuer.url.absoluteString, raw)
      XCTAssertEqual(issuer.discoveryURL.absoluteString, raw + "/.well-known/openid-configuration")
      XCTAssertEqual(issuer.tokenEndpoint.absoluteString, raw + "/v1/token")
      XCTAssertEqual(
        try JSONDecoder().decode(DatabricksOAuthIssuer.self, from: JSONEncoder().encode(issuer)),
        issuer)
    }
  }

  func testRejectsUnsupportedIssuerAuthoritiesAndPaths() {
    for raw in [
      "http://workspace.cloud.databricks.com/oidc",
      "https://example.com/oidc", "https://databricks.com.example.org/oidc",
      "https://my-app.databricksapps.com/oidc",
      "https://user@workspace.cloud.databricks.com/oidc",
      "https://workspace.cloud.databricks.com:8443/oidc",
      "https://workspace.cloud.databricks.com/oidc?next=foo",
      "https://workspace.cloud.databricks.com/oidc#fragment",
      "https://workspace.cloud.databricks.com", "https://workspace.cloud.databricks.com/other",
      "https://accounts.cloud.databricks.com/oidc/accounts/",
      "https://accounts.cloud.databricks.com/oidc/accounts/a/b",
      "https://accounts.cloud.databricks.com/oidc/accounts/a%2Fb",
      "https://accounts.cloud.databricks.com/oidc/accounts/..",
    ] {
      XCTAssertThrowsError(try DatabricksOAuthIssuer(URL(string: raw)!)) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidIssuer)
      }
    }
  }

  func testDiscoveryMustConfirmExactIssuerAndTokenEndpoint() throws {
    let issuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/test-account")!)
    let valid = [
      "issuer": issuer.url.absoluteString, "token_endpoint": issuer.tokenEndpoint.absoluteString,
    ]
    XCTAssertNoThrow(try issuer.validateDiscovery(JSONSerialization.data(withJSONObject: valid)))
    for invalid in [
      [
        "issuer": "https://example.com/oidc", "token_endpoint": issuer.tokenEndpoint.absoluteString,
      ],
      [
        "issuer": issuer.url.absoluteString,
        "token_endpoint": "https://accounts.cloud.databricks.com/oidc/v1/token",
      ],
      ["issuer": issuer.url.absoluteString, "token_endpoint": "https://example.com/token"],
      ["issuer": issuer.url.absoluteString],
    ] {
      XCTAssertThrowsError(
        try issuer.validateDiscovery(JSONSerialization.data(withJSONObject: invalid))
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidDiscovery)
      }
    }
  }

  func testCallbackIssuerIsValidatedAlongsideState() throws {
    let server = OAuthTestServer()
    let attempt = try server.attempt()
    let accountIssuer = "https://accounts.cloud.databricks.com/oidc/accounts/test-account"
    let response = try attempt.authorizationResponse(from: callback(attempt, issuer: accountIssuer))
    XCTAssertEqual(response.code, "test-code")
    XCTAssertEqual(response.issuer.url.absoluteString, accountIssuer)
    XCTAssertEqual(attempt.workspaceOrigin, server.workspaceURL)
    let fallback = try attempt.authorizationResponse(from: callback(attempt, issuer: nil))
    XCTAssertEqual(fallback.issuer.url, server.workspaceURL.appendingPathComponent("oidc"))
    for issuer in ["", "https://example.com/oidc", "http://accounts.cloud.databricks.com/oidc"] {
      XCTAssertThrowsError(
        try attempt.authorizationResponse(from: callback(attempt, issuer: issuer))
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidIssuer)
      }
    }
    let duplicate = URL(
      string: callback(attempt, issuer: accountIssuer).absoluteString + "&iss=" + accountIssuer)!
    XCTAssertThrowsError(try attempt.authorizationResponse(from: duplicate)) {
      XCTAssertEqual($0 as? DatabricksOAuthError, .invalidIssuer)
    }
    var wrongState = URLComponents(
      url: callback(attempt, issuer: accountIssuer), resolvingAgainstBaseURL: false)!
    wrongState.queryItems?.removeAll { $0.name == "state" }
    wrongState.queryItems?.append(URLQueryItem(name: "state", value: "wrong"))
    XCTAssertThrowsError(try attempt.authorizationResponse(from: wrongState.url!)) {
      XCTAssertEqual($0 as? DatabricksOAuthError, .invalidCallback)
    }
  }

  func testExchangeAndRefreshUseDiscoveredIssuerWithoutChangingDestination() async throws {
    let workspace = OAuthTestServer { _ in
      XCTFail("Token requests must target the discovered issuer, not the page host")
      return .init(data: OAuthTestServer.tokenData)
    }
    let requests = expectation(description: "exchange and refresh")
    requests.expectedFulfillmentCount = 2
    let authority = OAuthTestServer { request in
      XCTAssertEqual(request.httpMethod, "POST")
      XCTAssertEqual(request.url?.path, "/oidc/accounts/test-account/v1/token")
      XCTAssertNil(request.url?.query)
      requests.fulfill()
      return .init(data: OAuthTestServer.tokenData)
    }
    let issuer = try DatabricksOAuthIssuer(
      authority.workspaceURL.appendingPathComponent("oidc/accounts/test-account"))
    let attempt = try workspace.attempt()
    let client = DatabricksOAuthClient(session: workspace.session)
    let tokens = try await client.exchange(code: "test-code", for: attempt, issuer: issuer)
    XCTAssertEqual(tokens.accessToken, "opaque-access")
    XCTAssertEqual(tokens.issuer, issuer)
    let refreshed = try await client.refresh(
      tokens.refreshToken, for: attempt.credentialScope, issuer: tokens.issuer)
    XCTAssertEqual(refreshed.issuer, issuer)
    XCTAssertEqual(attempt.workspaceOrigin, workspace.workspaceURL)
    await fulfillment(of: [requests], timeout: 2)
  }

  func testInvalidDiscoveryNeverReceivesAuthorizationCode() async throws {
    for status in [200, 302, 500] {
      let server = OAuthTestServer(automaticDiscovery: false) { request in
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertTrue(request.url!.path.hasSuffix("/.well-known/openid-configuration"))
        XCTAssertNil(request.httpBody)
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        return .init(
          status: status,
          data: Data(
            #"{"issuer":"https://example.org/oidc","token_endpoint":"https://example.org/token"}"#
              .utf8))
      }
      do {
        _ = try await DatabricksOAuthClient(session: server.session).exchange(
          code: "test-code", for: server.attempt())
        XCTFail("Expected invalid discovery")
      } catch {
        XCTAssertEqual(error as? DatabricksOAuthError, .invalidDiscovery)
      }
    }
  }

  private func callback(_ attempt: DatabricksOAuthAttempt, issuer: String?) -> URL {
    var components = URLComponents(
      url: attempt.configuration.redirectURL, resolvingAgainstBaseURL: false)!
    components.queryItems = [
      URLQueryItem(name: "code", value: "test-code"),
      URLQueryItem(name: "state", value: attempt.state),
    ]
    if let issuer { components.queryItems?.append(URLQueryItem(name: "iss", value: issuer)) }
    return components.url!
  }
}
