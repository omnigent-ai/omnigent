import Foundation

struct WatchSessionPage: Decodable, Sendable {
  let data: [WatchSessionSummary]
}

struct WatchProjectSummary: Decodable, Sendable {
  let id: String?
  let name: String
}

struct WatchSessionSummary: Decodable, Identifiable, Sendable {
  let id: String
  let title: String?
  let agentName: String?
  let hostID: String?
  let projectID: String?
  let status: String
  let pendingElicitationsCount: Int

  var displayTitle: String {
    let title = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return title.isEmpty ? (agentName ?? "Untitled session") : title
  }

  var isActive: Bool { status == "launching" || status == "running" || status == "waiting" }

  var statusLabel: String {
    if pendingElicitationsCount > 0 {
      return pendingElicitationsCount == 1 ? "1 approval" : "\(pendingElicitationsCount) approvals"
    }
    return status == "idle" ? "Ready" : status.capitalized
  }

  private enum CodingKeys: String, CodingKey {
    case id, title, status
    case agentName = "agent_name"
    case hostID = "host_id"
    case projectID = "project_id"
    case pendingElicitationsCount = "pending_elicitations_count"
  }
}

struct WatchSessionDisplayItem: Identifiable, Sendable {
  let session: WatchSessionSummary
  let projectName: String?

  var id: String { session.id }
}

struct WatchProjectSessionGroup: Identifiable, Sendable {
  let id: String
  let name: String
  let sessions: [WatchSessionDisplayItem]
}

struct WatchSessionGrouping: Sendable {
  let needsAttention: [WatchSessionDisplayItem]
  let active: [WatchSessionDisplayItem]
  let projects: [WatchProjectSessionGroup]
  let recent: [WatchSessionDisplayItem]

  init(sessions: [WatchSessionSummary], projects: [WatchProjectSummary]) {
    let names = Dictionary(
      projects.compactMap { project in project.id.map { ($0, project.name) } },
      uniquingKeysWith: { first, _ in first }
    )
    let items = sessions.map {
      WatchSessionDisplayItem(session: $0, projectName: $0.projectID.flatMap { names[$0] })
    }
    needsAttention = items.filter { $0.session.pendingElicitationsCount > 0 }
    active = items.filter {
      $0.session.isActive && $0.session.pendingElicitationsCount == 0
    }
    let inactive = items.filter {
      !$0.session.isActive && $0.session.pendingElicitationsCount == 0
    }
    self.projects = projects.compactMap { project in
      guard let id = project.id else { return nil }
      let matches = inactive.filter { $0.session.projectID == id }
      return matches.isEmpty
        ? nil : WatchProjectSessionGroup(id: id, name: project.name, sessions: matches)
    }
    recent = inactive.filter { $0.projectName == nil }
  }
}

struct WatchSessionSnapshot: Decodable, Sendable {
  let status: String?
  let lastTaskError: WatchTaskError?
  let pendingElicitations: [WatchElicitation]
  let pendingInputs: [WatchPendingInput]

  private enum CodingKeys: String, CodingKey {
    case status
    case lastTaskError = "last_task_error"
    case pendingElicitations = "pending_elicitations"
    case pendingInputs = "pending_inputs"
  }

  init(from decoder: Decoder) throws {
    let values = try decoder.container(keyedBy: CodingKeys.self)
    status = try values.decodeIfPresent(String.self, forKey: .status)
    lastTaskError = try values.decodeIfPresent(WatchTaskError.self, forKey: .lastTaskError)
    pendingElicitations =
      try values.decodeIfPresent([WatchElicitation].self, forKey: .pendingElicitations) ?? []
    pendingInputs =
      try values.decodeIfPresent([WatchPendingInput].self, forKey: .pendingInputs) ?? []
  }
}

struct WatchTaskError: Decodable, Sendable {
  let message: String?
}

struct WatchTextContent: Decodable, Sendable {
  let text: String?
}

extension Collection where Element == WatchTextContent {
  fileprivate var transcriptText: String {
    compactMap(\.text).joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
  }
}

