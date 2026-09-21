import AuthenticationServices
import SwiftUI
import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class DatabricksWorkspaceBootstrapTests: XCTestCase {
  func testCachedGrantSkipsBrowserAndWaitsForCookieInstallation() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    let bootstrap = makeBootstrap(credentials, login: login, sessions: sessions)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    store.values = [try testSessionCookie(url: context.pageURL, value: "old")]
    store.holdNextWrite = true
    let writing = expectation(description: "cookie insertion started")
    store.onWrite = { writing.fulfill() }
    let anchor = try window()
    let task = Task { try await bootstrap.prepare(context: context, store: store, anchor: anchor) }
    await fulfillment(of: [writing], timeout: 2)
    XCTAssertTrue(store.values.isEmpty)
    XCTAssertEqual(login.calls, 0)
    XCTAssertEqual(sessions.calls, 1)
    store.releaseWrite()
    let result = try await task.value
    XCTAssertEqual(result.pageURL, context.pageURL)
    XCTAssertEqual(store.values.first?.value, "synthetic-session")
    XCTAssertFalse(store.events.contains("clear"))
  }

  func testInteractiveLoginClearsOnlySelectedWebStore() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    let bootstrap = makeBootstrap(
      MemoryDatabricksCredentialStore(), login: login, sessions: sessions)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    let other = FakeWebStore(identifier: UUID())
    other.values = [try testSessionCookie(url: context.pageURL, value: "other-context")]
    _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
    XCTAssertEqual(login.calls, 1)
    XCTAssertEqual(login.requestedURL, context.pageURL)
    XCTAssertEqual(store.events.first, "clear")
    XCTAssertEqual(other.values.first?.value, "other-context")
    XCTAssertTrue(other.events.isEmpty)
  }

  func testFailedCookieBootstrapCannotLeaveOldAccountDataForTheNextAttempt() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    sessions.failure = .rejected(500)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    store.values = [try testSessionCookie(url: context.pageURL, value: "old-account")]
    login.onSignIn = {
      XCTAssertTrue(store.values.isEmpty)
    }
    let bootstrap = makeBootstrap(credentials, login: login, sessions: sessions)
    do {
      _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
      XCTFail("Expected session failure")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .rejected(500)) }
    XCTAssertNotNil(credentials.snapshot(for: context.scope))
    XCTAssertTrue(store.values.isEmpty)
    sessions.failure = nil
    _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
    XCTAssertEqual(login.calls, 1)
    XCTAssertEqual(store.values.map(\.value), ["synthetic-session"])
  }

  func testStorageAndSessionErrorsDoNotTriggerBrowserFallbackOrInstallCookies() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    let store = FakeWebStore(identifier: context.storeIdentifier)
    let bootstrap = makeBootstrap(credentials, login: login, sessions: sessions)
    credentials.fail(.load)
    do {
      _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
      XCTFail("Expected storage failure")
    } catch { XCTAssertTrue(error is DatabricksCredentialError) }
    XCTAssertEqual(login.calls, 0)
    XCTAssertEqual(sessions.calls, 0)
    XCTAssertTrue(store.events.isEmpty)
    credentials.fail(nil)
    try credentials.save(credentialTokens(), for: context.scope)
    sessions.failure = .rejected(403)
    do {
      _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
      XCTFail("Expected session failure")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .rejected(403)) }
    XCTAssertEqual(login.calls, 0)
    XCTAssertTrue(store.events.isEmpty)
    XCTAssertNotNil(credentials.snapshot(for: context.scope))
  }

  func testCancellationDuringSessionRequestDiscardsLateResult() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let sessions = FakeWorkspaceSessions()
    sessions.hold = true
    let started = expectation(description: "session request started")
    sessions.onRequest = { started.fulfill() }
    let bootstrap = makeBootstrap(credentials, login: FakeWorkspaceLogin(), sessions: sessions)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    let anchor = try window()
    let task = Task { try await bootstrap.prepare(context: context, store: store, anchor: anchor) }
    await fulfillment(of: [started], timeout: 2)
    task.cancel()
    bootstrap.cancel()
    sessions.release()
    do {
      _ = try await task.value
      XCTFail("Expected cancellation")
    } catch { XCTAssertTrue(error is CancellationError) }
    XCTAssertTrue(store.events.isEmpty)
  }

  func testWrongStoreCannotReceiveWorkspaceCookies() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    let bootstrap = makeBootstrap(
      MemoryDatabricksCredentialStore(), login: login, sessions: sessions)
    do {
      _ = try await bootstrap.prepare(
        context: context, store: FakeWebStore(identifier: UUID()), anchor: window())
      XCTFail("Expected context mismatch")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .workspaceChanged) }
    XCTAssertEqual(login.calls, 0)
    XCTAssertEqual(sessions.calls, 0)
  }

  func testCoordinatorLoadsOnlyAfterNativeBootstrapIntoItsNamedStore() async throws {
    let context = try webContext(
      "https://test-\(UUID().uuidString.lowercased()).cloud.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let bootstrap = makeBootstrap(
      credentials, login: FakeWorkspaceLogin(), sessions: FakeWorkspaceSessions())
    let model = WebViewModel()
    let suite = "omnigent-test-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }
    let parent = OmnigentWebView(
      initialURL: context.pageURL, model: model, settings: SettingsStore(defaults: defaults),
      databricksInternalFeaturesEnabled: false,
      loadFailed: { _, _ in XCTFail("Unexpected load failure") }, loadSucceeded: {},
      pushServerPicker: {}, requestSwitchServer: { _ in }, openServerSetup: {})
    let coordinator = OmnigentWebView.Coordinator(parent, context: context, bootstrap: bootstrap)
    let configuration = WKWebViewConfiguration()
    configuration.websiteDataStore = coordinator.websiteDataStore
    let webView = RecordingWorkspaceWebView(frame: .zero, configuration: configuration)
    let anchor = try window()
    anchor.addSubview(webView)
    model.webView = webView
    coordinator.attach(webView)
    let loaded = expectation(description: "first page load")
    webView.onLoad = { loaded.fulfill() }
    coordinator.load(context.pageURL, in: webView)
    XCTAssertTrue(webView.requests.isEmpty)
    await fulfillment(of: [loaded], timeout: 10)
    XCTAssertEqual(webView.requests.first?.url, context.pageURL)
    XCTAssertNil(webView.requests.first?.value(forHTTPHeaderField: "Authorization"))
    XCTAssertEqual(webView.configuration.websiteDataStore.identifier, context.storeIdentifier)
    let cookies = await webView.configuration.websiteDataStore.httpCookieStore.allCookies()
    XCTAssertTrue(cookies.contains { $0.name == "DBAUTH" })
    XCTAssertFalse(model.isAuthenticating)
    coordinator.detach()
    model.isLoading = true
    coordinator.webView(webView, didFinish: nil)
    XCTAssertTrue(
      model.isLoading, "A detached coordinator must not update the replacement view's model")
    webView.removeFromSuperview()
    await coordinator.websiteDataStore.removeData(
      ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
  }

  func testClearedGrantCannotInstallACompletedCookieExchange() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let sessions = FakeWorkspaceSessions()
    sessions.onResponse = { try? credentials.delete(for: context.scope) }
    let bootstrap = makeBootstrap(credentials, login: FakeWorkspaceLogin(), sessions: sessions)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    do {
      _ = try await bootstrap.prepare(context: context, store: store, anchor: window())
      XCTFail("Expected stale credential rejection")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .credentialsChanged) }
    XCTAssertTrue(store.events.isEmpty)
  }

  func testCancellationReturnsToSetupWithoutAnErrorMessage() {
    XCTAssertNil(OmnigentWebView.Coordinator.workspaceErrorMessage(CancellationError()))
    XCTAssertNil(
      OmnigentWebView.Coordinator.workspaceErrorMessage(DatabricksSessionError.cancelled))
  }

  func testConfigurationAndSessionFailuresStillHaveMessages() {
    for error: Error in [
      DatabricksOAuthError.invalidClientID, DatabricksSessionError.missingCookie,
      DatabricksSessionError.networkUnavailable, DatabricksSessionError.rejected(403),
    ] {
      XCTAssertEqual(
        OmnigentWebView.Coordinator.workspaceErrorMessage(error), error.localizedDescription)
    }
  }

  func testUnreachableManagedHostKeepsTheSharedPageLoadGuidance() {
    let unreachable = NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotFindHost)
    let guidance = OmnigentWebView.connectionErrorMessage(
      for: unreachable, databricksInternalFeaturesEnabled: true)
    XCTAssertEqual(
      OmnigentWebView.Coordinator.workspaceErrorMessage(
        unreachable, databricksInternalFeaturesEnabled: true), guidance)
    XCTAssertEqual(
      OmnigentWebView.Coordinator.workspaceErrorMessage(
        unreachable, databricksInternalFeaturesEnabled: false), unreachable.localizedDescription)
    // Cancellation stays silent regardless of the managed flag.
    XCTAssertNil(
      OmnigentWebView.Coordinator.workspaceErrorMessage(
        DatabricksSessionError.cancelled, databricksInternalFeaturesEnabled: true))
  }

  func testSilentRecoveryDoesNotPresentBrowserWhenGrantIsMissing() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let login = FakeWorkspaceLogin()
    let sessions = FakeWorkspaceSessions()
    let bootstrap = makeBootstrap(
      MemoryDatabricksCredentialStore(), login: login, sessions: sessions)
    let store = FakeWebStore(identifier: context.storeIdentifier)
    do {
      _ = try await bootstrap.prepare(
        context: context, store: store, anchor: window(), intent: .recover)
      XCTFail("Expected explicit reauthentication requirement")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .reauthenticationRequired) }
    XCTAssertEqual(login.calls, 0)
    XCTAssertEqual(sessions.calls, 0)
    XCTAssertTrue(store.events.isEmpty)
  }

  func testRecoveryPreservesCurrentPageWithoutChangingCredentialContext() async throws {
    let context = try webContext("https://workspace.cloud.databricks.com/omnigent")
    let page = URL(string: "https://workspace.databricks.com/omnigent/c/abc?o=123#message")!
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let login = FakeWorkspaceLogin()
    let bootstrap = makeBootstrap(credentials, login: login, sessions: FakeWorkspaceSessions())
    let result = try await bootstrap.prepare(
      context: context, store: FakeWebStore(identifier: context.storeIdentifier), anchor: window(),
      intent: .recover, pageURL: page)
    XCTAssertEqual(result.pageURL, page)
    XCTAssertEqual(login.calls, 0)
    XCTAssertNotNil(credentials.snapshot(for: context.scope))
    let alias = try DatabricksCredentialScope(
      workspaceURL: page, configuration: context.configuration)
    XCTAssertNil(credentials.snapshot(for: alias))
  }

  func testSession401RefreshesOnceThenRetriesWithoutBrowser() async throws {
    let refreshed = expectation(description: "one refresh")
    refreshed.assertForOverFulfill = true
    let server = OAuthTestServer { request in
      XCTAssertEqual(request.httpMethod, "POST")
      refreshed.fulfill()
      return .init(data: OAuthTestServer.tokenData)
    }
    let context = try webContext(server.workspaceURL.absoluteString + "/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let manager = DatabricksTokenManager(
      store: credentials, client: DatabricksOAuthClient(session: server.session),
      now: { Date(timeIntervalSince1970: 1000) })
    let sessions = FakeWorkspaceSessions()
    sessions.failures = [.rejected(401)]
    let login = FakeWorkspaceLogin()
    let bootstrap = makeBootstrap(credentials, login: login, sessions: sessions, tokens: manager)
    _ = try await bootstrap.prepare(
      context: context, store: FakeWebStore(identifier: context.storeIdentifier), anchor: window(),
      intent: .recover)
    await fulfillment(of: [refreshed], timeout: 2)
    XCTAssertEqual(sessions.calls, 2)
    XCTAssertEqual(login.calls, 0)
    XCTAssertEqual(credentials.snapshot(for: context.scope)?.accessToken, "opaque-access")
  }

  func testRepeated401DoesNotLoopOrEraseAValidRefreshGrant() async throws {
    let refreshed = expectation(description: "one refresh")
    refreshed.assertForOverFulfill = true
    let server = OAuthTestServer { _ in
      refreshed.fulfill()
      return .init(data: OAuthTestServer.tokenData)
    }
    let context = try webContext(server.workspaceURL.absoluteString + "/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    try credentials.save(credentialTokens(), for: context.scope)
    let manager = DatabricksTokenManager(
      store: credentials, client: DatabricksOAuthClient(session: server.session),
      now: { Date(timeIntervalSince1970: 1000) })
    let sessions = FakeWorkspaceSessions()
    sessions.failure = .rejected(401)
    let login = FakeWorkspaceLogin()
    let bootstrap = makeBootstrap(credentials, login: login, sessions: sessions, tokens: manager)
    do {
      _ = try await bootstrap.prepare(
        context: context, store: FakeWebStore(identifier: context.storeIdentifier),
        anchor: window(), intent: .recover)
      XCTFail("Expected bounded failure")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .rejected(401)) }
    await fulfillment(of: [refreshed], timeout: 2)
    XCTAssertEqual(sessions.calls, 2)
    XCTAssertEqual(login.calls, 0)
    XCTAssertNotNil(credentials.snapshot(for: context.scope))
  }

  #if DEBUG
    func testDebugCookieFaultClearsOnlyTheSessionCookie() async throws {
      let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
      let credentials = MemoryDatabricksCredentialStore()
      let saved = credentialTokens()
      try credentials.save(saved, for: context.scope)
      let bootstrap = makeBootstrap(
        credentials, login: FakeWorkspaceLogin(), sessions: FakeWorkspaceSessions())
      let store = FakeWebStore(identifier: context.storeIdentifier)
      let unrelated = try XCTUnwrap(
        HTTPCookie(properties: [
          .name: "omnigent-theme", .value: "dark", .domain: try XCTUnwrap(context.pageURL.host),
          .path: "/",
        ]))
      store.values = [try testSessionCookie(url: context.pageURL), unrelated]
      let injected = try await bootstrap.inject(.sessionCookie, context: context, store: store)
      XCTAssertTrue(injected)
      XCTAssertEqual(store.values.map(\.name), ["omnigent-theme"])
      XCTAssertFalse(store.events.contains("clear"))
      XCTAssertEqual(credentials.snapshot(for: context.scope), saved)
    }

    func testDebugTokenFaultsKeepTheRefreshGrantUntilItIsTheChosenFault() async throws {
      let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
      let credentials = MemoryDatabricksCredentialStore()
      let now = Date(timeIntervalSince1970: 1000)
      try credentials.save(credentialTokens(), for: context.scope)
      let bootstrap = makeBootstrap(
        credentials, login: FakeWorkspaceLogin(), sessions: FakeWorkspaceSessions(),
        tokens: DatabricksTokenManager(store: credentials, now: { now }))
      let store = FakeWebStore(identifier: context.storeIdentifier)

      var injected = try await bootstrap.inject(.accessToken, context: context, store: store)
      XCTAssertTrue(injected)
      var stored = try XCTUnwrap(credentials.snapshot(for: context.scope))
      XCTAssertNotEqual(stored.accessToken, "test-access")
      XCTAssertEqual(stored.refreshToken, "test-refresh")
      XCTAssertLessThan(stored.expiresAt, now)

      try credentials.save(credentialTokens(), for: context.scope)
      injected = try await bootstrap.inject(.rejectedAccessToken, context: context, store: store)
      XCTAssertTrue(injected)
      stored = try XCTUnwrap(credentials.snapshot(for: context.scope))
      XCTAssertNotEqual(stored.accessToken, "test-access")
      XCTAssertEqual(stored.refreshToken, "test-refresh")
      // Unexpired, so the workspace rejects the token instead of the app refreshing it first.
      XCTAssertGreaterThan(stored.expiresAt, now.addingTimeInterval(60))

      injected = try await bootstrap.inject(.refreshToken, context: context, store: store)
      XCTAssertTrue(injected)
      stored = try XCTUnwrap(credentials.snapshot(for: context.scope))
      XCTAssertNotEqual(stored.refreshToken, "test-refresh")
      XCTAssertLessThan(stored.expiresAt, now)
      XCTAssertTrue(store.events.isEmpty)
    }

    func testDebugFaultNeedsSavedCredentialsAndTheSelectedStore() async throws {
      let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
      let credentials = MemoryDatabricksCredentialStore()
      let bootstrap = makeBootstrap(
        credentials, login: FakeWorkspaceLogin(), sessions: FakeWorkspaceSessions())
      let store = FakeWebStore(identifier: context.storeIdentifier)
      let injected = try await bootstrap.inject(.accessToken, context: context, store: store)
      XCTAssertFalse(injected)
      do {
        _ = try await bootstrap.inject(
          .sessionCookie, context: context, store: FakeWebStore(identifier: UUID()))
        XCTFail("Expected a store from another workspace to be rejected")
      } catch { XCTAssertEqual(error as? DatabricksSessionError, .workspaceChanged) }
    }

    func testInjectedAccessTokenFaultRecoversWithoutBrowserSignIn() async throws {
      let refreshed = expectation(description: "one refresh")
      refreshed.assertForOverFulfill = true
      let server = OAuthTestServer { _ in
        refreshed.fulfill()
        return .init(data: OAuthTestServer.tokenData)
      }
      let context = try webContext(server.workspaceURL.absoluteString + "/omnigent?o=123")
      let credentials = MemoryDatabricksCredentialStore()
      try credentials.save(credentialTokens(), for: context.scope)
      let login = FakeWorkspaceLogin()
      let sessions = FakeWorkspaceSessions()
      let bootstrap = makeBootstrap(
        credentials, login: login, sessions: sessions,
        tokens: DatabricksTokenManager(
          store: credentials, client: DatabricksOAuthClient(session: server.session),
          now: { Date(timeIntervalSince1970: 1000) }))
      let store = FakeWebStore(identifier: context.storeIdentifier)
      let injected = try await bootstrap.inject(.accessToken, context: context, store: store)
      XCTAssertTrue(injected)
      _ = try await bootstrap.prepare(
        context: context, store: store, anchor: window(), intent: .recover)
      await fulfillment(of: [refreshed], timeout: 2)
      XCTAssertEqual(login.calls, 0)
      XCTAssertEqual(credentials.snapshot(for: context.scope)?.accessToken, "opaque-access")
    }

    func testInjectedRefreshFaultAsksForSignInInsteadOfOpeningTheBrowser() async throws {
      let server = OAuthTestServer { _ in
        .init(status: 400, data: Data(#"{"error":"invalid_grant"}"#.utf8))
      }
      let context = try webContext(server.workspaceURL.absoluteString + "/omnigent?o=123")
      let credentials = MemoryDatabricksCredentialStore()
      try credentials.save(credentialTokens(), for: context.scope)
      let login = FakeWorkspaceLogin()
      let bootstrap = makeBootstrap(
        credentials, login: login, sessions: FakeWorkspaceSessions(),
        tokens: DatabricksTokenManager(
          store: credentials, client: DatabricksOAuthClient(session: server.session),
          now: { Date(timeIntervalSince1970: 1000) }))
      let store = FakeWebStore(identifier: context.storeIdentifier)
      let injected = try await bootstrap.inject(.refreshToken, context: context, store: store)
      XCTAssertTrue(injected)
      do {
        _ = try await bootstrap.prepare(
          context: context, store: store, anchor: window(), intent: .recover)
        XCTFail("Expected explicit reauthentication")
      } catch {
        XCTAssertEqual(error as? DatabricksSessionError, .reauthenticationRequired)
      }
      XCTAssertEqual(login.calls, 0)
      XCTAssertNil(credentials.snapshot(for: context.scope))
    }
  #endif

  private func window() throws -> UIWindow {
    let scene = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
    return UIWindow(windowScene: scene)
  }

  private func makeBootstrap(
    _ credentials: MemoryDatabricksCredentialStore, login: FakeWorkspaceLogin,
    sessions: FakeWorkspaceSessions, tokens: DatabricksTokenManager? = nil
  ) -> DatabricksWorkspaceBootstrap {
    login.persist = { url, configuration, tokens in
      try credentials.save(
        tokens, for: DatabricksCredentialScope(workspaceURL: url, configuration: configuration))
    }
    let tokens =
      tokens
      ?? DatabricksTokenManager(store: credentials, now: { Date(timeIntervalSince1970: 1000) })
    let suite = "omnigent-signout-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
    let installer = DatabricksCookieInstaller()
    return DatabricksWorkspaceBootstrap(
      tokens: tokens, login: login, sessions: sessions, installer: installer,
      signOuts: DatabricksSignOutManager(tokens: tokens, installer: installer, defaults: defaults))
  }
}

@MainActor
final class DatabricksLifecycleTests: XCTestCase {
  func testRecoveryRequiresHealthyPageAndCooldownBeforeAnotherAttempt() {
    var policy = DatabricksRecoveryPolicy()
    let now = Date(timeIntervalSince1970: 1000)
    XCTAssertTrue(policy.begin(now: now))
    XCTAssertFalse(policy.begin(now: now.addingTimeInterval(120)))
    policy.markReady()
    XCTAssertFalse(policy.begin(now: now.addingTimeInterval(59)))
    XCTAssertTrue(policy.begin(now: now.addingTimeInterval(60)))
    XCTAssertFalse(policy.begin(now: now.addingTimeInterval(180)))
  }

  func testRecoveryPageKeepsStoreIdentityAndRejectsAnotherWorkspace() throws {
    let context = try webContext("https://workspace.cloud.databricks.com/omnigent?o=123")
    let target = URL(string: "https://workspace.databricks.com/omnigent/c/abc?o=123#message")!
    let navigation = try context.navigating(to: target)
    XCTAssertEqual(navigation.scope, context.scope)
    XCTAssertEqual(navigation.storeIdentifier, context.storeIdentifier)
    XCTAssertEqual(navigation.pageURL, target)
    XCTAssertThrowsError(
      try context.navigating(to: URL(string: "https://workspace.databricks.com/omnigent?o=456")!))
  }

  func testSignOutClearsOnlyItsContextAndSuppressesAutoOpen() async throws {
    let f = try fixture()
    let otherContext = try webContext("https://workspace.databricks.com/omnigent?o=456")
    try f.credentials.save(credentialTokens(), for: f.context.scope)
    try f.credentials.save(credentialTokens(access: "other"), for: otherContext.scope)
    f.web.values = [try testSessionCookie(url: f.context.pageURL)]
    let settings = SettingsStore(defaults: f.defaults)
    settings.serverURL = f.context.pageURL.absoluteString
    let job = try f.signOuts.begin(context: f.context, store: f.web)
    XCTAssertTrue(f.signOuts.isPending(f.context.storeIdentifier))
    settings.stopAutoOpening(f.context)
    XCTAssertNil(settings.serverURL)
    try await job.value
    XCTAssertFalse(f.signOuts.isPending(f.context.storeIdentifier))
    XCTAssertNil(f.credentials.snapshot(for: f.context.scope))
    XCTAssertTrue(f.web.values.isEmpty)
    XCTAssertEqual(f.credentials.snapshot(for: otherContext.scope)?.accessToken, "other")
    settings.serverURL = otherContext.pageURL.absoluteString
    settings.stopAutoOpening(f.context)
    XCTAssertEqual(settings.serverURL, otherContext.pageURL.absoluteString)
  }

  func testFailedSignOutIsRetriedAfterManagerRecreationBeforeNewLogin() async throws {
    let f = try fixture()
    try f.credentials.save(credentialTokens(), for: f.context.scope)
    f.credentials.fail(.delete)
    do {
      try await f.signOuts.begin(context: f.context, store: f.web).value
      XCTFail("Expected cleanup failure")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .signOutIncomplete) }
    XCTAssertTrue(f.signOuts.isPending(f.context.storeIdentifier))
    f.credentials.fail(nil)
    let tokens = DatabricksTokenManager(store: f.credentials)
    let restored = DatabricksSignOutManager(
      tokens: tokens, installer: f.installer, defaults: f.defaults)
    let login = FakeWorkspaceLogin()
    let bootstrap = DatabricksWorkspaceBootstrap(
      tokens: tokens, login: login, sessions: FakeWorkspaceSessions(), installer: f.installer,
      signOuts: restored)
    do {
      _ = try await bootstrap.prepare(
        context: f.context, store: f.web, anchor: window(), intent: .recover)
      XCTFail("Expected explicit sign-in after cleanup")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .reauthenticationRequired) }
    XCTAssertEqual(login.calls, 0)
    XCTAssertNil(f.credentials.snapshot(for: f.context.scope))
    XCTAssertFalse(restored.isPending(f.context.storeIdentifier))
  }

  func testSignOutWaitsBehindAnAlreadyStartedCookieWrite() async throws {
    let f = try fixture()
    try f.credentials.save(credentialTokens(), for: f.context.scope)
    let started = expectation(description: "cookie write started")
    f.web.holdNextWrite = true
    f.web.onWrite = { started.fulfill() }
    let cookie = try testSessionCookie(url: f.context.pageURL)
    let write = Task { try await f.installer.install([cookie], in: f.web, reset: false) }
    await fulfillment(of: [started], timeout: 2)
    let cleanup = try f.signOuts.begin(context: f.context, store: f.web)
    f.web.releaseWrite()
    try await write.value
    try await cleanup.value
    XCTAssertTrue(f.web.values.isEmpty)
    XCTAssertEqual(f.web.events.last, "clear")
    XCTAssertNil(f.credentials.snapshot(for: f.context.scope))
  }

  func testPendingSignOutRejectsLateCookieBootstrap() async throws {
    let f = try fixture()
    try f.credentials.save(credentialTokens(), for: f.context.scope)
    let sessions = FakeWorkspaceSessions()
    sessions.hold = true
    let requested = expectation(description: "session request started")
    sessions.onRequest = { requested.fulfill() }
    let bootstrap = DatabricksWorkspaceBootstrap(
      tokens: f.tokens, login: FakeWorkspaceLogin(), sessions: sessions, installer: f.installer,
      signOuts: f.signOuts)
    let anchor = try window()
    let pending = Task {
      try await bootstrap.prepare(context: f.context, store: f.web, anchor: anchor)
    }
    await fulfillment(of: [requested], timeout: 2)
    let cleanup = try f.signOuts.begin(context: f.context, store: f.web)
    sessions.release()
    do {
      _ = try await pending.value
      XCTFail("Expected stale result rejection")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .credentialsChanged) }
    try await cleanup.value
    XCTAssertTrue(f.web.values.isEmpty)
    XCTAssertFalse(f.web.events.contains { $0.hasPrefix("write-start:") })
  }

  func testCompletedSignOutRejectsLateBootstrapEvenIfCredentialsReappear() async throws {
    let f = try fixture()
    let original = credentialTokens()
    try f.credentials.save(original, for: f.context.scope)
    let sessions = FakeWorkspaceSessions()
    sessions.hold = true
    let requested = expectation(description: "session request started")
    sessions.onRequest = { requested.fulfill() }
    let bootstrap = DatabricksWorkspaceBootstrap(
      tokens: f.tokens, login: FakeWorkspaceLogin(), sessions: sessions, installer: f.installer,
      signOuts: f.signOuts)
    let anchor = try window()
    let pending = Task {
      try await bootstrap.prepare(context: f.context, store: f.web, anchor: anchor)
    }
    await fulfillment(of: [requested], timeout: 2)
    try await f.signOuts.begin(context: f.context, store: f.web).value
    try f.credentials.save(original, for: f.context.scope)
    sessions.release()
    do {
      _ = try await pending.value
      XCTFail("Expected old-generation rejection")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .credentialsChanged) }
    XCTAssertTrue(f.web.values.isEmpty)
    XCTAssertEqual(f.credentials.snapshot(for: f.context.scope), original)
  }

  func testLogoutURLsAreDistinctFromExpiredLoginAndOtherContexts() throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let session = testWebSession(context: context, cookies: [])
    for path in ["/auth/logout", "/logout", "/login.html?logout=1"] {
      XCTAssertTrue(
        session.isSignOutURL(URL(string: context.scope.workspaceOrigin.absoluteString + path)!))
    }
    XCTAssertFalse(
      session.isSignOutURL(URL(string: "https://workspace.databricks.com/auth/logout?o=456")!))
    XCTAssertFalse(session.isSignOutURL(URL(string: "https://other.databricks.com/auth/logout")!))
    XCTAssertTrue(
      session.isAuthenticationURL(URL(string: "https://workspace.databricks.com/login.html")!))
    XCTAssertFalse(
      session.isAuthenticationURL(URL(string: "https://workspace.databricks.com/omnigent")!))
  }

  func testCoordinatorRecovers401ButDoesNotRetry403() async throws {
    for status in [401, 403] {
      let restored = expectation(description: "recovery or failure")
      var recoveryCount = 0
      let targetPath = "/omnigent/c/abc?o=123#message"
      let h = try await coordinatorFixture(
        recover: { url in
          XCTAssertEqual(url.path, "/omnigent/c/abc")
          XCTAssertEqual(url.query, "o=123")
          XCTAssertEqual(url.fragment, "message")
          recoveryCount += 1
          restored.fulfill()
        },
        failure: { message in
          XCTAssertEqual(message, DatabricksSessionError.rejected(403).localizedDescription)
          restored.fulfill()
        })
      h.view.requests.append(
        URLRequest(url: URL(string: h.context.scope.workspaceOrigin.absoluteString + targetPath)!))
      h.coordinator.webView(h.view, didCommit: nil)
      XCTAssertTrue(h.coordinator.handleWorkspaceHTTPStatus(status))
      await fulfillment(of: [restored], timeout: 2)
      XCTAssertEqual(recoveryCount, status == 401 ? 1 : 0)
      h.coordinator.detach()
      h.view.removeFromSuperview()
      await h.coordinator.websiteDataStore.removeData(
        ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
    }
  }

  func testForegroundMissingCookieRequestsOneRecovery() async throws {
    let recovered = expectation(description: "foreground recovery")
    recovered.assertForOverFulfill = true
    let h = try await coordinatorFixture(
      recover: { _ in recovered.fulfill() }, failure: { _ in XCTFail("Unexpected failure") })
    let cookieStore = h.coordinator.websiteDataStore.httpCookieStore
    for cookie in await cookieStore.allCookies() { await cookieStore.deleteCookie(cookie) }
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    await fulfillment(of: [recovered], timeout: 2)
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    h.coordinator.detach()
    h.view.removeFromSuperview()
  }

  func testNativeSignOutActionDetachesAndCompletesScopedCleanup() async throws {
    let signedOut = expectation(description: "sign-out callback")
    var cleanup: Task<Void, Error>?
    let h = try await coordinatorFixture(
      recover: { _ in XCTFail("Unexpected recovery") },
      failure: { _ in XCTFail("Unexpected failure") },
      signedOut: { _, job in
        cleanup = job
        signedOut.fulfill()
      })
    XCTAssertNotNil(h.model.signOut)
    h.model.signOut?()
    await fulfillment(of: [signedOut], timeout: 2)
    try await XCTUnwrap(cleanup).value
    XCTAssertNil(h.credentials.snapshot(for: h.context.scope))
    let cookies = await h.coordinator.websiteDataStore.httpCookieStore.allCookies()
    XCTAssertTrue(cookies.isEmpty)
    XCTAssertNil(h.model.signOut)
    h.view.removeFromSuperview()
  }

  private struct CoordinatorFixture {
    let context: DatabricksWebContext
    let credentials: MemoryDatabricksCredentialStore
    let model: WebViewModel
    let coordinator: OmnigentWebView.Coordinator
    let view: RecordingWorkspaceWebView
    let window: UIWindow
  }

  private func coordinatorFixture(
    recover: @escaping (URL) -> Void, failure: @escaping (String?) -> Void,
    signedOut: ((DatabricksWebContext, Task<Void, Error>) -> Void)? = nil
  ) async throws -> CoordinatorFixture {
    let context = try webContext(
      "https://test-\(UUID().uuidString.lowercased()).cloud.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    let tokens = DatabricksTokenManager(
      store: credentials, now: { Date(timeIntervalSince1970: 1000) })
    try credentials.save(credentialTokens(), for: context.scope)
    let suite = "omnigent-coordinator-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
    let installer = DatabricksCookieInstaller()
    let bootstrap = DatabricksWorkspaceBootstrap(
      tokens: tokens, login: FakeWorkspaceLogin(), sessions: FakeWorkspaceSessions(),
      installer: installer,
      signOuts: DatabricksSignOutManager(tokens: tokens, installer: installer, defaults: defaults))
    let model = WebViewModel()
    let parent = OmnigentWebView(
      initialURL: context.pageURL, model: model, settings: SettingsStore(defaults: defaults),
      databricksInternalFeaturesEnabled: false,
      loadFailed: { _, message in failure(message) }, loadSucceeded: {}, pushServerPicker: {},
      requestSwitchServer: { _ in }, openServerSetup: {},
      recoverWorkspace: recover, signedOut: signedOut)
    let coordinator = OmnigentWebView.Coordinator(parent, context: context, bootstrap: bootstrap)
    let configuration = WKWebViewConfiguration()
    configuration.websiteDataStore = coordinator.websiteDataStore
    let view = RecordingWorkspaceWebView(frame: .zero, configuration: configuration)
    let window = try window()
    window.addSubview(view)
    model.webView = view
    coordinator.attach(view)
    let loaded = expectation(description: "initial bootstrap")
    view.onLoad = { loaded.fulfill() }
    coordinator.load(context.pageURL, in: view)
    await fulfillment(of: [loaded], timeout: 10)
    return CoordinatorFixture(
      context: context, credentials: credentials, model: model, coordinator: coordinator,
      view: view, window: window)
  }

  private struct Fixture {
    let context: DatabricksWebContext
    let credentials: MemoryDatabricksCredentialStore
    let tokens: DatabricksTokenManager
    let installer: DatabricksCookieInstaller
    let defaults: UserDefaults
    let web: FakeWebStore
    let signOuts: DatabricksSignOutManager
  }

  private func fixture() throws -> Fixture {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let credentials = MemoryDatabricksCredentialStore()
    let tokens = DatabricksTokenManager(
      store: credentials, now: { Date(timeIntervalSince1970: 1000) })
    let installer = DatabricksCookieInstaller()
    let suite = "omnigent-lifecycle-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
    return Fixture(
      context: context, credentials: credentials, tokens: tokens, installer: installer,
      defaults: defaults,
      web: FakeWebStore(identifier: context.storeIdentifier),
      signOuts: DatabricksSignOutManager(tokens: tokens, installer: installer, defaults: defaults))
  }

  private func window() throws -> UIWindow {
    UIWindow(
      windowScene: try XCTUnwrap(
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first))
  }
}

