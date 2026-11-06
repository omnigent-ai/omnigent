import XCTest

@testable import Omnigent

@MainActor
final class DatabricksSessionClientTests: XCTestCase {
  func testCreatesSessionAndSendsScopedCookiesWithoutForwardingBearer() async throws {
    let server = OAuthTestServer { request in
      if request.url!.path == "/auth/session/create" {
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-access")
        XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
        XCTAssertEqual(items.first { $0.name == "next_url" }?.value, "/omnigent/c/abc?o=123")
        return .init(
          status: 302,
          headers: ["Location": "/omnigent/c/abc?o=123", "Set-Cookie": sessionCookieHeader()])
      }
      XCTAssertEqual(request.url!.path, "/omnigent/c/abc")
      XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
      XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "DBAUTH=synthetic-session")
      return .init()
    }
    let context = try webContext(
      server.workspaceURL.absoluteString + "/omnigent/c/abc?o=123#message")
    let result = try await DatabricksSessionClient(session: server.session).create(
      context: context, tokens: credentialTokens())
    XCTAssertEqual(result.pageURL, context.pageURL)
    let cookie = try XCTUnwrap(result.cookies.first)
    XCTAssertTrue(cookie.isHTTPOnly)
    XCTAssertTrue(cookie.isSecure)
    XCTAssertNotNil(cookie.expiresDate)
    XCTAssertEqual(cookie.properties?[.sameSitePolicy] as? String, "lax")
  }

  func testCanonicalToSharedHostRedirectKeepsDomainAndWorkspaceContext() async throws {
    let landing = OAuthTestServer { request in
      XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
      XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
      return .init(headers: ["Set-Cookie": sessionCookieHeader(domain: request.url!.host!)])
    }
    let authority = OAuthTestServer { request in
      XCTAssertEqual(request.url!.path, "/auth/session/create")
      XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-access")
      return .init(
        status: 302, headers: ["Location": landing.workspaceURL.absoluteString + "/omnigent?o=123"])
    }
    let context = try webContext(authority.workspaceURL.absoluteString + "/omnigent/c/abc#message")
    let issuer = try DatabricksOAuthIssuer(authority.workspaceURL.appendingPathComponent("oidc"))
    let result = try await DatabricksSessionClient(session: authority.session).create(
      context: context, tokens: credentialTokens(issuer: issuer))
    XCTAssertEqual(
      result.pageURL.absoluteString,
      landing.workspaceURL.absoluteString + "/omnigent/c/abc?o=123#message")
    XCTAssertEqual(result.workspaceID, "123")
    XCTAssertEqual(result.cookies.first?.domain, "." + landing.workspaceURL.host!)
    XCTAssertEqual(context.scope.workspaceOrigin, authority.workspaceURL)
    XCTAssertNotNil(result.navigationURL(for: context.pageURL))
  }

  func testWorkspaceTokenUsesIssuerOriginButAccountTokenUsesChosenOrigin() async throws {
    let selected = OAuthTestServer { _ in .init(headers: ["Set-Cookie": sessionCookieHeader()]) }
    let issuerHost = OAuthTestServer { request in
      XCTAssertEqual(request.url!.path, "/auth/session/create")
      return .init(
        status: 302, headers: ["Location": selected.workspaceURL.absoluteString + "/omnigent?o=123"]
      )
    }
    let context = try webContext(selected.workspaceURL.absoluteString + "/omnigent?o=123")
    let client = DatabricksSessionClient(session: selected.session)
    let workspaceIssuer = try DatabricksOAuthIssuer(
      issuerHost.workspaceURL.appendingPathComponent("oidc"))
    let workspace = try await client.create(
      context: context, tokens: credentialTokens(issuer: workspaceIssuer))
    XCTAssertEqual(workspace.pageURL, context.pageURL)
    let accountIssuer = try DatabricksOAuthIssuer(
      issuerHost.workspaceURL.appendingPathComponent("oidc/accounts/test-account"))
    let account = try await client.create(
      context: context, tokens: credentialTokens(issuer: accountIssuer))
    XCTAssertEqual(account.pageURL, context.pageURL)
  }

  func testAccountGrantWithoutWorkspaceContextDoesNotChooseADefaultWorkspace() async throws {
    let server = OAuthTestServer { _ in
      XCTFail("No session request should be sent without workspace context")
      return .init()
    }
    let issuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/test-account")!)
    do {
      _ = try await DatabricksSessionClient(session: server.session).create(
        context: webContext(server.workspaceURL.absoluteString),
        tokens: credentialTokens(issuer: issuer))
      XCTFail("Expected workspace context requirement")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .workspaceRequired) }
  }

  func testBlocksUnsafeRedirectsAndWorkspaceChanges() async throws {
    for (location, expected) in [
      ("https://example.org/omnigent", DatabricksSessionError.unsafeRedirect),
      ("http://workspace.databricks.com/omnigent", .unsafeRedirect),
      ("/omnigent?o=456", .workspaceChanged),
      ("//user@workspace.databricks.com/omnigent", .unsafeRedirect),
    ] {
      let server = OAuthTestServer { _ in .init(status: 302, headers: ["Location": location]) }
      do {
        _ = try await DatabricksSessionClient(session: server.session).create(
          context: webContext(server.workspaceURL.absoluteString + "/omnigent?o=123"),
          tokens: credentialTokens())
        XCTFail("Expected redirect rejection")
      } catch { XCTAssertEqual(error as? DatabricksSessionError, expected) }
    }
  }

  func testRedirectLoopIsBounded() async throws {
    let requested = expectation(description: "bounded redirects")
    requested.expectedFulfillmentCount = 9
    requested.assertForOverFulfill = true
    let server = OAuthTestServer { _ in
      requested.fulfill()
      return .init(status: 302, headers: ["Location": "/again"])
    }
    do {
      _ = try await DatabricksSessionClient(session: server.session).create(
        context: webContext(server.workspaceURL.absoluteString), tokens: credentialTokens())
      XCTFail("Expected redirect limit")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .unsafeRedirect) }
    await fulfillment(of: [requested], timeout: 2)
  }

  func testMissingExpiredAndInsecureSessionCookiesFailClosed() async throws {
    for header in [
      nil, "DBAUTH=; Path=/; Secure; HttpOnly",
      "DBAUTH=expired; Path=/; Secure; HttpOnly; Max-Age=0", "DBAUTH=insecure; Path=/; HttpOnly",
      "DBAUTH=readable; Path=/; Secure",
    ] {
      let final = OAuthTestServer { request in
        if request.url!.path == "/auth/session/create" {
          return .init(status: 302, headers: ["Location": "/omnigent"])
        }
        return .init(headers: header.map { ["Set-Cookie": $0] } ?? [:])
      }
      do {
        _ = try await DatabricksSessionClient(session: final.session).create(
          context: webContext(final.workspaceURL.absoluteString), tokens: credentialTokens())
        XCTFail("Expected invalid/missing cookie")
      } catch {
        XCTAssertTrue(
          [DatabricksSessionError.missingCookie, .unsafeCookie].contains(
            error as? DatabricksSessionError ?? .invalidResponse))
      }
    }
  }

  func testExpiredDeletionCookieCanPrecedeFreshSession() async throws {
    let server = OAuthTestServer { request in
      if request.url!.path == "/auth/session/create" {
        return .init(
          status: 302,
          headers: ["Location": "/omnigent", "Set-Cookie": "DBAUTH=; Path=/; Max-Age=0"])
      }
      return .init(headers: ["Set-Cookie": sessionCookieHeader()])
    }
    let result = try await DatabricksSessionClient(session: server.session).create(
      context: webContext(server.workspaceURL.absoluteString), tokens: credentialTokens())
    XCTAssertEqual(
      result.cookies.filter { $0.name == "DBAUTH" }.map(\.value), ["synthetic-session"])
  }

  func testRejectsCookieDomainWideningAndMatchesCookiePaths() throws {
    let url = URL(string: "https://workspace.cloud.databricks.com/omnigent")!
    for domain in [
      "example.org", "sibling.cloud.databricks.com", ".com", "databricks.com.example.org",
    ] {
      let response = HTTPURLResponse(
        url: url, statusCode: 200, httpVersion: nil,
        headerFields: ["Set-Cookie": sessionCookieHeader(domain: domain)])!
      XCTAssertThrowsError(try DatabricksSessionClient.cookies(from: response)) {
        XCTAssertEqual($0 as? DatabricksSessionError, .unsafeCookie)
      }
    }
    let cookie = try testSessionCookie(url: url, path: "/omnigent")
    XCTAssertTrue(
      DatabricksSessionClient.cookie(cookie, appliesTo: url.appendingPathComponent("c/abc")))
    XCTAssertFalse(
      DatabricksSessionClient.cookie(
        cookie, appliesTo: URL(string: "https://workspace.cloud.databricks.com/omnigent-other")!))
    XCTAssertFalse(
      DatabricksSessionClient.cookie(
        cookie, appliesTo: URL(string: "https://sibling.cloud.databricks.com/omnigent")!))
  }

  func testErrorStatusAndLoginPageAreNotSuccessfulSessions() async throws {
    for status in [401, 403, 500] {
      let server = OAuthTestServer { _ in .init(status: status) }
      do {
        _ = try await DatabricksSessionClient(session: server.session).create(
          context: webContext(server.workspaceURL.absoluteString), tokens: credentialTokens())
        XCTFail("Expected server rejection")
      } catch { XCTAssertEqual(error as? DatabricksSessionError, .rejected(status)) }
    }
    let server = OAuthTestServer { request in
      if request.url!.path == "/auth/session/create" {
        return .init(status: 302, headers: ["Location": "/login"])
      }
      return .init(headers: ["Set-Cookie": sessionCookieHeader()])
    }
    do {
      _ = try await DatabricksSessionClient(session: server.session).create(
        context: webContext(server.workspaceURL.absoluteString), tokens: credentialTokens())
      XCTFail("Expected login landing rejection")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .unexpectedLanding) }
  }

  func testWorkspaceSessionVerdictProbesIdentityWithCookieAndKeepsWorkspaceSelector() async throws {
    let server = OAuthTestServer { request in
      XCTAssertEqual(request.url!.path, "/omnigent/v1/me")
      XCTAssertEqual(request.url!.query, "o=123")
      XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "DBAUTH=synthetic-session")
      XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/json")
      XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
      return .init()
    }
    let page = URL(string: server.workspaceURL.absoluteString + "/omnigent/c/abc?o=123#message")!
    let cookie = try testSessionCookie(url: server.workspaceURL)
    let verdict = await DatabricksSessionClient(session: server.session).verify(
      pageURL: page, cookie: cookie)
    XCTAssertEqual(verdict, .accepted)
  }

  func testWorkspaceSessionVerdictClassifiesRejections() async throws {
    try await assertVerdict({ _ in .init(status: 401) }, .expired)
    try await assertVerdict({ _ in .init(status: 403) }, .forbidden)
    try await assertVerdict(
      { _ in .init(status: 303, headers: ["Location": "/login.html?next_url=%2Fomnigent"]) },
      .expired)
  }

  func testWorkspaceSessionVerdictKeepsSessionWhenAnswerIsUnclear() async throws {
    try await assertVerdict(
      { _ in .init(status: 302, headers: ["Location": "/omnigent/elsewhere"]) }, .unknown)
    try await assertVerdict({ _ in .init(status: 503) }, .unknown)
    try await assertVerdict({ _ in throw URLError(.notConnectedToInternet) }, .unknown)
  }

  private func assertVerdict(
    _ handler: @escaping OAuthTestServer.Handler, _ expected: DatabricksSessionVerdict
  ) async throws {
    let server = OAuthTestServer(handler: handler)
    let cookie = try testSessionCookie(url: server.workspaceURL)
    let verdict = await DatabricksSessionClient(session: server.session).verify(
      pageURL: server.workspaceURL.appendingPathComponent("omnigent"), cookie: cookie)
    XCTAssertEqual(verdict, expected)
  }
}
