import Foundation

/// What the iPhone hands the watch: the signed-in server's API root and the cookies a browser
/// would send to it. The watch then calls the existing API directly over its own network.
struct WatchCredentials: Codable, Hashable, Sendable {
  static let contextKey = "credentials"

  let apiURL: URL
  let cookie: String
}

/// Orders credential callbacks so a delayed context cannot undo a newer sign-out.
struct WatchCredentialState {
  private(set) var credentials: WatchCredentials?
  private var lastSentAt: Date?

  init(credentials: WatchCredentials? = nil) {
    self.credentials = credentials
  }

  mutating func apply(_ data: Data, sentAt: Date) -> Bool {
    if let lastSentAt, sentAt <= lastSentAt { return false }
    credentials = try? JSONDecoder().decode(WatchCredentials.self, from: data)
    lastSentAt = sentAt
    return true
  }
}

struct WatchPage<Element: Decodable & Sendable>: Decodable, Sendable {
  let data: [Element]
  let lastId: String?
  let hasMore: Bool?
}

struct WatchProject: Decodable, Sendable {
  let id: String?
  let name: String
}

struct WatchSession: Decodable, Hashable, Identifiable, Sendable {
  let id: String
  let title: String?
  let hostId: String?
  let projectId: String?
  var status: String
  var pendingElicitationsCount: Int?
  let updatedAt: TimeInterval?
  let viewerUnread: Bool?

  var name: String {
    let title = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return title.isEmpty ? "Untitled session" : title
  }
  var approvals: Int { pendingElicitationsCount ?? 0 }
  var updated: Date? { updatedAt.map(Date.init(timeIntervalSince1970:)) }
  var isActive: Bool { status == "running" || status == "waiting" }
  var statusLabel: String {
    if approvals > 0 { return approvals == 1 ? "1 approval" : "\(approvals) approvals" }
    return status == "idle" ? "Ready" : status.capitalized
  }
}

struct WatchSessionGroup: Identifiable, Sendable {
  let title: String
  let symbol: String
  let sessions: [WatchSession]
  var id: String { symbol + title }
}

extension [WatchSession] {
  /// The web sidebar's layout: a section per project in display order, then other sessions.
  func grouped(projects: [WatchProject]) -> [WatchSessionGroup] {
    let projectIDs = Set(projects.compactMap(\.id))
    let byProject = projects.compactMap { project in
      project.id.map { id in
        WatchSessionGroup(
          title: project.name, symbol: "folder", sessions: filter { $0.projectId == id })
      }
    }
    let others = filter { !projectIDs.contains($0.projectId ?? "") }
    return (byProject + [WatchSessionGroup(title: "Sessions", symbol: "", sessions: others)])
      .filter { !$0.sessions.isEmpty }
  }
}

struct WatchSnapshot: Decodable, Sendable {
  struct TaskError: Decodable, Sendable { let message: String? }

  let status: String?
  let hostId: String?
  let parentSessionId: String?
  let labels: [String: String]?
  let lastTaskError: TaskError?
  let pendingElicitations: [WatchElicitation]?
  let pendingInputs: [WatchItem]?

  func messages(items: [WatchItem]) -> [WatchMessage] {
    let committed = Array(items.reversed().bubbles.suffix(12))
    // Pending IDs are distinct from committed IDs; equal text can be a separate user input.
    return committed + (pendingInputs ?? []).compactMap(\.bubble)
  }
}

/// A committed item or a queued input (`pending_id`): both share this shape.
struct WatchItem: Decodable, Sendable {
  struct Part: Decodable, Sendable { let text: String? }

  let id: String?
  let pendingId: String?
  let type: String?
  let role: String?
  let content: [Part]?
  let message: String?
  let isMeta: Bool?
  let userAuthored: Bool?
  let createdBy: String?

  private var rawText: String { (content ?? []).compactMap(\.text).joined(separator: "\n") }

  var isInjectedReminder: Bool {
    role == "user" && pendingId == nil && userAuthored != true && (createdBy ?? "").isEmpty
      && rawText.contains("<system-reminder>")
  }