struct WatchPendingInput: Decodable, Identifiable, Sendable {
  let id: String
  let text: String

  init(id: String, text: String) {
    self.id = id
    self.text = text
  }

  init(from decoder: Decoder) throws {
    let values = try decoder.container(keyedBy: CodingKeys.self)
    id = try values.decode(String.self, forKey: .id)
    text =
      try values.decodeIfPresent([WatchTextContent].self, forKey: .content)?.transcriptText ?? ""
  }

  var transcriptMessage: WatchTranscriptMessage? {
    text.isEmpty ? nil : WatchTranscriptMessage(id: "pending:\(id)", role: "user", text: text)
  }

  private enum CodingKeys: String, CodingKey {
    case id = "pending_id"
    case content
  }
}

struct WatchSendMessageAcknowledgement: Decodable, Sendable {
  let pendingID: String?
  let denied: Bool?
  let reason: String?

  private enum CodingKeys: String, CodingKey {
    case denied, reason
    case pendingID = "pending_id"
  }
}

struct WatchSessionItemPage: Decodable, Sendable {
  let data: [WatchSessionItem]

  var messages: [WatchTranscriptMessage] {
    Array(data.compactMap(\.transcriptMessage).reversed())
  }
}

struct WatchSessionItem: Decodable, Sendable {
  let id: String
  let type: String
  let role: String?
  let content: [WatchTextContent]?

  var transcriptMessage: WatchTranscriptMessage? {
    guard type == "message", role == "user" || role == "assistant", let role else { return nil }
    let text = (content ?? []).transcriptText
    return text.isEmpty ? nil : WatchTranscriptMessage(id: id, role: role, text: text)
  }
}

struct WatchTranscriptMessage: Identifiable, Equatable, Sendable {
  let id: String
  let role: String
  let text: String
}

enum WatchTranscript {
  static func presented(
    committed: [WatchTranscriptMessage],
    pending: [WatchPendingInput],
    streaming: [WatchTranscriptMessage],
    limit: Int = 8
  ) -> [WatchTranscriptMessage] {
    let pendingMessages = pending.compactMap(\.transcriptMessage)
    return Array((committed + pendingMessages + streaming).suffix(limit))
  }

  static func reconcile(
    snapshot: [WatchPendingInput],
    provisional: WatchPendingInput?,
    committed: [WatchTranscriptMessage],
    baselineUserIDs: Set<String>
  ) -> [WatchPendingInput] {
    var inputs = snapshot
    if let provisional, !inputs.contains(where: { $0.id == provisional.id }) {
      inputs.append(provisional)
    }
    var newUserText = committed.filter {
      $0.role == "user" && !baselineUserIDs.contains($0.id)
    }.map { normalize($0.text) }
    return inputs.filter { input in
      guard let index = newUserText.firstIndex(where: { $0.hasSuffix(normalize(input.text)) })
      else {
        return true
      }
      newUserText.remove(at: index)
      return false
    }
  }

  static func hasReply(
    to text: String,
    committed: [WatchTranscriptMessage],
    baselineUserIDs: Set<String>
  ) -> Bool {
    guard
      let index = committed.lastIndex(where: {
        $0.role == "user" && !baselineUserIDs.contains($0.id)
          && normalize($0.text).hasSuffix(normalize(text))
      })
    else { return false }
    return committed[committed.index(after: index)...].contains { $0.role == "assistant" }
  }

  private static func normalize(_ text: String) -> String {
    text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
  }
}

struct WatchStreamDelta: Equatable, Sendable {
  let text: String
  let messageID: String?
  let index: Int?
  let isFinal: Bool
}

struct WatchStreamInput: Equatable, Sendable {
  let message: WatchTranscriptMessage
  let pendingID: String?
}

enum WatchSessionStreamEvent: Equatable, Sendable {
  case heartbeat
  case responseStarted(String?)
  case textDelta(WatchStreamDelta)
  case inputConsumed(WatchStreamInput)
  case messageCommitted(WatchTranscriptMessage)
  case responseEnded
  case failed(String?)
  case streamFinished
}

