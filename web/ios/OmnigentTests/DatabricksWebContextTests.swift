import WebKit
import XCTest

@testable import Omnigent

@MainActor
final class DatabricksWebContextTests: XCTestCase {
  func testStableStoreIdentitySeparatesWorkspacesAndClients() throws {
    let first = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let same = try webContext(
      "https://WORKSPACE.DATABRICKS.COM:443/omnigent/c/abc?o=123&view=chat#message")
    XCTAssertEqual(first.storeIdentifier, same.storeIdentifier)
    XCTAssertNotEqual(
      first.storeIdentifier,
      try webContext("https://workspace.databricks.com/omnigent?o=456").storeIdentifier)
    XCTAssertNotEqual(
      first.storeIdentifier,
      try webContext("https://workspace.databricks.com/omnigent?o=123", clientID: "another-client")
        .storeIdentifier)
    XCTAssertEqual(
      try webContext("https://workspace.databricks.com?o=123").pageURL.absoluteString,
      "https://workspace.databricks.com/omnigent?o=123")
  }

  func testAppsAndGenericServersKeepTheirExistingStrategy() throws {
    XCTAssertNil(try DatabricksWebContext.resolve(URL(string: "https://app.databricksapps.com")!))
    XCTAssertNil(try DatabricksWebContext.resolve(URL(string: "https://example.com")!))
    XCTAssertEqual(
      DatabricksWebContext.viewIdentity(for: URL(string: "https://example.com")!), "generic")
    XCTAssertThrowsError(
      try webContext("https://login.databricks.com/mobile-redirect?code=synthetic"))
    XCTAssertThrowsError(try webContext("https://workspace.databricks.com/oidc/v1/authorize"))
  }

  func testNamedWebKitStoresPersistSeparatelyAndClearOnlyTheirOwnData() async throws {
    let host = "test-\(UUID().uuidString.lowercased()).cloud.databricks.com"
    let contextA = try webContext("https://\(host)/omnigent?o=123")
    let contextB = try webContext("https://\(host)/omnigent?o=456")
    let first = DatabricksWebStore(identifier: contextA.storeIdentifier)
    let second = DatabricksWebStore(identifier: contextB.storeIdentifier)
    XCTAssertTrue(first.websiteDataStore.isPersistent)
    XCTAssertEqual(first.websiteDataStore.identifier, contextA.storeIdentifier)
    XCTAssertNotEqual(first.websiteDataStore.identifier, second.websiteDataStore.identifier)
    await first.setCookie(try testSessionCookie(url: contextA.pageURL, value: "first"))
    await second.setCookie(try testSessionCookie(url: contextB.pageURL, value: "second"))
    let reopened = DatabricksWebStore(identifier: contextA.storeIdentifier)
    let a = await reopened.cookies()
    let b = await second.cookies()
    XCTAssertEqual(a.first { $0.name == "DBAUTH" }?.value, "first")
    XCTAssertEqual(b.first { $0.name == "DBAUTH" }?.value, "second")
    XCTAssertTrue(a.first { $0.name == "DBAUTH" }?.isHTTPOnly == true)
    XCTAssertEqual(a.first { $0.name == "DBAUTH" }?.properties?[.sameSitePolicy] as? String, "lax")
    await first.clear()
    let cleared = await reopened.cookies()
    let retained = await second.cookies()
    XCTAssertTrue(cleared.isEmpty)
    XCTAssertEqual(retained.first { $0.name == "DBAUTH" }?.value, "second")
    await second.clear()
  }

