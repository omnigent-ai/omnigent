import Foundation
import Security

struct WatchKeychain: Sendable {
  static let shared = WatchKeychain()
  private let service: String

  init(service: String = "ai.omnigent.ios.watch") {
    self.service = service
  }

  private var query: [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: WatchCredentials.contextKey,
      kSecAttrSynchronizable as String: false,
    ]
  }

  func load() -> WatchCredentials? {
    var result: CFTypeRef?
    let search = query.merging([kSecReturnData as String: true]) { $1 }
    guard SecItemCopyMatching(search as CFDictionary, &result) == errSecSuccess,
      let data = result as? Data
    else { return nil }
    return try? JSONDecoder().decode(WatchCredentials.self, from: data)
  }

  func save(
    _ credentials: WatchCredentials?,
    update: (CFDictionary, CFDictionary) -> OSStatus = SecItemUpdate,
    add: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus = SecItemAdd,
    delete: (CFDictionary) -> OSStatus = SecItemDelete
  ) -> OSStatus {
    guard let credentials else {
      let status = delete(query as CFDictionary)
      return status == errSecItemNotFound ? errSecSuccess : status
    }
    guard let data = try? JSONEncoder().encode(credentials) else { return errSecParam }
    let attributes: [String: Any] = [
      kSecValueData as String: data,
      kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    var status = update(query as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
      status = add(query.merging(attributes) { $1 } as CFDictionary, nil)
      if status == errSecDuplicateItem {
        status = update(query as CFDictionary, attributes as CFDictionary)
      }
    }
    return status
  }
}
