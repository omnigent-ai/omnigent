import ManagedApp
import XCTest

@testable import Omnigent

/// Decodes the configuration straight from property lists, the exact shape the
/// ManagedApp framework hands `init(from:)`. No MDM, device management, or
/// simulator hook is involved, which matters because none of those can be faked
/// locally — the decoder is the part most likely to be wrong, so it is the part
/// covered here.
final class ManagedConfigurationTests: XCTestCase {
  // MARK: Accepted configurations

  func testAbsentKeyOffersNoServers() throws {
    XCTAssertEqual(try decode([:]), .empty)
  }

  func testEmptyListOffersNoServers() throws {
    XCTAssertEqual(try decode(["serverUrls": []]), .empty)
  }

  func testAdministratorOrderIsPreserved() throws {
    let config = try decode([
      "serverUrls": ["https://b.example.com", "https://a.example.com"]
    ])

    XCTAssertEqual(
      config.serverURLs.map(\.absoluteString),
      ["https://b.example.com", "https://a.example.com"])
  }

  func testBareHostGetsHTTPS() throws {
    let config = try decode(["serverUrls": ["omnigent.corp.example.com"]])

    XCTAssertEqual(config.serverURLs.map(\.absoluteString), ["https://omnigent.corp.example.com"])
  }

  func testWorkspaceMountIsPreserved() throws {
    // The Databricks mount must survive decoding; discovering it is a network
    // probe that stays on the connect path, so a preset URL that already names
    // the mount has to keep it.
    let config = try decode([
      "serverUrls": ["https://ws.cloud.databricks.com/ml/omnigents"]
    ])

    XCTAssertEqual(
      config.serverURLs.map(\.absoluteString),
      ["https://ws.cloud.databricks.com/ml/omnigents"])
  }

  func testDuplicateOriginsAreDroppedKeepingTheFirst() throws {
    let config = try decode([
      "serverUrls": [
        "https://a.example.com", "https://A.Example.com/", "https://b.example.com",
      ]
    ])

    XCTAssertEqual(
      config.serverURLs.map(\.absoluteString),
      ["https://a.example.com", "https://b.example.com"])
  }

  func testMaximumEntriesIsAccepted() throws {
    let entries = (1...OmnigentManagedConfiguration.maxServerURLs).map {
      "https://s\($0).example.com"
    }

    XCTAssertEqual(try decode(["serverUrls": entries]).serverURLs.count, entries.count)
  }

  // MARK: Rejected configurations
  //
  // Each case must surface OUR documented code, because that code is what the
  // administrator sees in the device event log and in our published spec.

  func testInvalidURLIsRejected() {
    assertFails(
      ["serverUrls": ["https://"]], with: OmnigentManagedConfiguration.invalidURLCode)
  }

  func testBlankEntryIsRejected() {
    assertFails(["serverUrls": ["   "]], with: OmnigentManagedConfiguration.invalidURLCode)
  }

  func testInsecureHTTPIsRejected() {
    assertFails(
      ["serverUrls": ["http://omnigent.corp.example.com"]],
      with: OmnigentManagedConfiguration.notHTTPSCode)
  }

  func testNonWebSchemeIsRejected() {
    assertFails(
      ["serverUrls": ["ftp://omnigent.corp.example.com"]],
      with: OmnigentManagedConfiguration.notHTTPSCode)
  }

  func testTooManyEntriesAreRejected() {
    let entries = (0...OmnigentManagedConfiguration.maxServerURLs).map {
      "https://s\($0).example.com"
    }

    assertFails(
      ["serverUrls": entries], with: OmnigentManagedConfiguration.tooManyEntriesCode)
  }

  func testAStringInsteadOfAListIsRejected() {
    assertFails(
      ["serverUrls": "https://omnigent.corp.example.com"],
      with: OmnigentManagedConfiguration.wrongTypeCode)
  }

  func testOneBadEntryRejectsTheWholeConfiguration() {
    // Reporting the mistake beats silently offering a partial list the admin
    // believes is complete.
    assertFails(
      ["serverUrls": ["https://good.example.com", "http://bad.example.com"]],
      with: OmnigentManagedConfiguration.notHTTPSCode)
  }

  func testOurErrorCodesAreNotSystemReserved() {
    // Also forces the static codes to be evaluated: they are built with a
    // force-unwrapped `init(rawValue:)`, so a value that drifted into the
    // reserved range fails here rather than on a managed device.
    for code in [
      OmnigentManagedConfiguration.invalidURLCode,
      OmnigentManagedConfiguration.notHTTPSCode,
      OmnigentManagedConfiguration.tooManyEntriesCode,
      OmnigentManagedConfiguration.wrongTypeCode,
    ] {
      XCTAssertLessThan(
        code.rawValue, ManagedAppConfigurationDecodingErrorCode.firstReserved,
        "Code \(code.rawValue) is system-reserved and would be reported as generic.")
    }
  }

