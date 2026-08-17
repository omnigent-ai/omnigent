import Foundation

struct WatchCredentials: Codable, Equatable, Sendable {
  let serverURL: URL
  let cookieHeader: String
  let syncedAt: Date
}

enum WatchEndpoint {
  /// Strip conversation routes and map a Databricks UI mount to its API proxy.
  static func apiBaseURL(for serverURL: URL, workspaceUIPath: String) -> URL {
    guard var components = URLComponents(url: serverURL, resolvingAgainstBaseURL: false) else {
      return serverURL
    }
    var segments = components.path.split(separator: "/")
    if segments.count >= 2, segments[segments.count - 2] == "c" {
      segments.removeLast(2)
      components.path = segments.isEmpty ? "" : "/" + segments.joined(separator: "/")
    }
    components.query = nil
    components.fragment = nil
    guard
      components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        == workspaceUIPath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    else { return components.url ?? serverURL }

    components.path = "/api/2.0/omnigent"
    return components.url ?? serverURL
  }

  static func url(baseURL: URL, path: String, queryItems: [URLQueryItem] = []) -> URL {
    var url = baseURL.appending(
      path: path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    if !queryItems.isEmpty { url.append(queryItems: queryItems) }
    return url
  }
}
