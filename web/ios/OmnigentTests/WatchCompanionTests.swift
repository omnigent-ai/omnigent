import Security
import XCTest

@testable import Omnigent

final class WatchCompanionTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_000)

  func testCredentialContextsCannotUndoNewerSignInOrSignOut() throws {
    let credentials = WatchCredentials(
      apiURL: URL(string: "https://omnigent.example.com/v1")!, cookie: "session=1")
    let data = try JSONEncoder().encode(credentials)
    var state = WatchCredentialState()
    XCTAssertTrue(state.apply(data, sentAt: now))
    XCTAssertEqual(state.credentials, credentials)

    let signOut = now.addingTimeInterval(1)
    XCTAssertTrue(state.apply(Data(), sentAt: signOut))
    XCTAssertNil(state.credentials)
    for stale in [now, signOut] {
      XCTAssertFalse(state.apply(data, sentAt: stale))
      XCTAssertNil(state.credentials)
    }

    XCTAssertTrue(state.apply(data, sentAt: now.addingTimeInterval(2)))
    XCTAssertFalse(state.apply(Data(), sentAt: signOut))
    XCTAssertEqual(state.credentials, credentials)
  }

  func testMalformedCredentialContextClearsSavedCredentials() {
    let credentials = WatchCredentials(
      apiURL: URL(string: "https://omnigent.example.com/v1")!, cookie: "session=1")
    var state = WatchCredentialState(credentials: credentials)
    XCTAssertTrue(state.apply(Data("invalid".utf8), sentAt: now))
    XCTAssertNil(state.credentials)
  }

  func testWatchKeychainRoundTripAndExplicitSignOut() throws {
    let service = "ai.omnigent.ios.tests.watch.\(UUID().uuidString)"
    let store = WatchKeychain(service: service)
    defer { _ = store.save(nil) }
    XCTAssertNil(store.load())
    for value in ["first", "renewed"] {
      let credentials = WatchCredentials(
        apiURL: URL(string: "https://omnigent.example.com/v1")!, cookie: "session=\(value)")
      XCTAssertEqual(store.save(credentials), errSecSuccess)
      XCTAssertEqual(WatchKeychain(service: service).load(), credentials)
    }
    let query: [String: Any] = [
      kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
      kSecAttrAccount as String: WatchCredentials.contextKey, kSecReturnAttributes as String: true,
    ]
    var result: CFTypeRef?
    XCTAssertEqual(SecItemCopyMatching(query as CFDictionary, &result), errSecSuccess)
    let attributes = try XCTUnwrap(result as? [String: Any])
    XCTAssertEqual(
      attributes[kSecAttrAccessible as String] as? String,
      kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String)
    XCTAssertFalse((attributes[kSecAttrSynchronizable as String] as? Bool) ?? false)
    XCTAssertEqual(store.save(nil), errSecSuccess)
    XCTAssertEqual(store.save(nil), errSecSuccess)
    XCTAssertNil(store.load())
  }

  func testWatchKeychainWriteFailurePreservesSavedCredentials() {
    let store = WatchKeychain(service: "ai.omnigent.ios.tests.watch.\(UUID().uuidString)")
    defer { _ = store.save(nil) }
    let saved = WatchCredentials(
      apiURL: URL(string: "https://omnigent.example.com/v1")!, cookie: "session=saved")
    let renewed = WatchCredentials(apiURL: saved.apiURL, cookie: "session=renewed")
    XCTAssertEqual(store.save(saved), errSecSuccess)
    for failure in [errSecInteractionNotAllowed, errSecAuthFailed] {
      XCTAssertEqual(
        store.save(
          renewed, update: { _, _ in failure },
          add: { _, _ in
            XCTFail("An update failure must not replace the item")
            return failure
          },
          delete: { _ in
            XCTFail("Renewal must not delete saved credentials")
            return failure
          }),
        failure)
      XCTAssertEqual(store.load(), saved)
    }
    XCTAssertEqual(
      store.save(nil, delete: { _ in errSecInteractionNotAllowed }), errSecInteractionNotAllowed)
    XCTAssertEqual(store.load(), saved)

    var updates = 0
    XCTAssertEqual(
      store.save(
        renewed,
        update: { query, attributes in
          updates += 1
          return updates == 1 ? errSecItemNotFound : SecItemUpdate(query, attributes)
        }, add: { _, _ in errSecDuplicateItem }),
      errSecSuccess)
    XCTAssertEqual(updates, 2)
    XCTAssertEqual(store.load(), renewed)
  }

  @MainActor func testOnlyNewestPhoneSyncCanPublishCredentials() async throws {
    let bridge = WatchBridge()
    let olderCookies = PendingWatchCookies()
    let newerCookies = PendingWatchCookies()
    let older = try XCTUnwrap(
      bridge.sync(
        serverURL: URL(string: "https://old.example.com"), workspaceID: "1",
        cookieStore: olderCookies))
    let newer = try XCTUnwrap(
      bridge.sync(
        serverURL: URL(string: "https://new.example.com"), workspaceID: "2",
        cookieStore: newerCookies))
    newerCookies.cookies.continuation.yield([cookie("new", domain: "new.example.com", path: "/")])
    await newer.value
    olderCookies.cookies.continuation.yield([cookie("old", domain: "old.example.com", path: "/")])
    await older.value
    let data = try XCTUnwrap(bridge.latest?[WatchCredentials.contextKey] as? Data)
    let latest = try JSONDecoder().decode(WatchCredentials.self, from: data)
    XCTAssertEqual(latest.apiURL.absoluteString, "https://new.example.com/v1?o=2")
    XCTAssertEqual(latest.cookie, "new=1")

    let signingOut = try XCTUnwrap(
      bridge.sync(serverURL: URL(string: "https://new.example.com"), cookieStore: newerCookies))
    bridge.signOut()
    newerCookies.cookies.continuation.yield([cookie("new", domain: "new.example.com", path: "/")])
    await signingOut.value
    XCTAssertEqual(bridge.latest?[WatchCredentials.contextKey] as? Data, Data())
  }

  @MainActor func testPollingPreservesApprovalFailureUntilSuccessfulResponse() async throws {
    let snapshot = Data(
      #"{"status":"running","pending_elicitations":[{"elicitation_id":"e1","params":{"message":"Run it?"}}]}"#
        .utf8)
    let server = try MockHTTPServer(requestHandler: { request in
      switch (request.method, URLComponents(string: request.path)?.path) {
      case ("GET", "/v1/sessions/s1"):
        return (200, [:], snapshot)
      case ("GET", "/v1/sessions/s1/items"):
        return (200, [:], Data(#"{"data":[]}"#.utf8))
      case ("POST", "/v1/sessions/s1/elicitations/e1/resolve"):
        let body = try? JSONSerialization.jsonObject(with: request.body) as? [String: String]
        return (body?["action"] == "accept" ? 401 : 200, [:], Data())
      default:
        XCTFail("Unexpected watch request: \(request.method) \(request.path)")
        return (404, [:], Data())
      }
    })
    let session = try WatchClient.decoder.decode(
      WatchSession.self, from: Data(#"{"id":"s1","status":"running"}"#.utf8))
    let conversation = WatchConversation(
      session: session,
      client: WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
    await conversation.load()
    XCTAssertNil(conversation.error)
    let approval = try XCTUnwrap(conversation.approvals.first)
    await conversation.resolve(approval, accept: true)
    XCTAssertEqual(conversation.error, WatchError.signedOut.localizedDescription)
    await conversation.load()
    XCTAssertEqual(conversation.error, WatchError.signedOut.localizedDescription)
    XCTAssertEqual(conversation.approvals.map(\.id), ["e1"])
    await conversation.resolve(approval, accept: false)
    XCTAssertNil(conversation.error)
    XCTAssertTrue(conversation.approvals.isEmpty)
  }

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

  func testClientRejectsRedirectsWithoutForwardingCredentials() async throws {
    let destination = try MockHTTPServer(requestHandler: { _ in
      XCTFail("API redirects must not forward requests or session cookies")
      return (200, [:], Data(#"{"data":[]}"#.utf8))
    })
    for status in [301, 302, 303, 307, 308] {
      let source = try MockHTTPServer(requestHandler: { request in
        XCTAssertEqual(request.headers["cookie"], "session=test")
        return (status, ["location": "http://localhost:\(destination.port)/sessions"], Data())
      })
      let client = WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(source.port)/v1")!, cookie: "session=test"))
      for method in ["GET", "POST"] {
        do {
          if method == "GET" {
            let _: WatchPage<WatchSession> = try await client.get("sessions")
          } else {
            try await client.post(
              "sessions/session/elicitations/prompt/resolve", ["action": "accept"], host: nil)
          }
          XCTFail("Expected \(method) to reject HTTP \(status)")
        } catch {
          XCTAssertEqual(error as? WatchError, .status(status))
        }
      }
    }
  }

  func testClientRejectsRedirectsAfterWrongReplicaRetry() async throws {
    let originalRequest = expectation(description: "request with the stale host key")
    let retriedRequest = expectation(description: "retry without the host key")
    let server = try MockHTTPServer(requestHandler: { request in
      guard URLComponents(string: request.path)?.path == "/v1/sessions" else {
        XCTFail("The retry must not follow a redirect outside the cookie's path")
        return (200, [:], Data(#"{"data":[]}"#.utf8))
      }
      XCTAssertEqual(request.headers["cookie"], "session=test")
      if request.headers["x-databricks-omnigent-slice-key"] != nil {
        originalRequest.fulfill()
        return (400, [:], Data(#"{"error":"wrong_replica"}"#.utf8))
      }
      retriedRequest.fulfill()
      return (307, ["location": "/outside-cookie-path"], Data())
    })
    let client = WatchClient(
      credentials: WatchCredentials(
        apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test"))
    do {
      let _: WatchPage<WatchSession> = try await client.get("sessions", host: "stale-host")
      XCTFail("Expected the retry to reject the redirect outside the cookie's path")
    } catch {
      XCTAssertEqual(error as? WatchError, .status(307))
    }
    await fulfillment(of: [originalRequest, retriedRequest], timeout: 2)
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
       "pending_inputs": [
         {"pending_id": "p1", "content": [{"type": "input_text", "text": "queued"}]},
         {"pending_id": "p2", "content": [{"type": "input_text", "text": "hello"}]},
         {"pending_id": "p3", "content": [{"type": "input_text", "text": "ok"}]}
       ],
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
        WatchMessage(id: "pending:p1", isUser: true, text: "queued"),
        WatchMessage(id: "pending:p2", isUser: true, text: "hello"),
        WatchMessage(id: "pending:p3", isUser: true, text: "ok"),
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
    // Both an older repeat and a repeat of the last user message remain visible while queued.
    XCTAssertEqual(
      snapshot.messages(items: Array(page.data.reversed())),
      page.data.bubbles + (snapshot.pendingInputs ?? []).compactMap(\.bubble))
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

@MainActor
private final class PendingWatchCookies: WatchCookieStore {
  let cookies = AsyncStream<[HTTPCookie]>.makeStream()

  func allCookies() async -> [HTTPCookie] {
    var iterator = cookies.stream.makeAsyncIterator()
    return await iterator.next() ?? []
  }
}
