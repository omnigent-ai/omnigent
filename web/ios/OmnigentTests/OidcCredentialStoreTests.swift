import Security
import XCTest

@testable import Omnigent

final class OidcCredentialStoreTests: XCTestCase {
  private let origin = "https://omnigent.example.com"
  private let otherOrigin = "https://omnigent.example.com:8443"

  func testKeychainRoundTripIsPerOriginAndDeviceOnly() throws {
    let service = "ai.omnigent.ios.tests.oidc.\(UUID().uuidString)"
    let store = OidcCredentialStore(service: service)
    defer {
      try? store.delete(origin: origin)
      try? store.delete(origin: otherOrigin)
    }
    XCTAssertNil(try store.load(origin: origin))
    let grant = OidcRefreshGrant(refreshToken: "refresh-1", userID: "user-1")
    try store.save(grant, origin: origin)
    XCTAssertNil(try store.load(origin: otherOrigin))
    XCTAssertEqual(try OidcCredentialStore(service: service).load(origin: origin), grant)

    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: origin, kSecAttrSynchronizable as String: false,
      kSecReturnAttributes as String: true, kSecReturnData as String: true,
      kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var result: CFTypeRef?
    XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, &result), errSecSuccess)
    let item = try XCTUnwrap(result as? [String: Any])
    XCTAssertEqual(
      item[kSecAttrAccessible as String] as? String,
      kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String)
    XCTAssertFalse((item[kSecAttrSynchronizable as String] as? Bool) ?? false)
    let record = try XCTUnwrap(
      JSONSerialization.jsonObject(with: XCTUnwrap(item[kSecValueData as String] as? Data))
        as? [String: Any])
    XCTAssertEqual(record["version"] as? Int, 1)
    XCTAssertEqual(record["refreshToken"] as? String, "refresh-1")
    XCTAssertEqual(record["userID"] as? String, "user-1")

    let replaced = OidcRefreshGrant(refreshToken: "refresh-2", userID: nil)
    try store.save(replaced, origin: origin)
    try store.save(grant, origin: otherOrigin)
    XCTAssertEqual(try store.load(origin: origin), replaced)
    try store.delete(origin: origin)
    try store.delete(origin: origin)
    XCTAssertNil(try store.load(origin: origin))
    XCTAssertEqual(try store.load(origin: otherOrigin), grant)
  }

  func testRejectsEmptyGrantsAndInvalidRecords() throws {
    let service = "ai.omnigent.ios.tests.oidc.\(UUID().uuidString)"
    let store = OidcCredentialStore(service: service)
    defer { try? store.delete(origin: origin) }
    XCTAssertThrowsError(
      try store.save(OidcRefreshGrant(refreshToken: "", userID: nil), origin: origin)
    ) {
      XCTAssertEqual($0 as? OidcCredentialStoreError, .invalidData)
    }
    try store.save(OidcRefreshGrant(refreshToken: "refresh", userID: nil), origin: origin)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: origin, kSecAttrSynchronizable as String: false,
    ]
    for data in [
      Data("not-json".utf8),
      Data(#"{"version":2,"refreshToken":"refresh"}"#.utf8),
      Data(#"{"version":1,"refreshToken":""}"#.utf8),
      Data(#"{"version":1}"#.utf8),
    ] {
      XCTAssertEqual(
        SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary),
        errSecSuccess)
      XCTAssertThrowsError(try store.load(origin: origin)) {
        XCTAssertEqual($0 as? OidcCredentialStoreError, .invalidData)
      }
    }
  }
}
