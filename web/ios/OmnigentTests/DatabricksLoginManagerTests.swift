import AuthenticationServices
import Security
import UIKit
import XCTest

@testable import Omnigent

@MainActor
final class DatabricksLoginManagerTests: XCTestCase {
  func testCompletesHTTPSCallbackAndExchangesExactlyOnce() async throws {
    let requested = expectation(description: "one token request")
    requested.assertForOverFulfill = true
    let harness = try Harness(
      server: OAuthTestServer { _ in
        requested.fulfill()
        return .init(data: OAuthTestServer.tokenData)
      }, redirectURL: "https://auth.example.com/custom/callback")
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    let browser = try XCTUnwrap(harness.browsers.first)
    XCTAssertTrue(harness.manager.isInFlight)
    XCTAssertTrue(browser.callback.matchesURL(harness.configuration.redirectURL))
    XCTAssertFalse(
      browser.callback.matchesURL(URL(string: "https://other.example.com/custom/callback")!))
    browser.succeed(redirect: harness.configuration.redirectURL)
    browser.succeed(redirect: harness.configuration.redirectURL)
    let tokens = try await task.value
    await fulfillment(of: [requested], timeout: 2)
    XCTAssertEqual(tokens.accessToken, "opaque-access")
    let scope = try DatabricksCredentialScope(
      workspaceURL: harness.server.workspaceURL, configuration: harness.configuration)
    XCTAssertEqual(harness.credentials.snapshot(for: scope), tokens)
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testRejectsConcurrentLogin() async throws {
    let harness = try Harness()
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let first = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    do {
      _ = try await harness.signIn().value
      XCTFail("Expected single-flight rejection")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .loginInProgress)
    }
    harness.manager.cancel()
    await assertCancelled(first)
  }

