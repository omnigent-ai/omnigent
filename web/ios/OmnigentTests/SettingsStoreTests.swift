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
