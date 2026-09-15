import Foundation
import Security

struct DatabricksCredentialScope: Hashable, Sendable {
  let workspaceOrigin: URL
  let clientID: String
  let workspaceID: String?

  init(workspaceURL: URL, configuration: DatabricksOAuthConfiguration) throws {
    guard var origin = URLComponents(url: workspaceURL, resolvingAgainstBaseURL: false),
      origin.scheme?.lowercased() == "https",
      ServerAuthentication(host: origin.host) == .databricksWorkspace,
      origin.user == nil, origin.password == nil,
      origin.port == nil || origin.port == 443
    else { throw DatabricksOAuthError.invalidWorkspace }
    let identifiers = (origin.queryItems ?? []).filter { $0.name == "o" }
    guard identifiers.count <= 1 else { throw DatabricksOAuthError.invalidWorkspace }
    if let identifier = identifiers.first {
      guard let value = identifier.value, !value.isEmpty,
        value.utf8.allSatisfy({ (48...57).contains($0) })
      else { throw DatabricksOAuthError.invalidWorkspace }
      workspaceID = value
    } else {
      workspaceID = nil
    }
    origin.scheme = "https"
    origin.host = origin.host?.lowercased()
    origin.port = nil
    origin.path = ""
    origin.query = nil
    origin.fragment = nil
    guard let url = origin.url else { throw DatabricksOAuthError.invalidWorkspace }
    workspaceOrigin = url
    clientID = configuration.clientID
  }

  var account: String {
    let origin = workspaceOrigin.absoluteString
    // Length-prefix the origin so neither component can collide with the separator.
    let base = "\(origin.utf8.count):\(origin)\(clientID)"
    guard let workspaceID else { return base }
    return "o:\(workspaceID.count):\(workspaceID):\(base)"
  }
}

protocol DatabricksCredentialStoring: Sendable {
  func load(for scope: DatabricksCredentialScope) throws -> DatabricksOAuthTokens?
  func save(_ tokens: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) throws
  func delete(for scope: DatabricksCredentialScope) throws
}

struct DatabricksCredentialStore: DatabricksCredentialStoring {
  private let service: String

  init(service: String = "ai.omnigent.ios.databricks-oauth") {
    self.service = service
  }

  func load(for scope: DatabricksCredentialScope) throws -> DatabricksOAuthTokens? {
    var query = query(for: scope)
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess else { throw DatabricksCredentialError.keychain(status) }
    guard let data = result as? Data,
      let record = try? JSONDecoder().decode(Record.self, from: data),
      record.version == (record.tokens.issuer == nil ? 1 : 2), record.tokens.isValid
    else { throw DatabricksCredentialError.invalidData }
    return record.tokens
  }

  func save(_ tokens: DatabricksOAuthTokens, for scope: DatabricksCredentialScope) throws {
    guard tokens.isValid else { throw DatabricksCredentialError.invalidData }
    let data = try JSONEncoder().encode(
      Record(version: tokens.issuer == nil ? 1 : 2, tokens: tokens))
    let query = query(for: scope)
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
    guard status == errSecSuccess else { throw DatabricksCredentialError.keychain(status) }
  }

  func delete(for scope: DatabricksCredentialScope) throws {
    let status = SecItemDelete(query(for: scope) as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw DatabricksCredentialError.keychain(status)
    }
  }

  private func query(for scope: DatabricksCredentialScope) -> [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: scope.account,
      kSecAttrSynchronizable as String: false,
    ]
  }

  private struct Record: Codable {
    let version: Int
    let tokens: DatabricksOAuthTokens
  }
}

enum DatabricksCredentialError: Error, Equatable, LocalizedError {
  case keychain(OSStatus)
  case invalidData

  var errorDescription: String? {
    switch self {
    case .keychain:
      "Could not access saved Databricks credentials. Unlock the device and try again."
    case .invalidData: "The saved Databricks credentials are invalid. Sign in again."
    }
  }
}