  func testStartFailureAndUserCancellationReleaseSession() async throws {
    let harness = try Harness()
    harness.startResult = false
    do {
      _ = try await harness.signIn().value
      XCTFail("Expected start failure")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .browserUnavailable)
    }
    XCTAssertFalse(harness.manager.isInFlight)
    harness.startResult = true
    let started = expectation(description: "browser restarted")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    harness.browsers.last!.completion(
      nil,
      NSError(
        domain: ASWebAuthenticationSessionErrorDomain,
        code: ASWebAuthenticationSessionError.Code.canceledLogin.rawValue))
    await assertCancelled(task)
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testCancellationDoesNotNeedBrowserCompletionAndIgnoresStaleCallback() async throws {
    let harness = try Harness()
    let started = expectation(description: "first browser")
    harness.onStart = { started.fulfill() }
    let first = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    let oldBrowser = harness.browsers[0]
    harness.manager.cancel()
    XCTAssertEqual(oldBrowser.cancelCount, 1)
    XCTAssertFalse(harness.manager.isInFlight)

    let restarted = expectation(description: "second browser")
    harness.onStart = { restarted.fulfill() }
    let second = harness.signIn()
    await fulfillment(of: [restarted], timeout: 2)
    await assertCancelled(first)
    oldBrowser.succeed(redirect: harness.configuration.redirectURL)
    let currentBrowser = harness.browsers[1]
    currentBrowser.succeed(redirect: harness.configuration.redirectURL)
    _ = try await second.value
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testCancellingCallerCancelsBrowser() async throws {
    let harness = try Harness()
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    task.cancel()
    await assertCancelled(task)
    XCTAssertEqual(harness.browsers[0].cancelCount, 1)
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testCancelDuringExchangeCancelsNetworkOperation() async throws {
    let requested = expectation(description: "exchange started")
    let harness = try Harness(
      server: OAuthTestServer { _ in
        requested.fulfill()
        return nil
      })
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    harness.browsers[0].succeed(redirect: harness.configuration.redirectURL)
    await fulfillment(of: [requested], timeout: 2)
    harness.manager.cancel()
    await assertCancelled(task)
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testInvalidCallbackAndBrowserFailureNeverExchangeCode() async throws {
    let harness = try Harness(
      server: OAuthTestServer { _ in
        XCTFail("Invalid authentication must not reach the token endpoint")
        return .init(data: OAuthTestServer.tokenData)
      })
    for browserError in [false, true] {
      let started = expectation(description: "browser started")
      harness.onStart = { started.fulfill() }
      let task = harness.signIn()
      await fulfillment(of: [started], timeout: 2)
      let error =
        browserError
        ? NSError(
          domain: "test", code: 1,
          userInfo: [NSLocalizedDescriptionKey: "sensitive details"]) : nil
      harness.browsers.last!.completion(
        URL(string: "https://login.databricks.com/mobile-redirect?code=value&state=wrong"), error)
      do {
        _ = try await task.value
        XCTFail("Expected invalid authentication")
      } catch {
        XCTAssertEqual(
          error as? DatabricksOAuthError, browserError ? .authenticationFailed : .invalidCallback)
        XCTAssertFalse(error.localizedDescription.contains("sensitive details"))
      }
      XCTAssertFalse(harness.manager.isInFlight)
    }
  }

  func testAlreadyCancelledCallerDoesNotPresentBrowser() async throws {
    let harness = try Harness()
    let task = harness.signIn()
    task.cancel()
    await assertCancelled(task)
    XCTAssertTrue(harness.browsers.isEmpty)
  }

  func testPersistenceFailureIsNotReportedAsSuccessfulLogin() async throws {
    let harness = try Harness()
    harness.credentials.fail(.save)
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    harness.browsers[0].succeed(redirect: harness.configuration.redirectURL)
    do {
      _ = try await task.value
      XCTFail("Expected persistence failure")
    } catch {
      XCTAssertEqual(error as? DatabricksCredentialError, .keychain(errSecInteractionNotAllowed))
    }
    XCTAssertFalse(harness.manager.isInFlight)
  }

  func testClearOrNewerLoginRejectsOldLoginCommit() async throws {
    for replaceWithLogin in [false, true] {
      let harness = try Harness()
      let scope = try DatabricksCredentialScope(
        workspaceURL: harness.server.workspaceURL, configuration: harness.configuration)
      let started = expectation(description: "browser started")
      harness.onStart = { started.fulfill() }
      let task = harness.signIn()
      await fulfillment(of: [started], timeout: 2)
      let newer = credentialTokens(access: "new-login", refresh: "new-login-refresh")
      if replaceWithLogin {
        let signIn = try await harness.tokenManager.beginSignIn(for: scope)
        try await harness.tokenManager.save(newer, for: signIn)
      } else {
        try await harness.tokenManager.clear(for: scope)
      }
      harness.browsers[0].succeed(redirect: harness.configuration.redirectURL)
      await assertCancelled(task)
      XCTAssertEqual(harness.credentials.snapshot(for: scope), replaceWithLogin ? newer : nil)
    }
  }

  func testCancelledReplacementPreservesSavedCredentials() async throws {
    let harness = try Harness()
    let scope = try DatabricksCredentialScope(
      workspaceURL: harness.server.workspaceURL, configuration: harness.configuration)
    let original = credentialTokens()
    try await harness.tokenManager.save(original, for: scope)
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    harness.manager.cancel()
    await assertCancelled(task)
    XCTAssertEqual(harness.credentials.snapshot(for: scope), original)
  }

  func testIssuerDiscoveryKeepsSelectedWorkspaceContext() async throws {
    let authority = OAuthTestServer()
    let issuer = try DatabricksOAuthIssuer(authority.workspaceURL.appendingPathComponent("oidc"))
    let harness = try Harness(
      server: OAuthTestServer { _ in
        XCTFail("Exchange must use the discovered authority")
        return .init(data: OAuthTestServer.tokenData)
      })
    let selectedURL = URL(string: harness.server.workspaceURL.absoluteString + "/omnigent?o=123")!
    let scope = try DatabricksCredentialScope(
      workspaceURL: selectedURL, configuration: harness.configuration)
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn(workspaceURL: selectedURL)
    await fulfillment(of: [started], timeout: 2)
    let browser = harness.browsers[0]
    XCTAssertEqual(
      URLComponents(url: browser.url, resolvingAgainstBaseURL: false)?.queryItems?.first {
        $0.name == "o"
      }?.value, "123")
    browser.succeed(redirect: harness.configuration.redirectURL, issuer: issuer.url.absoluteString)
    let tokens = try await task.value
    XCTAssertEqual(tokens.issuer, issuer)
    XCTAssertEqual(harness.credentials.snapshot(for: scope), tokens)
    let authorityScope = try DatabricksCredentialScope(
      workspaceURL: authority.workspaceURL, configuration: harness.configuration)
    XCTAssertNil(harness.credentials.snapshot(for: authorityScope))
  }

  func testUnsupportedCallbackIssuerNeverExchangesOrStoresTokens() async throws {
    let harness = try Harness(
      server: OAuthTestServer(automaticDiscovery: false) { _ in
        XCTFail("Unsupported issuer must not trigger any request")
        return .init(data: OAuthTestServer.tokenData)
      })
    let started = expectation(description: "browser started")
    harness.onStart = { started.fulfill() }
    let task = harness.signIn()
    await fulfillment(of: [started], timeout: 2)
    harness.browsers[0].succeed(
      redirect: harness.configuration.redirectURL, issuer: "https://example.org/oidc")
    do {
      _ = try await task.value
      XCTFail("Expected issuer rejection")
    } catch {
      XCTAssertEqual(error as? DatabricksOAuthError, .invalidIssuer)
    }
    let scope = try DatabricksCredentialScope(
      workspaceURL: harness.server.workspaceURL, configuration: harness.configuration)
    XCTAssertNil(harness.credentials.snapshot(for: scope))
  }

  private func assertCancelled(_ task: Task<DatabricksOAuthTokens, Error>) async {
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch {
      XCTAssertTrue(error is CancellationError)
    }
  }
}

@MainActor
private final class Harness {
  let server: OAuthTestServer
  let configuration: DatabricksOAuthConfiguration
  let anchor: ASPresentationAnchor
  let credentials = MemoryDatabricksCredentialStore()
  var browsers: [FakeAuthenticationSession] = []
  var startResult = true
  var onStart: (() -> Void)?
  lazy var tokenManager = DatabricksTokenManager(store: credentials)
  lazy var manager = DatabricksLoginManager(
    client: DatabricksOAuthClient(session: server.session),
    tokenManager: tokenManager
  ) {
    [unowned self] url, callback, _, completion in
    let browser = FakeAuthenticationSession(url: url, callback: callback, completion: completion)
    browser.startResult = startResult
    browser.onStart = onStart
    browsers.append(browser)
    return browser
  }

  init(
    server: OAuthTestServer = OAuthTestServer(),
    redirectURL: String = "https://login.databricks.com/mobile-redirect"
  ) throws {
    self.server = server
    let scene = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
    anchor = ASPresentationAnchor(windowScene: scene)
    configuration = try DatabricksOAuthConfiguration(
      clientID: "test-client", redirectURL: redirectURL)
  }

  func signIn(workspaceURL: URL? = nil) -> Task<DatabricksOAuthTokens, Error> {
    Task {
      try await manager.signIn(
        workspaceURL: workspaceURL ?? server.workspaceURL, configuration: configuration,
        anchor: anchor)
    }
  }
}

@MainActor
private final class FakeAuthenticationSession: DatabricksAuthenticationSession {
  let url: URL
  let callback: ASWebAuthenticationSession.Callback
  let completion: ASWebAuthenticationSession.CompletionHandler
  var startResult = true
  var cancelCount = 0
  var onStart: (() -> Void)?

  init(
    url: URL, callback: ASWebAuthenticationSession.Callback,
    completion: @escaping ASWebAuthenticationSession.CompletionHandler
  ) {
    self.url = url
    self.callback = callback
    self.completion = completion
  }

  func start() -> Bool {
    onStart?()
    return startResult
  }
  func cancel() { cancelCount += 1 }

  func succeed(redirect: URL, issuer: String? = nil) {
    let state = URLComponents(url: url, resolvingAgainstBaseURL: false)!.queryItems!.first {
      $0.name == "state"
    }!.value!
    var callback = URLComponents(url: redirect, resolvingAgainstBaseURL: false)!
    callback.queryItems = [
      URLQueryItem(name: "state", value: state), URLQueryItem(name: "code", value: "code"),
    ]
    if let issuer { callback.queryItems?.append(URLQueryItem(name: "iss", value: issuer)) }
    completion(callback.url, nil)
  }
}
