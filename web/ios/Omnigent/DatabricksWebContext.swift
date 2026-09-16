import CryptoKit
import Foundation
import WebKit

struct DatabricksWebContext: Sendable {
  let configuration: DatabricksOAuthConfiguration
  let scope: DatabricksCredentialScope
  let pageURL: URL

  init(url: URL, configuration: DatabricksOAuthConfiguration) throws {
    guard
      !(url.omnigentOrigin == configuration.redirectURL.omnigentOrigin
        && url.path == configuration.redirectURL.path),
      !DatabricksWebSession.isLoginPath(url.path)
    else { throw DatabricksOAuthError.invalidWorkspace }
    self.configuration = configuration
    scope = try DatabricksCredentialScope(workspaceURL: url, configuration: configuration)
    pageURL = WorkspaceURLExpander.workspaceUIURL(forBareRoot: url) ?? url
  }

  static func resolve(_ url: URL) throws -> Self? {
    guard ServerAuthentication(origin: url.omnigentOrigin) == .databricksWorkspace else {
      return nil
    }
    return try Self(url: url, configuration: .load())
  }

  static func viewIdentity(for url: URL) -> String {
    let context = contextIdentity(for: url)
    guard context != "generic" else { return context }
    return context + ":" + urlFingerprint(url)
  }

  static func serverLabel(for url: URL) -> String {
    if let context = try? resolve(url), let id = context.scope.workspaceID {
      return url.omnigentHostLabel + " · " + id
    }
    return url.omnigentHostLabel
  }

  static func contextIdentity(for url: URL) -> String {
    do { return try resolve(url)?.storeIdentifier.uuidString ?? "generic" } catch {
      return "invalid-workspace:" + urlFingerprint(url)
    }
  }

  private static func urlFingerprint(_ url: URL) -> String {
    SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
  }

  var storeIdentifier: UUID {
    var bytes = Array(
      SHA256.hash(data: Data(("omnigent.databricks.web.v1:" + scope.account).utf8)).prefix(16))
    bytes[6] = (bytes[6] & 0x0F) | 0x80
    bytes[8] = (bytes[8] & 0x3F) | 0x80
    return UUID(
      uuid: (
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
      ))
  }
}

@MainActor
protocol DatabricksWebStoring: AnyObject {
  var identifier: UUID { get }
  func cookies() async -> [HTTPCookie]
  func setCookie(_ cookie: HTTPCookie) async
  func deleteCookie(_ cookie: HTTPCookie) async
  func clear() async
}

@MainActor
final class DatabricksWebStore: DatabricksWebStoring {
  let identifier: UUID
  let websiteDataStore: WKWebsiteDataStore

  init(identifier: UUID) {
    self.identifier = identifier
    websiteDataStore = WKWebsiteDataStore(forIdentifier: identifier)
  }

  func cookies() async -> [HTTPCookie] { await websiteDataStore.httpCookieStore.allCookies() }
  func setCookie(_ cookie: HTTPCookie) async {
    await websiteDataStore.httpCookieStore.setCookie(cookie)
  }
  func deleteCookie(_ cookie: HTTPCookie) async {
    await websiteDataStore.httpCookieStore.deleteCookie(cookie)
  }
  func clear() async {
    await websiteDataStore.removeData(
      ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
  }
}

/// Serialize mutations across WebViews using the same persistent store, including canceled writes.
@MainActor
final class DatabricksCookieInstaller {
  static let shared = DatabricksCookieInstaller()
  private var tails: [UUID: (id: UUID, task: Task<Void, Never>)] = [:]

  func install(
    _ cookies: [HTTPCookie], in store: any DatabricksWebStoring, reset: Bool,
    validate: @escaping @MainActor () async throws -> Void = {}
  ) async throws {
    try Task.checkCancellation()
    let identifier = store.identifier
    let previous = tails[identifier]?.task
    let id = UUID()
    let operation = Task {
      await previous?.value
      try Task.checkCancellation()
      try await validate()
      try Task.checkCancellation()
      if reset {
        await store.clear()
      } else {
        let replacedNames = Set(cookies.map(\.name)).union(["DBAUTH"])
        for cookie in await store.cookies() where replacedNames.contains(cookie.name) {
          try Task.checkCancellation()
          await store.deleteCookie(cookie)
        }
      }
      for cookie in cookies {
        try Task.checkCancellation()
        await store.setCookie(cookie)
      }
      try Task.checkCancellation()
    }
    let tail = Task { _ = await operation.result }
    tails[identifier] = (id, tail)
    defer { if tails[identifier]?.id == id { tails.removeValue(forKey: identifier) } }
    try await withTaskCancellationHandler {
      try await operation.value
      try Task.checkCancellation()
    } onCancel: {
      operation.cancel()
    }
  }
}
