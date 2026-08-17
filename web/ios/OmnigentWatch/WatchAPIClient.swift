import Foundation

struct WatchAPIClient: Sendable {
  private static let sliceKeyHeader = "X-Databricks-Omnigent-Slice-Key"
  private static let maximumErrorBytes = 65_536

  let credentials: WatchCredentials

  func sessions() async throws -> [WatchSessionSummary] {
    let url = try endpoint(
      "/v1/sessions",
      queryItems: [
        URLQueryItem(name: "order", value: "desc"),
        URLQueryItem(name: "sort_by", value: "updated_at"),
        URLQueryItem(name: "limit", value: "20"),
      ]
    )
    return try decoder.decode(WatchSessionPage.self, from: await send(URLRequest(url: url))).data
  }

  func projects() async throws -> [WatchProjectSummary] {
    let data = try await send(URLRequest(url: endpoint("/v1/sessions/projects")))
    return try decoder.decode([WatchProjectSummary].self, from: data)
  }

  func session(id: String, hostID: String?) async throws -> WatchSessionSnapshot {
    let url = try endpoint(
      "/v1/sessions/\(id)",
      queryItems: [
        URLQueryItem(name: "include_items", value: "false"),
        URLQueryItem(name: "include_liveness", value: "false"),
      ]
    )
    var request = URLRequest(url: url)
    addSliceKey(hostID, to: &request)
    return try decoder.decode(WatchSessionSnapshot.self, from: await send(request))
  }

  func messages(sessionID: String, hostID: String?) async throws -> [WatchTranscriptMessage] {
    let url = try endpoint(
      "/v1/sessions/\(sessionID)/items",
      queryItems: [
        URLQueryItem(name: "order", value: "desc")
      ]
    )
    var request = URLRequest(url: url)
    addSliceKey(hostID, to: &request)
    let page = try decoder.decode(WatchSessionItemPage.self, from: await send(request))
    return Array(page.messages.suffix(8))
  }

  func resolve(
    sessionID: String,
    elicitationID: String,
    action: String,
    hostID: String?
  ) async throws {
    var request = URLRequest(
      url: try endpoint("/v1/sessions/\(sessionID)/elicitations/\(elicitationID)/resolve")
    )
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try JSONSerialization.data(withJSONObject: ["action": action])
    addSliceKey(hostID, to: &request)
    _ = try await send(request)
  }

  func sendMessage(
    sessionID: String,
    text: String,
    hostID: String?
  ) async throws -> WatchSendMessageAcknowledgement {
    let body: [String: Any] = [
      "type": "message",
      "data": [
        "role": "user",
        "content": [["type": "input_text", "text": text]],
      ],
    ]
    var request = URLRequest(url: try endpoint("/v1/sessions/\(sessionID)/events"))
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    addSliceKey(hostID, to: &request)
    let acknowledgement = try decoder.decode(
      WatchSendMessageAcknowledgement.self,
      from: await send(request)
    )
    if acknowledgement.denied == true {
      throw WatchAPIError.messageDenied(acknowledgement.reason ?? "The server denied this message.")
    }
    return acknowledgement
  }

  func stream(
    sessionID: String,
    hostID: String?
  ) throws -> AsyncThrowingStream<WatchSessionStreamEvent, Error> {
    var request = URLRequest(url: try endpoint("/v1/sessions/\(sessionID)/stream"))
    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
    request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
    request.timeoutInterval = 45
    addSliceKey(hostID, to: &request)
    request = authenticated(request)

    return AsyncThrowingStream { continuation in
      let producer = Task {
        do {
          let (bytes, response) = try await openStream(request)
          defer { bytes.task.cancel() }
          guard
            response.value(forHTTPHeaderField: "Content-Type")?
              .localizedCaseInsensitiveContains("text/event-stream") == true
          else { throw WatchAPIError.unexpectedStream }

          try await withTaskCancellationHandler {
            var parser = WatchSessionSSEParser()
            for try await byte in bytes {
              try Task.checkCancellation()
              for event in parser.feed(byte) {
                continuation.yield(event)
                if case .streamFinished = event {
                  continuation.finish()
                  return
                }
              }
            }
          } onCancel: {
            bytes.task.cancel()
          }
          try Task.checkCancellation()
          throw WatchAPIError.streamEnded
        } catch is CancellationError {
          continuation.finish()
        } catch let error as URLError where error.code == .cancelled {
          continuation.finish()
        } catch {
          continuation.finish(throwing: error)
        }
      }
      continuation.onTermination = { _ in producer.cancel() }
    }
  }

