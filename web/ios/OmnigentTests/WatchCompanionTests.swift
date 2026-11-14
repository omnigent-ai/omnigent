import XCTest

@testable import Omnigent

final class WatchCompanionTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_000)

  @MainActor func testCredentialsSendOnlyCookiesTheAPIWouldReceive() throws {
    let cookies = [
      cookie("session", domain: "omnigent.example.com", path: "/"),
      cookie("parent", domain: ".example.com", path: "/omnigent"),
      cookie("other-host", domain: "other.example.com", path: "/"),
      cookie("other-path", domain: "omnigent.example.com", path: "/admin"),
      cookie("secure", domain: "omnigent.example.com", path: "/", secure: true),
      cookie("expired", domain: "omnigent.example.com", path: "/", expires: now),
    ]
    let http = try XCTUnwrap(
      WatchCredentials(
        serverURL: URL(string: "http://omnigent.example.com/omnigent/")!,
        cookies: cookies))
    XCTAssertEqual(http.apiURL.absoluteString, "http://omnigent.example.com/omnigent/v1")
    XCTAssertEqual(http.cookie, "session=1; parent=1")

    let https = try XCTUnwrap(
      WatchCredentials(
        serverURL: URL(string: "https://omnigent.example.com/omnigent?o=42&next=x")!,
        cookies: cookies))
    // A shared host's workspace selector survives; the rest of the page's query does not.
    XCTAssertEqual(https.apiURL.absoluteString, "https://omnigent.example.com/omnigent/v1?o=42")
    XCTAssertEqual(
      WatchClient(credentials: https).request("sessions", ["limit": "5"], host: nil).url?
        .absoluteString,
      "https://omnigent.example.com/omnigent/v1/sessions?o=42&limit=5")
    XCTAssertEqual(https.cookie, "session=1; parent=1; secure=1")
    // Sign-in can learn the selector for a bare shared host.
    let learned = WatchCredentials(
      serverURL: URL(string: "https://omnigent.example.com/omnigent")!, workspaceID: "7",
      cookies: [])
    XCTAssertEqual(learned?.apiURL.absoluteString, "https://omnigent.example.com/omnigent/v1?o=7")
  }

  func testClientAddressesTheAPIWithCookieAndHostKey() {
    let client = WatchClient(
      credentials: WatchCredentials(
        apiURL: URL(string: "https://omnigent.example.com/v1")!, cookie: "session=1"))
    let request = client.request(
      "sessions/conv_1/items", ["order": "desc", "limit": "5"], host: "h1")
    XCTAssertEqual(
      request.url?.absoluteString,
      "https://omnigent.example.com/v1/sessions/conv_1/items?limit=5&order=desc")
    XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "session=1")
    XCTAssertEqual(request.value(forHTTPHeaderField: "X-Databricks-Omnigent-Slice-Key"), "h1")
    XCTAssertNil(
      client.request("sessions", host: nil).value(
        forHTTPHeaderField: "X-Databricks-Omnigent-Slice-Key"))
  }

  func testSessionsGroupByProjectThenTheRest() throws {
    let json = """
      {"data": [
        {"id": "a", "status": "running", "pending_elicitations_count": 1},
        {"id": "b", "status": "running", "title": "  "},
        {"id": "c", "status": "idle", "project_id": "p1"},
        {"id": "d", "status": "failed", "project_id": "gone"},
        {"id": "e", "status": "waiting"}
      ]}
      """
    let sessions = try WatchClient.decoder.decode(
      WatchPage<WatchSession>.self, from: Data(json.utf8)
    ).data
    let groups = sessions.grouped(projects: [
      WatchProject(id: "p1", name: "Docs"), WatchProject(id: nil, name: "Legacy"),
    ])
    XCTAssertEqual(groups.map(\.title), ["Docs", "Sessions"])
    XCTAssertEqual(groups.map { $0.sessions.map(\.id) }, [["c"], ["a", "b", "d", "e"]])
    XCTAssertEqual(
      sessions.map(\.statusLabel), ["1 approval", "Running", "Ready", "Failed", "Waiting"])
    XCTAssertEqual(sessions[1].name, "Untitled session")
    XCTAssertEqual(sessions[2].name, "Untitled session")
  }

  func testSnapshotDecodesMessagesQueuedInputsAndApprovals() throws {
    let json = """
      {"status": "running",
       "pending_inputs": [{"pending_id": "p1", "content": [{"type": "input_text", "text": "queued"}]}],
       "pending_elicitations": [
         {"elicitation_id": "e1", "params": {"mode": "form", "message": "Run it?",
          "requestedSchema": {"type": "object", "properties": {}}}},
         {"elicitation_id": "e2", "params": {"mode": "form", "message": "Pick",
          "requestedSchema": {"properties": {"choice": {"type": "string"}}}}},
         {"elicitation_id": "e3", "params": {"mode": "url", "message": "Sign in"}},
         {"elicitation_id": "e4", "params": {"mode": "form", "message": "Which?",
          "requestedSchema": null, "ask_user_question": {"questions": []}}}
       ]}
      """
    let snapshot = try WatchClient.decoder.decode(WatchSnapshot.self, from: Data(json.utf8))
    XCTAssertEqual(
      snapshot.pendingInputs?.compactMap(\.bubble),
      [
        WatchMessage(id: "pending:p1", isUser: true, text: "queued")
      ])
    XCTAssertEqual(snapshot.pendingElicitations?.map(\.isBinary), [true, false, false, false])

    let items = """
      {"data": [
        {"id": "m2", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "**hi**"}]},
        {"id": "f1", "type": "function_call", "name": "shell"},
        {"id": "h1", "type": "message", "role": "user", "is_meta": true, "content": [{"type": "input_text", "text": "hidden"}]},
        {"id": "x1", "type": "error", "code": "workspace_missing", "message": "workspace gone"},
        {"id": "r1", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>\\nnote\\n</system-reminder>"}]},
        {"id": "r2", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>a</system-reminder>\\n ok"}]},
        {"id": "m1", "type": "message", "role": "user", "content": [{"type": "input_text", "text": " hello "}]},
        {"id": "m1r", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>x</system-reminder>\\nhello"}]}
      ]}
      """
    let page = try WatchClient.decoder.decode(WatchPage<WatchItem>.self, from: Data(items.utf8))
    XCTAssertEqual(
      page.data.bubbles,
      [
        WatchMessage(id: "m2", isUser: false, text: "**hi**"),
        WatchMessage(id: "error:x1", isUser: false, text: "workspace gone"),
        WatchMessage(id: "r2", isUser: true, text: "ok"),
        WatchMessage(id: "m1", isUser: true, text: "hello"),
      ])
  }

  private func cookie(
    _ name: String, domain: String, path: String, secure: Bool = false, expires: Date? = nil
  ) -> HTTPCookie {
    var properties: [HTTPCookiePropertyKey: Any] = [
      .name: name, .value: "1", .domain: domain, .path: path,
    ]
    if secure { properties[.secure] = "TRUE" }
    if let expires { properties[.expires] = expires }
    return HTTPCookie(properties: properties)!
  }
}