struct WatchStreamingTranscript: Equatable, Sendable {
  private struct Entry: Equatable, Sendable {
    let key: String
    let messageID: String?
    var text: String
    var lastIndex: Int?
  }

  private var entries: [Entry] = []
  private var activeResponseID: String?
  private var generation = 0

  var messages: [WatchTranscriptMessage] {
    entries.map {
      WatchTranscriptMessage(id: "streaming:\($0.key)", role: "assistant", text: $0.text)
    }
  }

  mutating func start(responseID: String?) {
    guard let responseID, responseID != activeResponseID else { return }
    entries.removeAll { $0.messageID == nil }
    activeResponseID = responseID
  }

  mutating func append(_ delta: WatchStreamDelta) {
    let key = delta.messageID ?? activeResponseID ?? "anonymous-\(generation)"
    if let entryIndex = entries.firstIndex(where: { $0.key == key }) {
      if let index = delta.index, let lastIndex = entries[entryIndex].lastIndex,
        index <= lastIndex
      {
        return
      }
      entries[entryIndex].text += delta.text
      entries[entryIndex].lastIndex = delta.index ?? entries[entryIndex].lastIndex
    } else if !delta.text.isEmpty {
      entries.append(
        Entry(key: key, messageID: delta.messageID, text: delta.text, lastIndex: delta.index))
    }
    if delta.isFinal, delta.messageID == nil {
      activeResponseID = nil
      generation &+= 1
    }
  }

  mutating func reconcile(with committed: [WatchTranscriptMessage]) {
    for message in committed { commit(message) }
  }

  mutating func commit(_ message: WatchTranscriptMessage) {
    guard message.role == "assistant" else { return }
    let text = normalized(message.text)
    let prefixIndex = entries.indices
      .filter {
        let streamed = normalized(entries[$0].text)
        return !streamed.isEmpty && text.hasPrefix(streamed)
      }
      .max { entries[$0].text.count < entries[$1].text.count }
    let index =
      entries.firstIndex { $0.messageID == message.id }
      ?? entries.firstIndex { normalized($0.text) == text }
      ?? prefixIndex
    if let index { entries.remove(at: index) }
  }

  mutating func reset() {
    generation &+= 1
    entries.removeAll()
    activeResponseID = nil
  }

  private func normalized(_ text: String) -> String {
    text.trimmingCharacters(in: .whitespacesAndNewlines)
  }
}

struct WatchSessionSSEParser: Sendable {
  private static let maximumBytes = 1_048_576

  private var frame: [UInt8] = []
  private var discardingFrame = false
  private var endedLine = false
  private(set) var isFinished = false

  mutating func feed(_ data: Data) -> [WatchSessionStreamEvent] {
    data.flatMap { feed($0) }
  }

  mutating func feed(_ byte: UInt8) -> [WatchSessionStreamEvent] {
    guard !isFinished else { return [] }
    if !discardingFrame {
      frame.append(byte)
    }
    if frame.count > Self.maximumBytes {
      frame.removeAll(keepingCapacity: true)
      discardingFrame = true
    }
    guard byte == 0x0A else {
      if byte != 0x0D { endedLine = false }
      return []
    }
    guard endedLine else {
      endedLine = true
      return []
    }
    defer {
      frame.removeAll(keepingCapacity: true)
      discardingFrame = false
      endedLine = false
    }
    guard !discardingFrame else { return [] }

    let payload = String(decoding: frame, as: UTF8.self)
      .split(whereSeparator: \.isNewline)
      .compactMap { line -> Substring? in
        guard line.hasPrefix("data:") else { return nil }
        let value = line.dropFirst(5)
        return value.first == " " ? value.dropFirst() : value
      }
      .joined(separator: "\n")
    if payload.trimmingCharacters(in: .whitespacesAndNewlines) == "[DONE]" {
      isFinished = true
      return [.streamFinished]
    }
    return Self.decode(payload).map { [$0] } ?? []
  }