  // MARK: Classic (non-declarative) channel
  //
  // Same flat dictionary, delivered through the app's own defaults domain. This
  // channel cannot report errors to the administrator, so anything unusable
  // degrades to offering nothing instead of throwing.

  func testLegacyDictionaryIsRead() {
    let defaults = makeDefaults()
    defaults.set(
      ["serverUrls": ["https://corp.example.com"]],
      forKey: OmnigentManagedConfiguration.legacyDefaultsKey)

    XCTAssertEqual(
      OmnigentManagedConfiguration.read(legacyFrom: defaults).serverURLs.map(\.absoluteString),
      ["https://corp.example.com"])
  }

  func testAbsentLegacyKeyOffersNoServers() {
    XCTAssertEqual(OmnigentManagedConfiguration.read(legacyFrom: makeDefaults()), .empty)
  }

  func testInvalidLegacyDictionaryIsIgnoredRatherThanThrowing() {
    let defaults = makeDefaults()
    defaults.set(
      ["serverUrls": ["http://insecure.example.com"]],
      forKey: OmnigentManagedConfiguration.legacyDefaultsKey)

    XCTAssertEqual(OmnigentManagedConfiguration.read(legacyFrom: defaults), .empty)
  }

  func testLegacyDictionaryWithUnrelatedKeysIsTolerated() {
    // A service or other tooling may add keys of its own; they must not stop the
    // servers from being read.
    let defaults = makeDefaults()
    defaults.set(
      ["serverUrls": ["https://corp.example.com"], "somethingElse": 42],
      forKey: OmnigentManagedConfiguration.legacyDefaultsKey)

    XCTAssertEqual(OmnigentManagedConfiguration.read(legacyFrom: defaults).serverURLs.count, 1)
  }

  // MARK: Channel precedence

  func testDeclarativeConfigurationWinsOverLegacy() {
    let declarative = [URL(string: "https://declarative.example.com")!]
    let legacy = [URL(string: "https://legacy.example.com")!]

    XCTAssertEqual(ManagedServers.resolve(declarative: declarative, legacy: legacy), declarative)
  }

  func testLegacyIsUsedWhenNoDeclarativeConfigurationExists() {
    let legacy = [URL(string: "https://legacy.example.com")!]

    XCTAssertEqual(ManagedServers.resolve(declarative: [], legacy: legacy), legacy)
  }

  func testNeitherChannelOffersNothing() {
    XCTAssertEqual(ManagedServers.resolve(declarative: [], legacy: []), [])
  }

  // MARK: Helpers

  private func makeDefaults() -> UserDefaults {
    let name = "ManagedConfigurationTests.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: name)!
    addTeardownBlock { defaults.removePersistentDomain(forName: name) }
    return defaults
  }

  private func decode(_ plist: [String: Any]) throws -> OmnigentManagedConfiguration {
    let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
    return try PropertyListDecoder().decode(OmnigentManagedConfiguration.self, from: data)
  }

  private func assertFails(
    _ plist: [String: Any],
    with expected: ManagedAppConfigurationDecodingErrorCode,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    do {
      let config = try decode(plist)
      XCTFail(
        "Expected code \(expected.rawValue), decoded \(config.serverURLs)", file: file, line: line)
    } catch let failure as OmnigentManagedConfiguration.Failure {
      XCTAssertEqual(failure.code, expected, file: file, line: line)
      XCTAssertFalse(
        failure.message.isEmpty, "Admins need an actionable message", file: file, line: line)
    } catch {
      XCTFail("Expected a reportable failure, got \(error)", file: file, line: line)
    }
  }
}

/// The merge that decides what the connect screen and the server switcher show.
final class ManagedServersTests: XCTestCase {
  private let managed = [URL(string: "https://corp.example.com")!]

  func testRecentsDropAServerTheAdministratorAlreadyPresets() {
    let recents = ManagedServers.recents(
      ["https://corp.example.com", "https://mine.example.com"], excludingManaged: managed)

    XCTAssertEqual(recents, ["https://mine.example.com"])
  }

  func testRecentsAreUntouchedWithoutAConfiguration() {
    let recents = ["https://mine.example.com", "http://localhost:6767"]

    XCTAssertEqual(ManagedServers.recents(recents, excludingManaged: []), recents)
  }

  func testDifferingPortIsADifferentServer() {
    let recents = ManagedServers.recents(
      ["https://corp.example.com:8443"], excludingManaged: managed)

    XCTAssertEqual(recents, ["https://corp.example.com:8443"])
  }

  func testUnparseableRecentIsKept() {
    // Dropping an entry we can't parse would silently lose a server the user
    // connected to; the switcher already tolerates one it can't label.
    XCTAssertEqual(ManagedServers.recents(["not a url"], excludingManaged: managed), ["not a url"])
  }

  func testPresetServersComeFirst() {
    let merged = ManagedServers.merged(
      managed: managed, recents: ["https://mine.example.com", "https://corp.example.com"])

    XCTAssertEqual(merged, ["https://corp.example.com", "https://mine.example.com"])
  }
}
