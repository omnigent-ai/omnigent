import Security
import XCTest

@testable import Omnigent

final class DatabricksCredentialStoreTests: XCTestCase {
  func testScopeCanonicalizationAndIsolation() throws {
    let first = try credentialScope(
      workspace: "https://DBC-123.cloud.databricks.com:443/omnigent/c/abc?o=123#view")
    let same = try credentialScope()
    XCTAssertEqual(first, same)
    XCTAssertEqual(first.account, same.account)
    XCTAssertNotEqual(first, try credentialScope(clientID: "other-client"))
    XCTAssertNotEqual(first.account, try credentialScope(clientID: "other-client").account)
    XCTAssertNotEqual(first, try credentialScope(workspace: "https://adb-123.azuredatabricks.net"))
  }

  func testKeychainRoundTripRotationAndScopedDeletion() throws {
    let service = "ai.omnigent.ios.tests.oauth.\(UUID().uuidString)"
    let store = DatabricksCredentialStore(service: service)
    let original = try credentialScope()
    let anotherClient = try credentialScope(clientID: "another-client")
    let anotherWorkspace = try credentialScope(workspace: "https://adb-123.azuredatabricks.net")
    defer {
      for scope in [original, anotherClient, anotherWorkspace] { try? store.delete(for: scope) }
    }
    XCTAssertNil(try store.load(for: original))
    let tokens = credentialTokens()
    for scope in [original, anotherClient, anotherWorkspace] { try store.save(tokens, for: scope) }
    let reloaded = DatabricksCredentialStore(service: service)
    XCTAssertEqual(try reloaded.load(for: original), tokens)

    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: original.account, kSecAttrSynchronizable as String: false,
      kSecReturnAttributes as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var attributes: CFTypeRef?
    XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, &attributes), errSecSuccess)
    let item = try XCTUnwrap(attributes as? [String: Any])
    XCTAssertEqual(
      item[kSecAttrAccessible as String] as? String,
      kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String)
    XCTAssertFalse((item[kSecAttrSynchronizable as String] as? Bool) ?? false)

    let rotated = credentialTokens(access: "next-access", refresh: "next-refresh")
    try reloaded.save(rotated, for: original)
    XCTAssertEqual(try store.load(for: original), rotated)
    try store.delete(for: original)
    try store.delete(for: original)
    XCTAssertNil(try store.load(for: original))
    XCTAssertEqual(try store.load(for: anotherClient), tokens)
    XCTAssertEqual(try store.load(for: anotherWorkspace), tokens)
  }

  func testRejectsMalformedAndFutureRecordsWithoutDeletingThem() throws {
    let service = "ai.omnigent.ios.tests.oauth.\(UUID().uuidString)"
    let store = DatabricksCredentialStore(service: service)
    let scope = try credentialScope()
    defer { try? store.delete(for: scope) }
    try store.save(credentialTokens(), for: scope)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: scope.account, kSecAttrSynchronizable as String: false,
    ]
    for data in [
      Data("not-json".utf8),
      Data(
        #"{"version":99,"tokens":{"accessToken":"a","refreshToken":"r","expiresAt":1000}}"#.utf8),
    ] {
      XCTAssertEqual(
        SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary),
        errSecSuccess)
      XCTAssertThrowsError(try store.load(for: scope)) {
        XCTAssertEqual($0 as? DatabricksCredentialError, .invalidData)
      }
      XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, nil), errSecSuccess)
    }
  }
}

func credentialScope(
  workspace: String = "https://dbc-123.cloud.databricks.com", clientID: String = "test-client"
) throws -> DatabricksCredentialScope {
  try DatabricksCredentialScope(
    workspaceURL: URL(string: workspace)!,
    configuration: DatabricksOAuthConfiguration(
      clientID: clientID, redirectURL: "https://login.databricks.com/mobile-redirect"))
}

func credentialTokens(
  access: String = "test-access", refresh: String = "test-refresh", expiry: TimeInterval = 3000
) -> DatabricksOAuthTokens {
  DatabricksOAuthTokens(
    accessToken: access, refreshToken: refresh, expiresAt: Date(timeIntervalSince1970: expiry))
}

/// Locked test storage; never falls through to the app's real Keychain service.
final class MemoryDatabricksCredentialStore: DatabricksCredentialStoring, @unchecked Sendable {
  enum Operation { case load, save, delete }
  private let lock = NSLock()
  private var values: [DatabricksCredentialScope: DatabricksOAuthTokens] = [:]
  private var failing: Operation?

  func fail(_ operation: Operation?) {
    lock.lock()
    defer { lock.unlock() }
    failing = operation
  }

  func snapshot(for scope: DatabricksCredentialScope) -> DatabricksOAuthTokens? {
    lock.lock()
    defer { lock.unlock() }
    return values[scope]
  }

  func load(for scope: DatabricksCredentialScope) throws -> DatabricksOAuthTokens? {
    lock.lock()
    defer { lock.unlock() }
    if failing == .load { throw DatabricksCredentialError.keychain(errSecInteractionNotAllowed) }
    return values[scope]
  }

  func save(_ tokens: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) throws {
    lock.lock()
    defer { lock.unlock() }
    if failing == .save { throw DatabricksCredentialError.keychain(errSecInteractionNotAllowed) }
    values[scope] = tokens
  }

  func delete(for scope: DatabricksCredentialScope) throws {
    lock.lock()
    defer { lock.unlock() }
    if failing == .delete { throw DatabricksCredentialError.keychain(errSecInteractionNotAllowed) }
    values.removeValue(forKey: scope)
  }
}
