import Foundation

enum WorkspaceURLExpander {
  static let workspaceUIPath = "/ml/omnigents"

  static func expandIfNeeded(_ url: URL, session: URLSession = .shared) async -> URL {
    guard url.scheme?.lowercased() == "https", isBareRoot(url), let origin = originURL(for: url) else {
      return url
    }

    var request = URLRequest(url: origin)
    request.httpMethod = "HEAD"
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.timeoutInterval = 8

    do {
      let (_, response) = try await session.data(for: request)
      guard let http = response as? HTTPURLResponse else { return url }
      guard (http.value(forHTTPHeaderField: "server") ?? "").lowercased() == "databricks" else {
        return url
      }
      return URL(string: "\(origin.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")))\(workspaceUIPath)") ?? url
    } catch {
      return url
    }
  }

  private static func isBareRoot(_ url: URL) -> Bool {
    url.path.isEmpty || url.path == "/"
  }

  private static func originURL(for url: URL) -> URL? {
    guard let scheme = url.scheme, let host = url.host else { return nil }
    var components = URLComponents()
    components.scheme = scheme
    components.host = host
    components.port = url.port
    components.path = "/"
    return components.url
  }
}
