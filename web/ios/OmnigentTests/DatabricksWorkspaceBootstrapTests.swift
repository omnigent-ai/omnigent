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

  private func window() throws -> UIWindow {
    let scene = try XCTUnwrap(
      UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
    return UIWindow(windowScene: scene)
  }

  private func makeBootstrap(
    _ credentials: MemoryDatabricksCredentialStore, login: FakeWorkspaceLogin,
    sessions: FakeWorkspaceSessions
  ) -> DatabricksWorkspaceBootstrap {
    login.persist = { url, configuration, tokens in
      try credentials.save(
        tokens, for: DatabricksCredentialScope(workspaceURL: url, configuration: configuration))
    }
    return DatabricksWorkspaceBootstrap(
      tokens: DatabricksTokenManager(
        store: credentials, now: { Date(timeIntervalSince1970: 1000) }),
      login: login, sessions: sessions, installer: DatabricksCookieInstaller())
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
  var hold = false
  var onRequest: (() -> Void)?
  var onResponse: (() -> Void)?
  private var pending: CheckedContinuation<Void, Never>?

  func create(context: DatabricksWebContext, tokens: DatabricksOAuthTokens) async throws
    -> DatabricksWebSession
  {
    calls += 1
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
  var requests: [URLRequest] = []
  var onLoad: (() -> Void)?
  override func load(_ request: URLRequest) -> WKNavigation? {
    requests.append(request)
    onLoad?()
    return nil
  }
}
