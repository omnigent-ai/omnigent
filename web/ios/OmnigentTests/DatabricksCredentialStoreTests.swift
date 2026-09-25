import Security
import XCTest

@testable import Omnigent

final class DatabricksCredentialStoreTests: XCTestCase {
  func testScopeCanonicalizationAndIsolation() throws {
    let first = try credentialScope(
      workspace: "https://DBC-123.cloud.databricks.com:443/omnigent/c/abc?view=chat#view")
    let same = try credentialScope()
    XCTAssertEqual(first, same)
    XCTAssertEqual(first.account, same.account)
    XCTAssertNotEqual(first, try credentialScope(clientID: "other-client"))
    XCTAssertNotEqual(first.account, try credentialScope(clientID: "other-client").account)
    XCTAssertNotEqual(first, try credentialScope(workspace: "https://adb-123.azuredatabricks.net"))
  }

  func testWorkspaceIDsSeparateScopesWithoutChangingLegacyKeys() throws {
    let base = "https://dbc-123.cloud.databricks.com"
    let legacy = try credentialScope(workspace: base)
    XCTAssertEqual(legacy.account, "\(base.utf8.count):\(base)test-client")
    let first = try credentialScope(workspace: base + "/omnigent?o=123")
    let same = try credentialScope(workspace: base + "/omnigent/c/abc?view=chat&o=123#fragment")
    let other = try credentialScope(workspace: base + "/omnigent?o=456")
    XCTAssertEqual(first, same)
    XCTAssertEqual(first.account, same.account)
    XCTAssertNotEqual(first.account, other.account)
    XCTAssertNotEqual(first.account, legacy.account)
    XCTAssertEqual(first.workspaceID, "123")
    for query in ["o=", "o", "o=123&o=456", "o=abc", "o=-1", "o=12%203"] {
      XCTAssertThrowsError(try credentialScope(workspace: base + "/omnigent?" + query)) {
        XCTAssertEqual($0 as? DatabricksOAuthError, .invalidWorkspace)
      }
    }
  }

  func testKeychainRoundTripRotationAndScopedDeletion() throws {
    let service = "ai.omnigent.ios.tests.oauth.\(UUID().uuidString)"
    let store = DatabricksCredentialStore(service: service)
    let original = try credentialScope()
    let anotherClient = try credentialScope(clientID: "another-client")
    let anotherWorkspace = try credentialScope(workspace: "https://adb-123.azuredatabricks.net")
    let spogWorkspace = try credentialScope(workspace: "https://dbc-123.cloud.databricks.com?o=123")
    defer {
      for scope in [original, anotherClient, anotherWorkspace, spogWorkspace] {
        try? store.delete(for: scope)
      }
    }
    XCTAssertNil(try store.load(for: original))
    let tokens = credentialTokens()
    try store.save(tokens, for: original)
    XCTAssertNil(try store.load(for: spogWorkspace))
    for scope in [anotherClient, anotherWorkspace, spogWorkspace] {
      try store.save(tokens, for: scope)
    }
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
    XCTAssertEqual(try store.load(for: spogWorkspace), tokens)
  }

  func testIssuerAwareRecordsUpgradeToVersionTwo() throws {
    let service = "ai.omnigent.ios.tests.oauth.\(UUID().uuidString)"
    let scope = try credentialScope()
    let store = DatabricksCredentialStore(service: service)
    defer { try? store.delete(for: scope) }
    try store.save(credentialTokens(), for: scope)
    XCTAssertNil(try store.load(for: scope)?.issuer)
    let issuer = try DatabricksOAuthIssuer(
      URL(string: "https://accounts.cloud.databricks.com/oidc/accounts/test-account")!)
    let tokens = credentialTokens(issuer: issuer)
    try store.save(tokens, for: scope)
    let restored = try DatabricksCredentialStore(service: service).load(for: scope)
    XCTAssertEqual(restored, tokens)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: scope.account, kSecAttrSynchronizable as String: false,
      kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var result: CFTypeRef?
    XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, &result), errSecSuccess)
    let record = try XCTUnwrap(
      JSONSerialization.jsonObject(with: XCTUnwrap(result as? Data)) as? [String: Any])
    XCTAssertEqual(record["version"] as? Int, 2)
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
      Data(#"{"version":2,"tokens":{"accessToken":"a","refreshToken":"r","expiresAt":1000}}"#.utf8),
      Data(
        #"{"version":2,"tokens":{"accessToken":"a","refreshToken":"r","expiresAt":1000,"issuer":"https://example.com/oidc"}}"#
          .utf8),
      Data(
        #"{"version":1,"tokens":{"accessToken":"a","refreshToken":"r","expiresAt":1000,"issuer":"https://accounts.cloud.databricks.com/oidc/accounts/test"}}"#
          .utf8),
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
  access: String = "test-access", refresh: String = "test-refresh", expiry: TimeInterval = 3000,
  issuer: DatabricksOAuthIssuer? = nil
) -> DatabricksOAuthTokens {
  DatabricksOAuthTokens(
    accessToken: access, refreshToken: refresh, expiresAt: Date(timeIntervalSince1970: expiry),
    issuer: issuer)
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