  private var decoder: JSONDecoder { JSONDecoder() }

  private func endpoint(_ path: String, queryItems: [URLQueryItem] = []) throws -> URL {
    WatchEndpoint.url(baseURL: credentials.serverURL, path: path, queryItems: queryItems)
  }

  private func addSliceKey(_ hostID: String?, to request: inout URLRequest) {
    guard credentials.serverURL.path.hasPrefix("/api/2.0/omnigent"), let hostID else { return }
    request.setValue(hostID, forHTTPHeaderField: Self.sliceKeyHeader)
  }

  private func send(_ original: URLRequest) async throws -> Data {
    var request = authenticated(original)
    var (data, response) = try await URLSession.shared.data(for: request)
    if request.httpMethod == "GET", request.value(forHTTPHeaderField: Self.sliceKeyHeader) != nil,
      isWrongReplica(response: response, data: data)
    {
      request.setValue(nil, forHTTPHeaderField: Self.sliceKeyHeader)
      (data, response) = try await URLSession.shared.data(for: request)
    }
    _ = try validate(response)
    return data
  }

  private func openStream(
    _ original: URLRequest
  ) async throws -> (URLSession.AsyncBytes, HTTPURLResponse) {
    var request = original
    var (bytes, response) = try await URLSession.shared.bytes(for: request)
    if request.value(forHTTPHeaderField: Self.sliceKeyHeader) != nil,
      try await isWrongReplica(response: response, bytes: bytes)
    {
      request.setValue(nil, forHTTPHeaderField: Self.sliceKeyHeader)
      (bytes, response) = try await URLSession.shared.bytes(for: request)
    }
    do {
      return (bytes, try validate(response))
    } catch {
      bytes.task.cancel()
      throw error
    }
  }

  private func isWrongReplica(
    response: URLResponse,
    bytes: URLSession.AsyncBytes
  ) async throws -> Bool {
    guard (response as? HTTPURLResponse)?.statusCode == 400 else { return false }
    defer { bytes.task.cancel() }
    var data = Data()
    for try await byte in bytes {
      guard data.count < Self.maximumErrorBytes else { return false }
      data.append(byte)
    }
    return isWrongReplica(response: response, data: data)
  }

  private func isWrongReplica(response: URLResponse, data: Data) -> Bool {
    guard (response as? HTTPURLResponse)?.statusCode == 400,
      let envelope = try? decoder.decode(WatchAPIErrorEnvelope.self, from: data)
    else { return false }
    return envelope.error?.code == "wrong_replica"
  }

  private func authenticated(_ original: URLRequest) -> URLRequest {
    var request = original
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.setValue("watchos", forHTTPHeaderField: "X-Omnigent-Client")
    if !credentials.cookieHeader.isEmpty {
      request.setValue(credentials.cookieHeader, forHTTPHeaderField: "Cookie")
    }
    return request
  }

  private func validate(_ response: URLResponse) throws -> HTTPURLResponse {
    guard let http = response as? HTTPURLResponse else { throw WatchAPIError.invalidResponse }
    guard (200..<300).contains(http.statusCode) else {
      if http.statusCode == 401 || http.statusCode == 403 {
        throw WatchAPIError.authenticationExpired
      }
      throw WatchAPIError.httpStatus(http.statusCode)
    }
    return http
  }
}

private struct WatchAPIErrorEnvelope: Decodable {
  struct Detail: Decodable {
    let code: String?
  }

  let error: Detail?
}

enum WatchAPIError: LocalizedError {
  case invalidResponse
  case authenticationExpired
  case httpStatus(Int)
  case messageDenied(String)
  case unexpectedStream
  case streamEnded

  var errorDescription: String? {
    switch self {
    case .invalidResponse: "Invalid server response."
    case .authenticationExpired: "Reopen Omnigent on your iPhone and sign in."
    case .httpStatus(let status): "Server returned HTTP \(status)."
    case .messageDenied(let reason): reason
    case .unexpectedStream: "Server returned an unsupported stream."
    case .streamEnded: "Live connection lost."
    }
  }
}