  /// Strip injected reminders, but preserve literal markup in authored and assistant messages.
  var text: String {
    let visible =
      isInjectedReminder
      ? rawText.replacing(#/<system-reminder>[\s\S]*?</system-reminder>/#, with: "") : rawText
    return visible.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  /// The chat bubble for a message or a failed turn; nil for tool calls and empty turns.
  var bubble: WatchMessage? {
    // Hidden durable context (skill instructions, wake notices) the web never shows.
    if isMeta == true { return nil }
    if type == "error", let id, let message {
      return WatchMessage(id: "error:" + id, isUser: false, text: message)
    }
    guard !text.isEmpty else { return nil }
    if let pendingId { return WatchMessage(id: "pending:" + pendingId, isUser: true, text: text) }
    guard let id, type == "message", role == "user" || role == "assistant" else { return nil }
    return WatchMessage(id: id, isUser: role == "user", text: text)
  }
}

extension Sequence<WatchItem> {
  /// The chat bubbles in order. Claude Code re-records a prompt behind injected context; show it once.
  var bubbles: [WatchMessage] {
    reduce(into: []) { shown, item in
      guard let bubble = item.bubble else { return }
      guard
        !(item.isInjectedReminder && shown.last?.isUser == true && shown.last?.text == bubble.text)
      else { return }
      shown.append(bubble)
    }
  }
}

struct WatchMessage: Hashable, Identifiable, Sendable {
  let id: String
  let isUser: Bool
  let text: String
  var isPending: Bool { id.hasPrefix("pending:") }
  var isError: Bool { id.hasPrefix("error:") }
}

struct WatchElicitation: Decodable, Identifiable, Sendable {
  struct Params: Decodable, Sendable {
    let mode: String?
    let message: String
    let contentPreview: String?
    let targetSessionId: String?
    let requestedSchema: Schema?
    let askUserQuestion: AnyField?
  }
  struct Schema: Decodable, Sendable { let properties: [String: AnyField]? }
  struct AnyField: Decodable, Sendable { init(from decoder: Decoder) {} }

  let elicitationId: String
  let params: Params
  var id: String { elicitationId }
  /// Plain yes/no prompts only; questions, forms, and URL flows need the phone.
  var isBinary: Bool {
    (params.mode ?? "form") == "form" && params.askUserQuestion == nil
      && params.requestedSchema?.properties?.isEmpty != false
  }
}

enum WatchError: LocalizedError, Equatable {
  case signedOut
  case status(Int)
  case routingUnavailable

  var errorDescription: String? {
    switch self {
    case .signedOut: "Open Omnigent on your iPhone to sign in again."
    case .status(let code): "The server returned HTTP \(code)."
    case .routingUnavailable:
      "Couldn't find this session's host. Open it on your iPhone to respond."
    }
  }
}

struct WatchClient: Sendable {
  static let decoder = {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
  }()
  private static let sliceKey = "X-Databricks-Omnigent-Slice-Key"
  private static let session: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpShouldSetCookies = false
    return URLSession(
      configuration: configuration, delegate: WatchRedirectBlocker(), delegateQueue: nil)
  }()

  let credentials: WatchCredentials

  /// A hostless child uses its nearest host-bound ancestor, as in the web client.
  func routingHost(for sessionID: String) async throws -> String? {
    var current = sessionID
    var visited: Set<String> = []
    for _ in 0..<16 {
      guard visited.insert(current).inserted else { throw WatchError.routingUnavailable }
      let state: WatchSnapshot = try await get(
        "sessions/\(current)",
        [
          "include_items": "false", "include_liveness": "false", "include_live_status": "false",
          "include_usage": "false",
        ])
      for host in [state.hostId, state.labels?["omnigent.sandbox.requested_host_id"]] {
        if let host, !host.isEmpty { return host }
      }
      let parent =
        state.parentSessionId ?? state.labels?["omnigent.side_chat.source_id"]
        ?? (state.labels?["omnigent.side_chat"] == "1"
          ? state.labels?["omnigent.fork.source_id"] : nil)
      guard let parent, !parent.isEmpty else { return nil }
      current = parent
    }
    throw WatchError.routingUnavailable
  }

  func get<Response: Decodable>(
    _ path: String, _ query: [String: String] = [:], host: String? = nil
  ) async throws -> Response {
    try Self.decoder.decode(Response.self, from: await send(request(path, query, host: host)))
  }

  func post(_ path: String, _ body: [String: Any], host: String?) async throws {
    var request = request(path, host: host)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    _ = try await send(request)
  }

  func request(_ path: String, _ query: [String: String] = [:], host: String?) -> URLRequest {
    var url = credentials.apiURL.appending(path: path)
    url.append(queryItems: query.sorted { $0.key < $1.key }.map(URLQueryItem.init))
    var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
    request.setValue(
      credentials.cookie.isEmpty ? nil : credentials.cookie, forHTTPHeaderField: "Cookie")
    request.setValue(host, forHTTPHeaderField: Self.sliceKey)
    return request
  }

  private func send(_ request: URLRequest) async throws -> Data {
    var request = request
    var (data, response) = try await Self.session.data(for: request)
    // A stale host key reaches a replica without the session's tunnel; retry keyless like the web.
    if (response as? HTTPURLResponse)?.statusCode == 400,
      request.value(forHTTPHeaderField: Self.sliceKey) != nil,
      String(decoding: data, as: UTF8.self).contains("wrong_replica")
    {
      request.setValue(nil, forHTTPHeaderField: Self.sliceKey)
      (data, response) = try await Self.session.data(for: request)
    }
    switch (response as? HTTPURLResponse)?.statusCode ?? 0 {
    case 200..<300: return data
    case 401, 403: throw WatchError.signedOut
    case let code: throw WatchError.status(code)
    }
  }
}

/// API redirects must not carry scoped cookies to another host or path.
private final class WatchRedirectBlocker: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}