  private static func decode(_ payload: String) -> WatchSessionStreamEvent? {
    guard let data = payload.data(using: .utf8),
      let event = try? JSONDecoder().decode(WireStreamEvent.self, from: data)
    else { return nil }
    let type: String
    if event.type.contains(".TaskStatus."), let status = event.type.split(separator: ".").last {
      type = "response.\(status.lowercased())"
    } else {
      type = event.type
    }
    switch type {
    case "session.heartbeat":
      return .heartbeat
    case "response.created", "response.in_progress":
      return .responseStarted(event.response?.id)
    case "response.output_text.delta":
      guard let delta = event.delta else { return nil }
      return .textDelta(
        WatchStreamDelta(
          text: delta,
          messageID: event.messageID,
          index: event.index,
          isFinal: event.isFinal ?? false
        ))
    case "session.input.consumed":
      guard let input = event.data?.transcriptMessage else { return nil }
      return .inputConsumed(
        WatchStreamInput(message: input, pendingID: event.data?.clearedPendingID))
    case "response.output_item.done":
      return event.item?.transcriptMessage.map(WatchSessionStreamEvent.messageCommitted)
    case "response.completed", "response.incomplete", "response.cancelled":
      return .responseEnded
    case "response.failed":
      return .failed(event.response?.errorMessage ?? event.error?.message)
    case "response.error":
      return .failed(event.error?.message ?? event.message)
    case "session.status" where event.status == "failed":
      return .failed(event.error?.message)
    default:
      return nil
    }
  }
}

private struct WireStreamEvent: Decodable {
  struct Response: Decodable {
    let id: String?
    let error: ErrorDetail?
    let lastError: ErrorDetail?

    var errorMessage: String? { error?.message ?? lastError?.message }

    private enum CodingKeys: String, CodingKey {
      case id, error
      case lastError = "last_error"
    }
  }

  struct ConsumedInput: Decodable {
    struct MessageData: Decodable {
      let role: String?
      let content: [WatchTextContent]?
    }

    let itemID: String
    let type: String
    let data: MessageData
    let clearedPendingID: String?

    var transcriptMessage: WatchTranscriptMessage? {
      guard type == "message", data.role == "user" else { return nil }
      let text = (data.content ?? []).transcriptText
      return text.isEmpty ? nil : WatchTranscriptMessage(id: itemID, role: "user", text: text)
    }

    private enum CodingKeys: String, CodingKey {
      case type, data
      case itemID = "item_id"
      case clearedPendingID = "cleared_pending_id"
    }
  }

  struct ErrorDetail: Decodable {
    let message: String?
  }

  let type: String
  let delta: String?
  let messageID: String?
  let index: Int?
  let isFinal: Bool?
  let response: Response?
  let data: ConsumedInput?
  let item: WatchSessionItem?
  let error: ErrorDetail?
  let message: String?
  let status: String?

  private enum CodingKeys: String, CodingKey {
    case type, delta, index, response, data, item, error, message, status
    case messageID = "message_id"
    case isFinal = "final"
  }
}

struct WatchElicitation: Decodable, Identifiable, Sendable {
  let id: String
  let params: Params

  struct Params: Decodable, Sendable {
    let message: String
    let contentPreview: String?
    let targetSessionID: String?
    let mode: String
    let requestedSchema: RequestedSchema?

    var canResolveOnWatch: Bool {
      mode == "form" && requestedSchema?.hasFields != true
    }

    private enum CodingKeys: String, CodingKey {
      case message, mode, requestedSchema
      case contentPreview = "content_preview"
      case targetSessionID = "target_session_id"
    }
  }

  struct RequestedSchema: Decodable, Sendable {
    private struct Field: Decodable, Sendable {}

    private let properties: [String: Field]?
    var hasFields: Bool { properties?.isEmpty == false }
  }

  private enum CodingKeys: String, CodingKey {
    case id = "elicitation_id"
    case params
  }
}
