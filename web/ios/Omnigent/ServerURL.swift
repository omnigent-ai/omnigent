import Foundation

enum ServerURLError: LocalizedError, Equatable {
  case empty
  case invalid(String)
  case unsupportedScheme(String)
  case insecureHTTPNotAllowed

  var errorDescription: String? {
    switch self {
    case .empty:
      "Server URL is empty."
    case .invalid(let message):
      "Invalid URL: \(message)"
    case .unsupportedScheme(let scheme):
      "Unsupported scheme '\(scheme)'. Use https."
    case .insecureHTTPNotAllowed:
      "iOS release builds require https:// server URLs."
    }
  }
}

enum ServerURL {
  static func normalize(_ raw: String, allowsInsecureHTTP: Bool) throws -> URL {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { throw ServerURLError.empty }

    let withScheme: String
    if trimmed.contains("://") {
      withScheme = trimmed
    } else {
      withScheme = "\(allowsInsecureHTTP ? "http" : "https")://\(trimmed)"
    }

    guard let url = URL(string: withScheme), let scheme = url.scheme?.lowercased() else {
      throw ServerURLError.invalid(withScheme)
    }
    guard scheme == "http" || scheme == "https" else {
      throw ServerURLError.unsupportedScheme(scheme)
    }
    if scheme == "http" && !allowsInsecureHTTP {
      throw ServerURLError.insecureHTTPNotAllowed
    }
    return url
  }
}