@MainActor
private final class FakeWorkspaceLogin: DatabricksSigningIn {
  var calls = 0
  var requestedURL: URL?
  var onSignIn: (() -> Void)?
  var persist: ((URL, DatabricksOAuthConfiguration, DatabricksOAuthTokens) throws -> Void)?
  func signIn(
    workspaceURL: URL, configuration: DatabricksOAuthConfiguration, anchor: ASPresentationAnchor
  ) async throws -> DatabricksOAuthTokens {
    calls += 1
    requestedURL = workspaceURL
    onSignIn?()
    let tokens = credentialTokens()
    try persist?(workspaceURL, configuration, tokens)
    return tokens
  }
  func cancel() {}
}

@MainActor
private final class FakeWorkspaceSessions: DatabricksSessionCreating {
  var calls = 0
  var failure: DatabricksSessionError?
  var failures: [DatabricksSessionError] = []
  var hold = false
  var onRequest: (() -> Void)?
  var onResponse: (() -> Void)?
  private var pending: CheckedContinuation<Void, Never>?

  func create(context: DatabricksWebContext, tokens: DatabricksOAuthTokens) async throws
    -> DatabricksWebSession
  {
    calls += 1
    if !failures.isEmpty { throw failures.removeFirst() }
    if let failure { throw failure }
    if hold {
      await withCheckedContinuation { continuation in
        pending = continuation
        onRequest?()
      }
    }
    onResponse?()
    return testWebSession(context: context, cookies: [try testSessionCookie(url: context.pageURL)])
  }
  func release() {
    pending?.resume()
    pending = nil
  }
}

@MainActor
private final class RecordingWorkspaceWebView: WKWebView {
  override var url: URL? { requests.last?.url }
  var requests: [URLRequest] = []
  var onLoad: (() -> Void)?
  override func load(_ request: URLRequest) -> WKNavigation? {
    requests.append(request)
    onLoad?()
    return nil
  }
}
