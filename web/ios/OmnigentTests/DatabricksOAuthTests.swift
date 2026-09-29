import XCTest

@testable import Omnigent

final class DatabricksOAuthConfigurationTests: XCTestCase {
  func testAcceptsBuildConfigurationOverrides() throws {
    let configuration = try DatabricksOAuthConfiguration(
      clientID: "custom-client", redirectURL: "https://auth.example.com/mobile/callback")
    XCTAssertEqual(configuration.clientID, "custom-client")
    XCTAssertEqual(
      configuration.redirectURL.absoluteString, "https://auth.example.com/mobile/callback")
  }

  func testRejectsMissingOrUnexpandedClientID() {
    for clientID in ["", " ", "client\nvalue", "$(DATABRICKS_OAUTH_CLIENT_ID)"] {
      XCTAssertThrowsError(
        try DatabricksOAuthConfiguration(
          clientID: clientID, redirectURL: "https://login.databricks.com/mobile-redirect")
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidClientID)
      }
    }
  }

  func testRequiresUnambiguousHTTPSCallback() {
    for redirect in [
      "", "$(DATABRICKS_OAUTH_REDIRECT_URL)", "http://example.com/callback",
      "omnigent://auth/callback", "https://example.com", "https://user@example.com/callback",
      "https://example.com:8443/callback", "https://example.com/callback?next=foo",
      "https://example.com/callback#fragment", "https://example.com/call back",
    ] {
      XCTAssertThrowsError(
        try DatabricksOAuthConfiguration(clientID: "client", redirectURL: redirect)
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidRedirectURL)
      }
    }
  }

  func testReadsProcessedBuildSettings() throws {
    let info = Bundle.main.infoDictionary ?? [:]
    let clientID = try XCTUnwrap(info["DatabricksOAuthClientID"] as? String)
    let redirect = try XCTUnwrap(info["DatabricksOAuthRedirectURL"] as? String)
    XCTAssertFalse(clientID.contains("$("))
    XCTAssertFalse(redirect.contains("$("))
    if clientID.isEmpty {
      XCTAssertThrowsError(try DatabricksOAuthConfiguration.load()) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidClientID)
      }
    } else {
      let config = try DatabricksOAuthConfiguration.load()
      XCTAssertEqual(config.clientID, clientID)
      XCTAssertEqual(config.redirectURL.absoluteString, redirect)
    }
  }
}

final class DatabricksOAuthAttemptTests: XCTestCase {
  func testUsesWorkspaceIssuerAndPublicClientPKCE() throws {
    let config = try configuration(clientID: "client+&value")
    let attempt = try DatabricksOAuthAttempt(
      workspaceURL: URL(
        string: "https://DBC-123.cloud.databricks.com/omnigent/c/abc?o=123&view=chat#view")!,
      configuration: config)
    let components = try XCTUnwrap(
      URLComponents(url: attempt.authorizationURL, resolvingAgainstBaseURL: false))
    XCTAssertEqual(components.host, "dbc-123.cloud.databricks.com")
    XCTAssertEqual(components.path, "/oidc/v1/authorize")
    let query = Dictionary(
      uniqueKeysWithValues: components.queryItems!.map { ($0.name, $0.value!) })
    XCTAssertEqual(query["client_id"], config.clientID)
    XCTAssertEqual(query["redirect_uri"], config.redirectURL.absoluteString)
    XCTAssertEqual(query["response_type"], "code")
    XCTAssertEqual(query["scope"], "all-apis offline_access")
    XCTAssertEqual(query["state"], attempt.state)
    XCTAssertEqual(query["code_challenge"], DatabricksOAuthAttempt.challenge(for: attempt.verifier))
    XCTAssertEqual(query["code_challenge_method"], "S256")
    XCTAssertNil(query["code_verifier"])
    XCTAssertEqual(query["o"], "123")
    XCTAssertNil(query["view"])
    XCTAssertFalse(components.percentEncodedQuery!.contains("+"))
  }

