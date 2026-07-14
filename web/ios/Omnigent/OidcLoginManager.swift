import Foundation
import UIKit
import WebKit

final class OidcLoginManager {
  @MainActor
  private var loginTask: Task<Void, Never>?

  @MainActor
  var isInFlight: Bool { loginTask != nil }

  @MainActor
  func start(origin: String, cookieStore: WKHTTPCookieStore, onSession: @escaping () -> Void) {
    guard loginTask == nil, let originURL = URL(string: origin) else { return }

    loginTask = Task { [weak self] in
      defer { self?.loginTask = nil }
      do {
        let ticket = try await Self.requestTicket(origin: originURL)
        guard await UIApplication.shared.open(ticket.loginURL) else { return }
        guard let token = try await Self.pollForToken(origin: originURL, ticket: ticket.id)
        else { return }
        let cookie = try Self.sessionCookie(origin: originURL, token: token)
        await cookieStore.setCookie(cookie)
        guard !Task.isCancelled else { return }
        onSession()
      } catch is CancellationError {
        return
      } catch {
        return
      }
    }
  }

  @MainActor
  func cancel() {
    loginTask?.cancel()
    loginTask = nil
  }

  struct Ticket: Equatable {
    let id: String
    let loginURL: URL
  }

  static func ticket(from data: Data, origin: URL) throws -> Ticket {
    let response = try JSONDecoder().decode(TicketResponse.self, from: data)
    guard !response.ticket.isEmpty,
      response.loginURL.hasPrefix("/"),
      !response.loginURL.hasPrefix("//"),
      let loginURL = URL(string: response.loginURL, relativeTo: origin)?.absoluteURL,
      loginURL.omnigentOrigin == origin.omnigentOrigin
    else { throw LoginError.invalidResponse }
    return Ticket(id: response.ticket, loginURL: loginURL)
  }

  static func token(from data: Data) throws -> String {
    let response = try JSONDecoder().decode(PollResponse.self, from: data)
    guard isJWTShaped(response.token) else { throw LoginError.invalidResponse }
    return response.token
  }

  static func sessionCookie(origin: URL, token: String) throws -> HTTPCookie {
    guard origin.host != nil, isJWTShaped(token) else { throw LoginError.invalidResponse }
    var properties: [HTTPCookiePropertyKey: Any] = [
      .originURL: origin,
      .path: "/",
      .name: origin.scheme?.lowercased() == "https" ? "__Host-ap_session" : "ap_session",
      .value: token,
    ]
    if origin.scheme?.lowercased() == "https" {
      properties[.secure] = "TRUE"
    }
    guard let cookie = HTTPCookie(properties: properties) else { throw LoginError.invalidResponse }
    return cookie
  }

  private static func requestTicket(origin: URL) async throws -> Ticket {
    var request = URLRequest(url: endpoint("/auth/cli-login", origin: origin))
    request.httpMethod = "POST"
    request.httpBody = Data()
    request.timeoutInterval = requestTimeout
    let (data, response) = try await URLSession.shared.data(for: request)
    guard (response as? HTTPURLResponse)?.statusCode == 200 else {
      throw LoginError.invalidResponse
    }
    return try ticket(from: data, origin: origin)
  }

  private static func pollForToken(origin: URL, ticket: String) async throws -> String? {
    let clock = ContinuousClock()
    let deadline = clock.now + pollTimeout
    while clock.now < deadline {
      try await Task.sleep(for: pollInterval)
      var components = URLComponents(
        url: endpoint("/auth/cli-poll", origin: origin), resolvingAgainstBaseURL: false)
      components?.queryItems = [URLQueryItem(name: "ticket", value: ticket)]
      guard let url = components?.url else { throw LoginError.invalidResponse }
      var request = URLRequest(url: url)
      request.timeoutInterval = requestTimeout
      do {
        let (data, response) = try await URLSession.shared.data(for: request)
        switch (response as? HTTPURLResponse)?.statusCode {
        case 200:
          return try token(from: data)
        case 202:
          continue
        case 410:
          return nil
        default:
          continue
        }
      } catch is CancellationError {
        throw CancellationError()
      } catch {
        continue
      }
    }
    return nil
  }

  private static func endpoint(_ path: String, origin: URL) -> URL {
    URL(string: path, relativeTo: origin)!.absoluteURL
  }

  private static func isJWTShaped(_ value: String) -> Bool {
    let parts = value.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { return false }
    let allowed = CharacterSet(
      charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    return parts.allSatisfy {
      !$0.isEmpty && $0.unicodeScalars.allSatisfy(allowed.contains)
    }
  }

  private struct TicketResponse: Decodable {
    let ticket: String
    let loginURL: String

    enum CodingKeys: String, CodingKey {
      case ticket
      case loginURL = "login_url"
    }
  }

  private struct PollResponse: Decodable {
    let token: String
  }

  private enum LoginError: Error {
    case invalidResponse
  }

  private static let pollInterval = Duration.seconds(2)
  private static let pollTimeout = Duration.seconds(300)
  private static let requestTimeout: TimeInterval = 10
}
