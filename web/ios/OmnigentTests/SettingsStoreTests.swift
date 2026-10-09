import XCTest

@testable import Omnigent

@MainActor
final class SettingsStoreTests: XCTestCase {
  private var suiteName: String!
  private var defaults: UserDefaults!

  override func setUp() {
    super.setUp()
    suiteName = "SettingsStoreTests.\(UUID().uuidString)"
    defaults = UserDefaults(suiteName: suiteName)
    defaults.removePersistentDomain(forName: suiteName)
  }

  override func tearDown() {
    defaults.removePersistentDomain(forName: suiteName)
    defaults = nil
    suiteName = nil
    super.tearDown()
  }

  func testRecentServersAreDedupedAndCapped() {
    let store = SettingsStore(defaults: defaults)
    for host in ["a", "b", "c", "d", "e", "f", "c"] {
      store.rememberRecentServer(URL(string: "https://\(host).example.com")!)
    }

    XCTAssertEqual(store.recentServers.count, 5)
    XCTAssertEqual(store.recentServers.first, "https://c.example.com")
    XCTAssertFalse(store.recentServers.contains("https://a.example.com"))
  }

  func testProtocolGrantsAreScopedByOrigin() {
    let store = SettingsStore(defaults: defaults)
    store.allowProtocol("vscode", from: "https://one.example.com")

    XCTAssertTrue(store.isProtocolAllowed("vscode", from: "https://one.example.com"))
    XCTAssertFalse(store.isProtocolAllowed("vscode", from: "https://two.example.com"))
  }

  func testOidcSignOutStopsAutoOpeningOnlyThatServer() {
    let store = SettingsStore(defaults: defaults)
    store.serverURL = "https://omnigent.example.com/omnigent"
    store.rememberRecentServer(URL(string: "https://omnigent.example.com/omnigent")!)

    store.stopAutoOpening(oidcServer: URL(string: "https://other.example.com")!)
    XCTAssertEqual(store.serverURL, "https://omnigent.example.com/omnigent")

    store.stopAutoOpening(oidcServer: URL(string: "https://OMNIGENT.example.com/omnigent/")!)
    XCTAssertNil(store.serverURL)
    XCTAssertEqual(store.recentServers, ["https://omnigent.example.com/omnigent"])
  }

  func testOidcSignOutStopsAutoOpeningAnIPv6Server() {
    let store = SettingsStore(defaults: defaults)
    store.serverURL = "http://[::1]:6767"
    store.stopAutoOpening(oidcServer: URL(string: "http://[::1]:6768/")!)
    XCTAssertEqual(store.serverURL, "http://[::1]:6767")
    store.stopAutoOpening(oidcServer: URL(string: "http://[::1]:6767/omnigent/")!)
    XCTAssertNil(store.serverURL)
  }

  func testRefusedServerStaysPrefilledButIsNotReopened() {
    let store = SettingsStore(defaults: defaults)
    store.serverURL = "https://omnigent.example.com/omnigent"
    XCTAssertEqual(store.autoOpenServerURL, "https://omnigent.example.com/omnigent")

    store.suppressAutoOpening(oidcServer: URL(string: "https://other.example.com")!)
    XCTAssertEqual(store.autoOpenServerURL, "https://omnigent.example.com/omnigent")

    store.suppressAutoOpening(oidcServer: URL(string: "https://omnigent.example.com/omnigent/")!)
    XCTAssertEqual(store.serverURL, "https://omnigent.example.com/omnigent")
    XCTAssertNil(store.autoOpenServerURL)
    XCTAssertTrue(store.recentServers.isEmpty)
    // The suppression survives a relaunch.
    XCTAssertNil(SettingsStore(defaults: defaults).autoOpenServerURL)
    XCTAssertEqual(
      SettingsStore(defaults: defaults).serverURL, "https://omnigent.example.com/omnigent")

    // Reconnecting to the same server keeps it until a load succeeds.
    store.serverURL = "https://omnigent.example.com/omnigent"
    XCTAssertNil(store.autoOpenServerURL)
    store.connectionSucceeded()
    XCTAssertEqual(store.autoOpenServerURL, "https://omnigent.example.com/omnigent")
  }

  func testChoosingAnotherServerEndsTheSuppression() {
    let store = SettingsStore(defaults: defaults)
    store.serverURL = "https://omnigent.example.com"
    store.suppressAutoOpening(oidcServer: URL(string: "https://omnigent.example.com")!)
    store.serverURL = "https://other.example.com"
    XCTAssertEqual(store.autoOpenServerURL, "https://other.example.com")
    store.serverURL = "https://omnigent.example.com"
    XCTAssertEqual(store.autoOpenServerURL, "https://omnigent.example.com")
  }

  func testKnownServerURLMatchesByOrigin() {
    // A recorded server URL carries the workspace mount (/ml/omnigents); a deep
    // link matches by ORIGIN and reuses that recorded URL so the mount is
    // already present — no probe needed.
    let store = SettingsStore(defaults: defaults)
    store.rememberRecentServer(
      URL(string: "https://my-workspace.cloud.databricks.com/ml/omnigents")!)

    let known = store.knownServerURL(forOrigin: "https://my-workspace.cloud.databricks.com")
    XCTAssertEqual(
      known?.absoluteString, "https://my-workspace.cloud.databricks.com/ml/omnigents")
  }

  func testKnownServerURLReturnsNilForNeverConnectedOrigin() {
    let store = SettingsStore(defaults: defaults)
    store.rememberRecentServer(URL(string: "https://other.example.com")!)

    XCTAssertNil(store.knownServerURL(forOrigin: "https://never-seen.example.com"))
  }
}