  func testCookieInstallerWaitsForCanceledWritesBeforeReplacingSession() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let store = FakeWebStore(identifier: context.storeIdentifier)
    let installer = DatabricksCookieInstaller()
    let started = expectation(description: "old write started")
    store.holdNextWrite = true
    store.onWrite = { started.fulfill() }
    let oldCookie = try testSessionCookie(url: context.pageURL, value: "old")
    let newCookie = try testSessionCookie(url: context.pageURL, value: "new")
    let first = Task { try await installer.install([oldCookie], in: store, reset: false) }
    await fulfillment(of: [started], timeout: 2)
    first.cancel()
    store.onWrite = nil
    let second = Task { try await installer.install([newCookie], in: store, reset: true) }
    store.releaseWrite()
    do {
      try await first.value
      XCTFail("Expected cancellation")
    } catch { XCTAssertTrue(error is CancellationError) }
    try await second.value
    XCTAssertEqual(store.values.map(\.value), ["new"])
    XCTAssertEqual(
      store.events,
      ["write-start:old", "write-end:old", "clear", "write-start:new", "write-end:new"])
  }

  func testCookieInstallerValidatesBeforeMutatingTheStore() async throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let store = FakeWebStore(identifier: context.storeIdentifier)
    let old = try testSessionCookie(url: context.pageURL, value: "old")
    store.values = [old]
    do {
      try await DatabricksCookieInstaller().install([], in: store, reset: true) {
        throw DatabricksSessionError.credentialsChanged
      }
      XCTFail("Expected stale installation rejection")
    } catch { XCTAssertEqual(error as? DatabricksSessionError, .credentialsChanged) }
    XCTAssertEqual(store.values.first?.value, "old")
    XCTAssertTrue(store.events.isEmpty)
  }

  func testNavigationBindingRejectsOtherWorkspacesAndLoginDestinations() throws {
    let context = try webContext("https://workspace.databricks.com/omnigent?o=123")
    let result = testWebSession(context: context, cookies: [])
    let normalized = result.navigationURL(
      for: URL(string: "https://workspace.databricks.com/omnigent/c/abc")!)
    XCTAssertEqual(
      normalized?.absoluteString, "https://workspace.databricks.com/omnigent/c/abc?o=123")
    XCTAssertNil(
      result.navigationURL(for: URL(string: "https://workspace.databricks.com/omnigent?o=456")!))
    XCTAssertNil(
      result.navigationURL(for: URL(string: "https://different.databricks.com/omnigent?o=123")!))
    XCTAssertNil(result.navigationURL(for: URL(string: "https://workspace.databricks.com/login")!))
    XCTAssertNil(
      result.navigationURL(for: URL(string: "http://workspace.databricks.com/omnigent?o=123")!))
  }
}

@MainActor
final class FakeWebStore: DatabricksWebStoring {
  let identifier: UUID
  var values: [HTTPCookie] = []
  var events: [String] = []
  var holdNextWrite = false
  var onWrite: (() -> Void)?
  private var pending: CheckedContinuation<Void, Never>?

  init(identifier: UUID) { self.identifier = identifier }
  func cookies() async -> [HTTPCookie] { values }
  func setCookie(_ cookie: HTTPCookie) async {
    events.append("write-start:" + cookie.value)
    if holdNextWrite {
      holdNextWrite = false
      await withCheckedContinuation { continuation in
        pending = continuation
        onWrite?()
      }
    } else {
      onWrite?()
    }
    values.removeAll {
      $0.name == cookie.name && $0.domain == cookie.domain && $0.path == cookie.path
    }
    values.append(cookie)
    events.append("write-end:" + cookie.value)
  }
  func deleteCookie(_ cookie: HTTPCookie) async {
    events.append("delete:" + cookie.name)
    values.removeAll {
      $0.name == cookie.name && $0.domain == cookie.domain && $0.path == cookie.path
    }
  }
  func clear() async {
    events.append("clear")
    values = []
  }
  func releaseWrite() {
    pending?.resume()
    pending = nil
  }
}

func testWebSession(context: DatabricksWebContext, cookies: [HTTPCookie]) -> DatabricksWebSession {
  DatabricksWebSession(
    pageURL: context.pageURL, cookies: cookies,
    allowedOrigins: [context.scope.workspaceOrigin.absoluteString],
    configuration: context.configuration, workspaceID: context.scope.workspaceID)
}