  func testPKCEMatchesRFC7636Vector() {
    XCTAssertEqual(
      DatabricksOAuthAttempt.challenge(for: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
  }

  func testEachAttemptHasFreshIndependentStateAndVerifier() throws {
    let first = try attempt()
    let second = try attempt()
    XCTAssertNotEqual(first.state, second.state)
    XCTAssertNotEqual(first.verifier, second.verifier)
    XCTAssertNotEqual(first.state, first.verifier)
    let allowed = CharacterSet(
      charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    for value in [first.state, first.verifier, second.state, second.verifier] {
      XCTAssertEqual(value.count, 43)
      XCTAssertTrue(value.unicodeScalars.allSatisfy(allowed.contains))
    }
  }

  func testRejectsNonWorkspaceAndInsecureIssuers() throws {
    let config = try configuration()
    for raw in [
      "http://dbc-123.cloud.databricks.com", "https://my-app.databricksapps.com",
      "https://example.com", "https://databricks.com.example.org",
      "https://user@dbc-123.cloud.databricks.com", "https://dbc-123.cloud.databricks.com:8443",
    ] {
      XCTAssertThrowsError(
        try DatabricksOAuthAttempt(workspaceURL: URL(string: raw)!, configuration: config)
      ) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidWorkspace)
      }
    }
  }

  func testValidatesConfiguredCallbackAndState() throws {
    let attempt = try attempt()
    let callback = URL(
      string:
        "https://login.databricks.com/mobile-redirect?state=\(attempt.state)&code=code%2Bvalue")!
    XCTAssertEqual(try attempt.authorizationCode(from: callback), "code+value")
    for raw in [
      "https://example.org/mobile-redirect?state=\(attempt.state)&code=value",
      "http://login.databricks.com/mobile-redirect?state=\(attempt.state)&code=value",
      "https://login.databricks.com:8443/mobile-redirect?state=\(attempt.state)&code=value",
      "https://user@login.databricks.com/mobile-redirect?state=\(attempt.state)&code=value",
      "https://login.databricks.com/mobile-redirect/extra?state=\(attempt.state)&code=value",
      "https://login.databricks.com/mobile-redirect?state=wrong&code=value",
      "https://login.databricks.com/mobile-redirect?state=\(attempt.state)&code=value#fragment",
    ] {
      XCTAssertThrowsError(try attempt.authorizationCode(from: URL(string: raw)!)) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidCallback)
      }
    }
  }

  func testRejectsMissingDuplicateAndMixedCallbackParameters() throws {
    let attempt = try attempt()
    for query in [
      "code=value", "state=\(attempt.state)", "state=\(attempt.state)&code=",
      "state=\(attempt.state)&state=\(attempt.state)&code=value",
      "state=\(attempt.state)&code=one&code=two",
      "state=\(attempt.state)&code=value&error=access_denied",
      "state=\(attempt.state)&error=", "state=\(attempt.state)&error=one&error=two",
    ] {
      let callback = URL(string: "https://login.databricks.com/mobile-redirect?\(query)")!
      XCTAssertThrowsError(try attempt.authorizationCode(from: callback)) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidCallback)
      }
    }
  }

  func testProviderErrorsAreSanitizedAndStillRequireState() throws {
    let attempt = try attempt()
    for (error, expected) in [
      ("access_denied", DatabricksOAuthError.authorizationDenied),
      ("server_error", .authorizationFailed),
    ] {
      let callback = URL(
        string:
          "https://login.databricks.com/mobile-redirect?state=\(attempt.state)&error=\(error)&error_description=sensitive-value"
      )!
      XCTAssertThrowsError(try attempt.authorizationCode(from: callback)) {
        XCTAssertEqual($0 as? DatabricksOAuthError, expected)
        XCTAssertFalse($0.localizedDescription.contains("sensitive-value"))
      }
    }
    XCTAssertThrowsError(
      try attempt.authorizationCode(
        from: URL(string: "https://login.databricks.com/mobile-redirect?error=access_denied")!)
    ) {
      XCTAssertEqual($0 as? DatabricksOAuthError, .invalidCallback)
    }
  }

  func testExchangeFormEncodesOpaqueValuesWithoutSecretsInURL() throws {
    let attempt = try DatabricksOAuthAttempt(
      workspaceURL: URL(string: "https://adb-123.azuredatabricks.net/omnigent")!,
      configuration: configuration(clientID: "client+&value"))
    let request = attempt.tokenRequest(code: "code+/=& value")
    XCTAssertEqual(request.url?.absoluteString, "https://adb-123.azuredatabricks.net/oidc/v1/token")
    XCTAssertEqual(request.httpMethod, "POST")
    XCTAssertEqual(
      request.value(forHTTPHeaderField: "Content-Type"), "application/x-www-form-urlencoded")
    XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    let body = String(data: try XCTUnwrap(request.httpBody), encoding: .utf8)!
    let pairs = body.split(separator: "&").map {
      $0.split(separator: "=", maxSplits: 1).map(String.init)
    }
    let fields = Dictionary(
      uniqueKeysWithValues: pairs.map { ($0[0], $0[1].removingPercentEncoding!) })
    XCTAssertEqual(fields["code"], "code+/=& value")
    XCTAssertEqual(fields["code_verifier"], attempt.verifier)
    XCTAssertEqual(fields["client_id"], "client+&value")
    XCTAssertEqual(fields["redirect_uri"], attempt.configuration.redirectURL.absoluteString)
    XCTAssertEqual(fields["scope"], "all-apis offline_access")
    XCTAssertEqual(fields["grant_type"], "authorization_code")
    XCTAssertNil(fields["client_secret"])
    XCTAssertFalse(body.contains("+"))
  }

  private func configuration(clientID: String = "test-client") throws
    -> DatabricksOAuthConfiguration
  {
    try DatabricksOAuthConfiguration(
      clientID: clientID, redirectURL: "https://login.databricks.com/mobile-redirect")
  }

  private func attempt() throws -> DatabricksOAuthAttempt {
    try DatabricksOAuthAttempt(
      workspaceURL: URL(string: "https://dbc-123.cloud.databricks.com")!,
      configuration: configuration())
  }
}
