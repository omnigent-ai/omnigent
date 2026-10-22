import Foundation
import Security

/// An OIDC server's refresh grant; the session token itself lives only in the cookie.
struct OidcRefreshGrant: Equatable, Sendable {
  let refreshToken: String
  let userID: String?
}

/// Refresh grants keyed by normalized server origin (`URL.omnigentOrigin`).
protocol OidcCredentialStoring: Sendable {
  func load(origin: String) throws -> OidcRefreshGrant?
  func save(_ grant: OidcRefreshGrant, origin: String) throws
  func delete(origin: String) throws
}

/// Keychain-backed grants: device-only, never synced, readable only while unlocked.
struct OidcCredentialStore: OidcCredentialStoring {
  private let service: String

  init(service: String = "ai.omnigent.ios.oidc") {
    self.service = service
  }

  func load(origin: String) throws -> OidcRefreshGrant? {
    var query = query(for: origin)
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess else { throw OidcCredentialStoreError.keychain(status) }
    guard let data = result as? Data,
      let record = try? JSONDecoder().decode(Record.self, from: data),
      record.version == Record.currentVersion, !record.refreshToken.isEmpty
    else { throw OidcCredentialStoreError.invalidData }
    return OidcRefreshGrant(refreshToken: record.refreshToken, userID: record.userID)
  }

  func save(_ grant: OidcRefreshGrant, origin: String) throws {
    guard !grant.refreshToken.isEmpty else { throw OidcCredentialStoreError.invalidData }
    let data = try JSONEncoder().encode(
      Record(
        version: Record.currentVersion, refreshToken: grant.refreshToken, userID: grant.userID))
    let query = query(for: origin)
    let attributes: [String: Any] = [
      kSecValueData as String: data,
      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
      let item = query.merging(attributes) { _, new in new }
      status = SecItemAdd(item as CFDictionary, nil)
      if status == errSecDuplicateItem {
        status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
      }
    }
    guard status == errSecSuccess else { throw OidcCredentialStoreError.keychain(status) }
  }

  func delete(origin: String) throws {
    let status = SecItemDelete(query(for: origin) as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw OidcCredentialStoreError.keychain(status)
    }
  }

  private func query(for origin: String) -> [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: origin,
      kSecAttrSynchronizable as String: false,
    ]
  }

  private struct Record: Codable {
    static let currentVersion = 1
    let version: Int
    let refreshToken: String
    let userID: String?
  }
}

enum OidcCredentialStoreError: Error, Equatable, LocalizedError {
  case keychain(OSStatus)
  case invalidData

  var errorDescription: String? {
    switch self {
    case .keychain:
      "Could not access the saved sign-in. Unlock the device and try again."
    case .invalidData: "The saved sign-in is invalid. Sign in again."
    }
  }
}
