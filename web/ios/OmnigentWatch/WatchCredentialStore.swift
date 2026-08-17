import Foundation
import Security

enum WatchCredentialStore {
  private static let service = "ai.omnigent.ios.watch.credentials"
  private static let account = "current-server"

  static func load() -> WatchCredentials? {
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
      kSecReturnData as String: true,
      kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var result: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
      let data = result as? Data
    else { return nil }
    return try? JSONDecoder().decode(WatchCredentials.self, from: data)
  }

  static func save(_ credentials: WatchCredentials) throws {
    let data = try JSONEncoder().encode(credentials)
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
    ]
    let attributes: [String: Any] = [
      kSecValueData as String: data,
      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
      var inserted = query
      inserted.merge(attributes) { _, new in new }
      let insertStatus = SecItemAdd(inserted as CFDictionary, nil)
      guard insertStatus == errSecSuccess else { throw StoreError.status(insertStatus) }
    } else if status != errSecSuccess {
      throw StoreError.status(status)
    }
  }

  enum StoreError: Error {
    case status(OSStatus)
  }
}
