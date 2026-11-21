import Security
import WatchConnectivity
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
    let service = "ai.omnigent.ios.tests.watch.\(UUID().uuidString)"
    let store = WatchKeychain(service: service)
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
    XCTAssertNil(store.load(), "Failed deletion must not restore a signed-out account")
    XCTAssertNil(
      WatchKeychain(service: service).load(), "Sign-out intent survives store recreation")
    XCTAssertEqual(store.save(renewed, update: { _, _ in errSecAuthFailed }), errSecAuthFailed)
    XCTAssertNil(store.load(), "A failed replacement must not undo pending sign-out")

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
    let defaultsName = "WatchCompanionTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: defaultsName))
    defer { defaults.removePersistentDomain(forName: defaultsName) }
    let transport = PendingWatchTransport()
    let bridge = WatchBridge(defaults: defaults, transport: transport)
    bridge.start()
    let olderCookies = PendingWatchCookies()
    let newerCookies = PendingWatchCookies()
    let older = try XCTUnwrap(
      bridge.sync(
        serverURL: URL(string: "https://old.example.com"), workspaceID: "1",
        cookieStore: olderCookies))
    let newer = try XCTUnwrap(
      bridge.sync(
        serverURL: URL(string: "https://entered.example.com/omnigent"),
        workspacePageURL: URL(string: "https://new.example.com/omnigent/c/ignored"),
        workspaceID: "2", cookieName: "new",
        cookieStore: newerCookies))
    newerCookies.cookies.continuation.yield([
      cookie("new", domain: "new.example.com", path: "/"),
      cookie("unrelated", domain: "new.example.com", path: "/"),
    ])
    await newer.value
    olderCookies.cookies.continuation.yield([cookie("old", domain: "old.example.com", path: "/")])
    await older.value
    let data = try XCTUnwrap(bridge.latest?[WatchCredentials.contextKey] as? Data)
    let latest = try JSONDecoder().decode(WatchCredentials.self, from: data)
    XCTAssertEqual(latest.apiURL.absoluteString, "https://new.example.com/omnigent/v1?o=2")
    XCTAssertEqual(latest.cookie, "new=1")
    transport.activationState = .activated
    bridge.flush(now: now)
    let signedIn = try XCTUnwrap(transport.contexts.last)
    XCTAssertEqual(signedIn[WatchCredentials.contextKey] as? Data, data)

    transport.activationState = .notActivated
    let signingOut = try XCTUnwrap(
      bridge.sync(serverURL: URL(string: "https://new.example.com"), cookieStore: newerCookies))
    bridge.signOut()
    newerCookies.cookies.continuation.yield([cookie("new", domain: "new.example.com", path: "/")])
    await signingOut.value
    XCTAssertEqual(bridge.latest?[WatchCredentials.contextKey] as? Data, Data())
    XCTAssertEqual(transport.contexts.count, 1, "Inactive handoffs must remain pending")

    let pendingSignIn = try XCTUnwrap(
      bridge.sync(serverURL: URL(string: "https://new.example.com"), cookieStore: newerCookies))
    newerCookies.cookies.continuation.yield([cookie("new", domain: "new.example.com", path: "/")])
    await pendingSignIn.value
    XCTAssertFalse(try XCTUnwrap(bridge.latest?[WatchCredentials.contextKey] as? Data).isEmpty)
    // Undelivered sign-in exists only in memory; restart must still invalidate the old account.
    let restored = WatchBridge(defaults: defaults, transport: transport)
    restored.start()
    XCTAssertEqual(restored.latest?[WatchCredentials.contextKey] as? Data, Data())
    transport.activationState = .activated
    transport.failDelivery = true
    restored.flush(now: now.addingTimeInterval(-1))
    let failedSignOut = try XCTUnwrap(transport.contexts.last)

    transport.activationState = .notActivated
    let retried = WatchBridge(defaults: defaults, transport: transport)
    retried.start()
    XCTAssertEqual(retried.latest?[WatchCredentials.contextKey] as? Data, Data())
    transport.activationState = .activated
    transport.failDelivery = false
    retried.flush(now: now.addingTimeInterval(-2))
    let signedOut = try XCTUnwrap(transport.contexts.last)
    XCTAssertEqual(signedOut[WatchCredentials.contextKey] as? Data, Data())
    XCTAssertGreaterThan(
      try XCTUnwrap(failedSignOut["sentAt"] as? Date),
      try XCTUnwrap(signedIn["sentAt"] as? Date))
    XCTAssertGreaterThan(
      try XCTUnwrap(signedOut["sentAt"] as? Date),
      try XCTUnwrap(failedSignOut["sentAt"] as? Date))
    let acceptedCount = transport.contexts.count
    let afterAcceptance = WatchBridge(defaults: defaults, transport: transport)
    afterAcceptance.start()
    XCTAssertEqual(
      afterAcceptance.latest?[WatchCredentials.contextKey] as? Data, Data(),
      "The system can accept a sign-out context and time out before delivering it")
    XCTAssertEqual(transport.contexts.count, acceptedCount + 1)
    XCTAssertGreaterThan(
      try XCTUnwrap(transport.contexts.last?["sentAt"] as? Date),
      try XCTUnwrap(signedOut["sentAt"] as? Date))
    let freshSignIn = try XCTUnwrap(
      afterAcceptance.sync(
        serverURL: URL(string: "https://new.example.com"), cookieStore: newerCookies))
    newerCookies.cookies.continuation.yield([cookie("new", domain: "new.example.com", path: "/")])
    await freshSignIn.value
    XCTAssertFalse(
      try XCTUnwrap(transport.contexts.last?[WatchCredentials.contextKey] as? Data).isEmpty)
    XCTAssertNil(WatchBridge(defaults: defaults, transport: transport).latest)
    let sentCount = transport.contexts.count
    bridge.flush()
    XCTAssertEqual(transport.contexts.count, sentCount, "Only the active delegate may send")
  }

  @MainActor func testPollingPreservesApprovalFailureUntilSuccessfulResponse() async throws {
    let snapshot = Data(
      #"{"status":"running","pending_elicitations":[{"elicitation_id":"e1","params":{"message":"Run it?"}}]}"#
        .utf8)
    for (resolvedElsewhere, failureStatus) in [
      (false, 401), (true, 401), (false, 403), (true, 403),
    ] {
      let expectedError =
        failureStatus == 401
        ? WatchError.signedOut.localizedDescription
        : "You don't have permission to perform this action."
      // Only the serial HTTP handler reads or changes this server-side state.
      var promptResolved = false
      let server = try MockHTTPServer(requestHandler: { request in
        switch (request.method, URLComponents(string: request.path)?.path) {
        case ("GET", "/v1/sessions/s1"):
          return (
            200, [:],
            promptResolved
              ? Data(#"{"status":"running","pending_elicitations":[]}"#.utf8) : snapshot
          )
        case ("GET", "/v1/sessions/s1/items"):
          return (200, [:], Data(#"{"data":[]}"#.utf8))
        case ("POST", "/v1/sessions/s1/elicitations/e1/resolve"):
          let body = try? JSONSerialization.jsonObject(with: request.body) as? [String: String]
          if body?["action"] == "accept" { return (failureStatus, [:], Data()) }
          XCTAssertEqual(body?["action"], "decline")
          promptResolved = true
          return (200, [:], Data())
        default:
          XCTFail("Unexpected watch request: \(request.method) \(request.path)")
          return (404, [:], Data())
        }
      })
      let session = try WatchClient.decoder.decode(
        WatchSession.self, from: Data(#"{"id":"s1","status":"running"}"#.utf8))
      let client = WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test"))
      let conversation = WatchConversation(session: session, client: client)
      await conversation.load()
      XCTAssertNil(conversation.error)
      let approval = try XCTUnwrap(conversation.approvals.first)
      await conversation.resolve(approval, accept: true)
      XCTAssertEqual(conversation.error, expectedError)
      await conversation.load()
      XCTAssertEqual(conversation.error, expectedError)
      XCTAssertEqual(
        conversation.pollDelay, .seconds(5), "Approval failures do not slow healthy reads")
      XCTAssertEqual(conversation.approvals.map(\.id), ["e1"])
      if resolvedElsewhere {
        try await client.post(
          "sessions/s1/elicitations/e1/resolve", ["action": "decline"], host: nil)
        await conversation.load()
      } else {
        await conversation.resolve(approval, accept: false)
      }
      XCTAssertNil(conversation.error, "resolvedElsewhere: \(resolvedElsewhere)")
      XCTAssertTrue(conversation.approvals.isEmpty)
    }

    for nativeFromStart in [false, true] {
      var submitted = false
      var confirmed = false
      var readsAfterSubmit = 0
      let server = try MockHTTPServer(requestHandler: { request in
        let url = URLComponents(string: request.path)
        switch (request.method, url?.path) {
        case ("GET", "/v1/sessions/s1"):
          var params: [String: Any] = ["message": "Run it?"]
          if submitted || nativeFromStart {
            let state = submitted ? (readsAfterSubmit == 0 ? "sending" : "unconfirmed") : "ready"
            params["native_delivery"] = ["state": state, "attempt": "a1"]
          }
          if submitted
            && url?.queryItems?.contains(where: { $0.name == "include_live_status" }) != true
          {
            readsAfterSubmit += 1
          }
          let pending: [[String: Any]] =
            confirmed ? [] : [["elicitation_id": "e1", "params": params]]
          return (
            200, [:], try! JSONSerialization.data(withJSONObject: ["pending_elicitations": pending])
          )
        case ("GET", "/v1/sessions/s1/items"):
          return (200, [:], Data(#"{"data":[]}"#.utf8))
        case ("POST", "/v1/sessions/s1/elicitations/e1/resolve"):
          XCTAssertFalse(submitted, "Native-delivery retries must be handled on the phone")
          submitted = true
          return (202, [:], Data())
        case ("POST", "/v1/sessions/s1/events"):
          confirmed = true
          return (202, [:], Data())
        default:
          XCTFail("Unexpected watch request: \(request.method) \(request.path)")
          return (404, [:], Data())
        }
      })
      let client = WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test"))
      let session = try WatchClient.decoder.decode(
        WatchSession.self, from: Data(#"{"id":"s1","status":"running"}"#.utf8))
      let conversation = WatchConversation(session: session, client: client)
      await conversation.load()
      let approval = try XCTUnwrap(conversation.approvals.first)
      XCTAssertEqual(approval.isBinary, !nativeFromStart)
      await conversation.resolve(approval, accept: true)
      if nativeFromStart {
        XCTAssertEqual(conversation.approvals.map(\.id), ["e1"])
        try await client.post(
          "sessions/s1/elicitations/e1/resolve", ["action": "accept"], host: nil)
        await conversation.load()
      }
      for _ in 0..<2 {
        let waiting = try XCTUnwrap(conversation.approvals.first)
        XCTAssertEqual(waiting.id, "e1")
        XCTAssertFalse(
          waiting.isBinary, "Unconfirmed delivery must remain visible with phone-only handling")
        await conversation.resolve(waiting, accept: false)
        await conversation.load()
      }
      try await client.post(
        "sessions/s1/events",
        ["type": "external_elicitation_resolved", "data": ["elicitation_id": "e1"]], host: nil)
      await conversation.load()
      XCTAssertNil(conversation.error)
      XCTAssertTrue(conversation.approvals.isEmpty)
    }
  }

  @MainActor func testConversationPollingBacksOffFailedReads() async throws {
    let lock = NSLock()
    var failureStatus = 0
    let server = try MockHTTPServer(requestHandler: { request in
      let failure = lock.withLock { failureStatus }
      if failure != 0 { return (failure, [:], Data()) }
      let path = URLComponents(string: request.path)?.path
      XCTAssertTrue(path == "/v1/sessions/s1" || path == "/v1/sessions/s1/items")
      return (
        200, [:],
        Data((path == "/v1/sessions/s1/items" ? #"{"data":[]}"# : #"{"status":"idle"}"#).utf8)
      )
    })
    let session = try WatchClient.decoder.decode(
      WatchSession.self, from: Data(#"{"id":"s1","status":"idle"}"#.utf8))
    let conversation = WatchConversation(
      session: session,
      client: WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
    for status in [401, 403, 503] {
      lock.withLock { failureStatus = status }
      for seconds in [10, 20, 40, 80, 120, 120] {
        await conversation.load()
        XCTAssertNotNil(conversation.error)
        XCTAssertEqual(conversation.pollDelay, .seconds(seconds))
      }
      lock.withLock { failureStatus = 0 }
      await conversation.load()
      XCTAssertNil(conversation.error)
      XCTAssertEqual(conversation.pollDelay, .seconds(5))
    }
  }

  @MainActor func testOnlyNewestConversationLoadCanPublishResults() async throws {
    for olderFails in [false, true] {
      let olderRequests = expectation(description: "older snapshot and items are waiting")
      olderRequests.expectedFulfillmentCount = 2
      let releaseOlder = DispatchSemaphore(value: 0)
      defer {
        releaseOlder.signal()
        releaseOlder.signal()
      }
      let lock = NSLock()
      var reads: [String: Int] = [:]
      let server = try MockHTTPServer(
        concurrentConnections: true,
        requestHandler: { request in
          let url = URLComponents(string: request.path)
          if url?.queryItems?.contains(where: { $0.name == "include_live_status" }) == true {
            return (200, [:], Data("{}".utf8))
          }
          let path = url?.path ?? ""
          let isItems = path == "/v1/sessions/s1/items"
          XCTAssertTrue(isItems || path == "/v1/sessions/s1")
          let older = lock.withLock {
            reads[path, default: 0] += 1
            return reads[path] == 1
          }
          if older {
            olderRequests.fulfill()
            XCTAssertEqual(releaseOlder.wait(timeout: .now() + 10), .success)
          }
          let id = older ? "older" : "newer"
          if isItems {
            return (
              200, [:],
              Data(
                "{\"data\":[{\"id\":\"\(id)\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[{\"text\":\"\(id)\"}]}]}"
                  .utf8)
            )
          }
          if older && olderFails { return (500, [:], Data()) }
          let status = older ? "failed" : "idle"
          return (
            200, [:],
            Data(
              "{\"status\":\"\(status)\",\"last_task_error\":{\"message\":\"old failure\"},\"pending_elicitations\":[{\"elicitation_id\":\"\(id)\",\"params\":{\"message\":\"Run it?\"}}]}"
                .utf8)
          )
        })
      let session = try WatchClient.decoder.decode(
        WatchSession.self, from: Data(#"{"id":"s1","status":"running"}"#.utf8))
      let conversation = WatchConversation(
        session: session,
        client: WatchClient(
          credentials: WatchCredentials(
            apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
      let older = Task { await conversation.load() }
      let waiting = await XCTWaiter().fulfillment(of: [olderRequests], timeout: 3)
      XCTAssertEqual(waiting, .completed)
      await conversation.load()
      XCTAssertEqual(conversation.messages.map(\.id), ["newer"])
      XCTAssertFalse(conversation.isLoading)
      releaseOlder.signal()
      releaseOlder.signal()
      await older.value
      XCTAssertEqual(conversation.messages.map(\.id), ["newer"])
      XCTAssertEqual(conversation.approvals.map(\.id), ["newer"])
      XCTAssertEqual(conversation.session.status, "idle")
      XCTAssertNil(conversation.error)
      XCTAssertEqual(conversation.pollDelay, .seconds(5))
    }
  }

  @MainActor func testOnlyNewestSessionListLoadCanPublishResults() async throws {
    for olderFails in [false, true] {
      let olderRequests = expectation(description: "older sessions and projects are waiting")
      olderRequests.expectedFulfillmentCount = 2
      let releaseOlder = DispatchSemaphore(value: 0)
      defer {
        releaseOlder.signal()
        releaseOlder.signal()
      }
      let lock = NSLock()
      var reads: [String: Int] = [:]
      let server = try MockHTTPServer(
        concurrentConnections: true,
        requestHandler: { request in
          let path = URLComponents(string: request.path)?.path ?? ""
          let projects = path == "/v1/sessions/projects"
          XCTAssertTrue(projects || path == "/v1/sessions")
          let older = lock.withLock {
            reads[path, default: 0] += 1
            return reads[path] == 1
          }
          if older {
            olderRequests.fulfill()
            XCTAssertEqual(releaseOlder.wait(timeout: .now() + 10), .success)
          }
          let id = older ? "older" : "newer"
          if projects {
            return (200, [:], Data("[{\"id\":\"\(id)\",\"name\":\"\(id)\"}]".utf8))
          }
          if older && olderFails { return (500, [:], Data()) }
          return (
            200, [:],
            Data(
              "{\"data\":[{\"id\":\"\(id)\",\"status\":\"idle\",\"project_id\":\"\(id)\",\"pending_elicitations_count\":\(older ? 0 : 1)}]}"
                .utf8)
          )
        })
      let list = WatchSessionList(
        client: WatchClient(
          credentials: WatchCredentials(
            apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
      let older = Task { await list.load() }
      let waiting = await XCTWaiter().fulfillment(of: [olderRequests], timeout: 3)
      XCTAssertEqual(waiting, .completed)
      await list.load()
      XCTAssertEqual(list.sessions?.map(\.id), ["newer"])
      releaseOlder.signal()
      releaseOlder.signal()
      await older.value
      XCTAssertEqual(list.sessions?.map(\.id), ["newer"])
      XCTAssertEqual(list.groups.map(\.id), ["project:newer"])
      XCTAssertEqual(list.sessions?.first?.approvals, 1)
      XCTAssertNil(list.error)
    }
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
    XCTAssertEqual(learned?.cookie, "")

    let sessionNames = ["DBAUTH", "__Host-ap_session", "ap_session"]
    let nativeCookies =
      sessionNames.map {
        cookie($0, domain: "omnigent.example.com", path: "/", secure: true)
      } + [cookie("unrelated", domain: "omnigent.example.com", path: "/")]
    for name in sessionNames + ["absent"] {
      let credentials = WatchCredentials(
        serverURL: URL(string: "https://omnigent.example.com/omnigent")!, cookieName: name,
        cookies: nativeCookies)
      if name == "absent" {
        XCTAssertNil(credentials, "A missing known session cookie must clear authentication")
      } else {
        XCTAssertEqual(try XCTUnwrap(credentials).cookie, "\(name)=1")
      }
    }
    for invalid in [
      cookie("session", domain: "omnigent.example.com", path: "/", value: ""),
      cookie("session", domain: "omnigent.example.com", path: "/", expires: now),
      cookie("session", domain: "other.example.com", path: "/"),
      cookie("session", domain: "omnigent.example.com", path: "/admin"),
    ] {
      XCTAssertNil(
        WatchCredentials(
          serverURL: URL(string: "https://omnigent.example.com/omnigent")!, cookieName: "session",
          cookies: [invalid]))
    }

    for enteredPath in ["/omnigent", "/omnigent/c/original"] {
      let landed = try XCTUnwrap(
        WatchCredentials(
          serverURL: URL(string: "https://entered.example.com\(enteredPath)")!,
          workspacePageURL: URL(
            string: "https://landed.example.com:8443/omnigent/c/session?o=42&next=ignored#message")!,
          cookieName: "DBAUTH",
          cookies: [
            cookie("DBAUTH", domain: "entered.example.com", path: "/"),
            cookie("DBAUTH", domain: "landed.example.com", path: "/omnigent", secure: true),
          ]))
      XCTAssertEqual(
        landed.apiURL.absoluteString, "https://landed.example.com:8443/omnigent/v1?o=42")
      XCTAssertEqual(landed.cookie, "DBAUTH=1")
    }
    for unsafeLanding in ["http://landed.example.com", "https://user@landed.example.com"] {
      XCTAssertNil(
        WatchCredentials(
          serverURL: URL(string: "https://entered.example.com")!,
          workspacePageURL: URL(string: unsafeLanding), cookies: []))
    }
  }

  @MainActor func testClientAddressesTheAPIWithCookieAndHostKey() async throws {
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

    let cases: [(metadata: String, host: String?, succeeds: Bool)] = [
      (#"{"parent_session_id":"parent"}"#, "inherited-host", true),
      (#"{"host_id":"child-host","parent_session_id":"parent"}"#, "child-host", true),
      (#"{"labels":{"omnigent.sandbox.requested_host_id":"sandbox-host"}}"#, "sandbox-host", true),
      (#"{"host_id":null,"parent_session_id":null}"#, nil, true),
      (#"{"parent_session_id":"child"}"#, nil, false),
      ("missing", nil, false),
    ]
    for testCase in cases {
      var resolved = false
      var responseCount = 0
      let pending =
        #"[{"elicitation_id":"e1","params":{"message":"Run it?","target_session_id":"child"}}]"#
      let server = try MockHTTPServer(requestHandler: { request in
        switch (request.method, URLComponents(string: request.path)?.path) {
        case ("GET", "/v1/sessions/child"):
          if testCase.metadata == "missing" { return (404, [:], Data()) }
          return (200, [:], Data(testCase.metadata.utf8))
        case ("GET", "/v1/sessions/parent"):
          return (200, [:], Data(#"{"parent_session_id":"grandparent"}"#.utf8))
        case ("GET", "/v1/sessions/grandparent"):
          return (200, [:], Data(#"{"host_id":"inherited-host"}"#.utf8))
        case ("POST", "/v1/sessions/child/elicitations/e1/resolve"):
          responseCount += 1
          XCTAssertEqual(responseCount, 1, "Only the first answer may be submitted")
          XCTAssertTrue(testCase.succeeds, "Unresolved routing must not POST an approval")
          XCTAssertEqual(request.headers["x-databricks-omnigent-slice-key"], testCase.host)
          XCTAssertEqual(request.headers["cookie"], "session=test")
          resolved = true
          return (200, [:], Data())
        case ("GET", "/v1/sessions/s1"):
          return (200, [:], Data("{\"pending_elicitations\":\(resolved ? "[]" : pending)}".utf8))
        case ("GET", "/v1/sessions/s1/items"):
          return (200, [:], Data(#"{"data":[]}"#.utf8))
        default:
          XCTFail("Unexpected watch request: \(request.method) \(request.path)")
          return (404, [:], Data())
        }
      })
      let session = try WatchClient.decoder.decode(
        WatchSession.self,
        from: Data(#"{"id":"s1","status":"running","host_id":"displayed-host"}"#.utf8))
      let conversation = WatchConversation(
        session: session,
        client: WatchClient(
          credentials: WatchCredentials(
            apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
      await conversation.load()
      let approval = try XCTUnwrap(conversation.approvals.first)
      let first = Task { await conversation.resolve(approval, accept: true) }
      let conflicting = Task { await conversation.resolve(approval, accept: false) }
      await first.value
      await conflicting.value
      if testCase.succeeds { await conversation.resolve(approval, accept: false) }
      XCTAssertEqual(conversation.error == nil, testCase.succeeds, testCase.metadata)
      XCTAssertEqual(conversation.approvals.isEmpty, testCase.succeeds, testCase.metadata)
    }

    var bound = false
    let server = try MockHTTPServer(requestHandler: { request in
      let url = URLComponents(string: request.path)
      let host = request.headers["x-databricks-omnigent-slice-key"]
      switch url?.path {
      case "/v1/sessions/s1":
        if url?.queryItems?.contains(where: { $0.name == "include_live_status" }) == true {
          return (200, [:], Data((bound ? #"{"host_id":"late-host"}"# : "{}").utf8))
        }
        if bound { XCTAssertEqual(host, "late-host", "Later polls must use the assigned host") }
        bound = true
        let pending =
          host == "late-host"
          ? #"[{"elicitation_id":"late","params":{"message":"Run it?"}}]"# : "[]"
        return (
          200, [:], Data("{\"host_id\":\"late-host\",\"pending_elicitations\":\(pending)}".utf8)
        )
      case "/v1/sessions/s1/items":
        XCTAssertNil(host, "Durable items do not depend on the runner's route")
        return (200, [:], Data(#"{"data":[]}"#.utf8))
      default:
        XCTFail("Unexpected watch request: \(request.path)")
        return (404, [:], Data())
      }
    })
    let initiallyHostless = try WatchClient.decoder.decode(
      WatchSession.self, from: Data(#"{"id":"s1","status":"running"}"#.utf8))
    let conversation = WatchConversation(
      session: initiallyHostless,
      client: WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(server.port)/v1")!, cookie: "session=test")))
    await conversation.load()
    XCTAssertTrue(conversation.approvals.isEmpty)
    await conversation.load()
    XCTAssertNil(conversation.error)
    XCTAssertEqual(conversation.approvals.map(\.id), ["late"])

    var actualKey: String?
    var probeStatus = 502
    var storedText = "Stored before opening"
    var pendingID: String? = "keyless"
    var probeKeys: [String?] = []
    var snapshotKeys: [String?] = []
    var responseKeys: [String?] = []
    let keylessServer = try MockHTTPServer(requestHandler: { request in
      let url = URLComponents(string: request.path)
      let key = request.headers["x-databricks-omnigent-slice-key"]
      XCTAssertEqual(request.headers["cookie"], "session=test")
      switch (request.method, url?.path) {
      case ("GET", "/v1/sessions/s1"):
        let metadata = #""host_id":"moving-host","runner_id":"runner""#
        if url?.queryItems?.contains(where: { $0.name == "include_live_status" }) == true {
          return (200, [:], Data("{\(metadata)}".utf8))
        }
        snapshotKeys.append(key)
        let pending =
          key == actualKey && pendingID != nil
          ? #"[{"elicitation_id":"\#(pendingID!)","params":{"message":"Run it?"}}]"# : "[]"
        return (200, [:], Data("{\(metadata),\"pending_elicitations\":\(pending)}".utf8))
      case ("GET", "/v1/sessions/s1/resources/terminals"):
        probeKeys.append(key)
        XCTAssertEqual(url?.queryItems?.first { $0.name == "limit" }?.value, "1")
        if key != actualKey {
          return (400, [:], Data(#"{"error":{"code":"wrong_replica"}}"#.utf8))
        }
        if probeStatus != 200 {
          let failure =
            probeStatus == 503
            ? #"{"error":{"code":"runner_unavailable"}}"#
            : #"{"detail":"runner resource endpoint unavailable"}"#
          return (probeStatus, [:], Data(failure.utf8))
        }
        return (200, [:], Data(#"{"data":[]}"#.utf8))
      case ("GET", "/v1/sessions/s1/items"):
        XCTAssertNil(key, "Stored messages must remain readable without verified affinity")
        return (
          200, [:],
          Data(
            #"{"data":[{"id":"stored","type":"message","role":"assistant","content":[{"text":"\#(storedText)"}]}]}"#
              .utf8)
        )
      case ("POST", let path) where path?.hasPrefix("/v1/sessions/s1/elicitations/") == true:
        responseKeys.append(key)
        XCTAssertEqual(probeStatus, 200, "A failed routing probe must never permit an approval")
        XCTAssertNil(key, "Approvals must use the proven keyless route")
        pendingID = nil
        return (200, [:], Data())
      default:
        XCTFail("Unexpected watch request: \(request.method) \(request.path)")
        return (404, [:], Data())
      }
    })
    let routed = WatchConversation(
      session: initiallyHostless,
      client: WatchClient(
        credentials: WatchCredentials(
          apiURL: URL(string: "http://127.0.0.1:\(keylessServer.port)/v1")!,
          cookie: "session=test")))
    await routed.load()
    XCTAssertEqual(routed.messages.map(\.text), [storedText])
    XCTAssertEqual(routed.error, WatchError.status(502).localizedDescription)
    XCTAssertTrue(routed.approvals.isEmpty)
    XCTAssertTrue(snapshotKeys.isEmpty, "Unverified routing must not fetch live approvals")
    probeStatus = 200
    probeKeys.removeAll()
    await routed.load()
    XCTAssertNil(routed.error)
    XCTAssertEqual(routed.approvals.map(\.id), ["keyless"])
    XCTAssertEqual(probeKeys, ["moving-host", nil])
    XCTAssertEqual(snapshotKeys, [nil], "An empty keyed HTTP 200 is not a routing proof")
    let keylessApproval = try XCTUnwrap(routed.approvals.first)
    await routed.resolve(keylessApproval, accept: true)
    XCTAssertEqual(responseKeys, [nil])
    XCTAssertTrue(routed.approvals.isEmpty)
    pendingID = "later"
    await routed.load()
    XCTAssertEqual(routed.approvals.map(\.id), ["later"])
    XCTAssertEqual(probeKeys, ["moving-host", nil, nil, nil, nil])
    XCTAssertTrue(snapshotKeys.allSatisfy { $0 == nil })

    // A host can reconnect keyed; do not let learned keyless affinity strand it.
    actualKey = "moving-host"
    await routed.load()
    XCTAssertNil(routed.error)
    XCTAssertEqual(Array(probeKeys.suffix(2)), [nil, "moving-host"])
    XCTAssertEqual(snapshotKeys.last!, "moving-host")
    await routed.load()
    XCTAssertEqual(probeKeys.last!, "moving-host")

    // Failed routing still refreshes stored messages but withholds live approvals.
    actualKey = nil
    probeStatus = 503
    storedText = "Committed during outage"
    let priorSnapshotCount = snapshotKeys.count
    await routed.load()
    XCTAssertNotNil(routed.error)
    XCTAssertEqual(snapshotKeys.count, priorSnapshotCount)
    XCTAssertTrue(routed.approvals.isEmpty)
    XCTAssertEqual(routed.messages.map(\.text), [storedText])
    probeStatus = 200
    await routed.load()
    XCTAssertNil(routed.error)
    XCTAssertEqual(Array(probeKeys.suffix(2)), ["moving-host", nil])
    XCTAssertNil(snapshotKeys.last!)

    let laterApproval = try XCTUnwrap(routed.approvals.first)
    for status in [502, 503] {
      probeStatus = status
      storedText = "Committed during HTTP \(status)"
      await routed.load()
      XCTAssertNotNil(routed.error)
      XCTAssertTrue(routed.approvals.isEmpty)
      XCTAssertEqual(routed.messages.map(\.text), [storedText])
      XCTAssertEqual(snapshotKeys.count, priorSnapshotCount + 1)
      await routed.resolve(laterApproval, accept: true)
      XCTAssertEqual(responseKeys.count, 1, "Even a previously displayed approval must fail closed")
    }
    probeStatus = 200
    await routed.load()
    XCTAssertEqual(routed.approvals.map(\.id), ["later"])
    await routed.resolve(laterApproval, accept: false)
    XCTAssertNil(routed.error)
    XCTAssertTrue(routed.approvals.isEmpty)
    XCTAssertEqual(responseKeys.count, 2, "Approval resumes only after routing recovers")
  }

  @MainActor func testToolHeavyTranscriptBackfillsVisibleMessages() async throws {
    let tools = (1...35).reversed().map { ["id": "tool-\($0)", "type": "function_call"] }
    let olderItems: [[String: Any]] =
      Array(tools.suffix(5)) + [
        ["id": "failure", "type": "error", "message": "Task failed"],
        ["id": "reply", "type": "message", "role": "assistant", "content": [["text": "Working"]]],
        [
          "id": "question", "type": "message", "role": "user",
          "content": [["text": "Please check"]],
        ],
      ]
    let newestPage = try JSONSerialization.data(withJSONObject: [
      "data": Array(tools.prefix(30)), "last_id": "tool-6", "has_more": true,
    ])
    let olderPage = try JSONSerialization.data(withJSONObject: [
      "data": olderItems, "last_id": "question", "has_more": false,
    ])
    let server = try MockHTTPServer(requestHandler: { request in
      let url = URLComponents(string: request.path)
      switch url?.path {
      case "/v1/sessions/s1": return (200, [:], Data(#"{"status":"running"}"#.utf8))
      case "/v1/sessions/s1/items":
        let cursor = url?.queryItems?.first { $0.name == "after" }?.value
        XCTAssertTrue(cursor == nil || cursor == "tool-6")
        return (200, [:], cursor == nil ? newestPage : olderPage)
      default:
        XCTFail("Unexpected watch request: \(request.path)")
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
    XCTAssertEqual(conversation.messages.map(\.id), ["question", "reply", "error:failure"])
    XCTAssertEqual(conversation.messages.map(\.text), ["Please check", "Working", "Task failed"])
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
        return (400, [:], Data(#"{"error":{"code":"wrong_replica"}}"#.utf8))
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
        {"id": "e", "status": "waiting"},
        {"id": "f", "status": "idle", "project_id": "p2"}
      ]}
      """
    let sessions = try WatchClient.decoder.decode(
      WatchPage<WatchSession>.self, from: Data(json.utf8)
    ).data
    let groups = sessions.grouped(projects: [
      WatchProject(id: "p1", name: "Docs"), WatchProject(id: "p2", name: "Docs"),
      WatchProject(id: nil, name: "Legacy"),
    ])
    XCTAssertEqual(groups.map(\.title), ["Docs", "Docs", "Sessions"])
    XCTAssertEqual(groups.map(\.id), ["project:p1", "project:p2", "sessions"])
    XCTAssertEqual(groups.map { $0.sessions.map(\.id) }, [["c"], ["f"], ["a", "b", "d", "e"]])
    XCTAssertEqual(
      sessions.map(\.statusLabel), ["1 approval", "Running", "Ready", "Failed", "Waiting", "Ready"])
    XCTAssertEqual(sessions[1].name, "Untitled session")
    XCTAssertEqual(sessions[2].name, "Untitled session")

    let cases: [(fields: String, needsResponse: Bool, active: Bool, label: String)] = [
      (#""status":"running","blocked_on":"permission_prompt""#, true, false, "Needs response"),
      (#""status":"idle","background_work_running":true"#, false, true, "Background work"),
      (#""status":"failed","background_work_running":true"#, false, false, "Failed"),
      (
        #""status":"running","blocked_on":"permission_prompt","pending_elicitations_count":1"#,
        true, false, "1 approval"
      ),
      (#""status":"idle","blocked_on":"permission_prompt""#, false, false, "Ready"),
      (#""status":"running","blocked_on":"""#, false, true, "Running"),
    ]
    for testCase in cases {
      let session = try WatchClient.decoder.decode(
        WatchSession.self, from: Data("{\"id\":\"state\",\(testCase.fields)}".utf8))
      XCTAssertEqual(session.needsResponse, testCase.needsResponse, testCase.fields)
      XCTAssertEqual(session.isActive, testCase.active, testCase.fields)
      XCTAssertEqual(session.statusLabel, testCase.label, testCase.fields)
    }
  }

  func testSnapshotDecodesMessagesQueuedInputsAndApprovals() throws {
    let json = """
      {"status": "running",
       "pending_inputs": [
         {"pending_id": "p1", "content": [{"type": "input_text", "text": "queued"}]},
         {"pending_id": "p2", "content": [{"type": "input_text", "text": "hello"}]},
         {"pending_id": "p3", "content": [{"type": "input_text", "text": "ok"}]},
         {"pending_id": "p4", "content": [{"type": "input_text", "text": "<system-reminder>queued</system-reminder>"}]}
       ],
       "pending_elicitations": [
         {"elicitation_id": "e1", "params": {"mode": "form", "message": "Run it?",
          "requestedSchema": {"type": "object", "properties": {}}}},
         {"elicitation_id": "e2", "params": {"mode": "form", "message": "Pick",
          "requestedSchema": {"properties": {"choice": {"type": "string"}}}}},
         {"elicitation_id": "e3", "params": {"mode": "url", "message": "Sign in"}},
         {"elicitation_id": "e4", "params": {"mode": "form", "message": "Which?",
          "requestedSchema": null, "ask_user_question": {"questions": []}}},
         {"elicitation_id": "e5", "params": {}},
         {"elicitation_id": "e6", "params": {"message": null}}
       ]}
      """
    let snapshot = try WatchClient.decoder.decode(WatchSnapshot.self, from: Data(json.utf8))
    XCTAssertEqual(
      snapshot.pendingInputs?.compactMap(\.bubble),
      [
        WatchMessage(id: "pending:p1", isUser: true, text: "queued"),
        WatchMessage(id: "pending:p2", isUser: true, text: "hello"),
        WatchMessage(id: "pending:p3", isUser: true, text: "ok"),
        WatchMessage(
          id: "pending:p4", isUser: true, text: "<system-reminder>queued</system-reminder>"),
      ])
    XCTAssertEqual(
      snapshot.pendingElicitations?.map(\.isBinary), [true, false, false, false, true, true])
    XCTAssertEqual(
      snapshot.pendingElicitations?.map(\.message),
      ["Run it?", "Pick", "Sign in", "Which?", "Approval required", "Approval required"])

    let items = """
      {"data": [
        {"id": "m2", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "**hi**"}]},
        {"id": "f1", "type": "function_call", "name": "shell"},
        {"id": "h1", "type": "message", "role": "user", "is_meta": true, "content": [{"type": "input_text", "text": "hidden"}]},
        {"id": "x1", "type": "error", "code": "workspace_missing", "message": "workspace gone"},
        {"id": "r1", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>\\nnote\\n</system-reminder>"}]},
        {"id": "r2", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>a</system-reminder>\\n ok"}]},
        {"id": "m1", "type": "message", "role": "user", "content": [{"type": "input_text", "text": " hello "}]},
        {"id": "m1r", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<system-reminder>x</system-reminder>\\nhello"}]},
        {"id": "authored", "type": "message", "role": "user", "user_authored": true, "content": [{"text": "<system-reminder>authored</system-reminder>"}]},
        {"id": "attributed", "type": "message", "role": "user", "created_by": "user@example.test", "content": [{"text": "<system-reminder>attributed</system-reminder>"}]},
        {"id": "assistant", "type": "message", "role": "assistant", "content": [{"text": "<system-reminder>quoted</system-reminder>"}]},
        {"id": "repeat1", "type": "message", "role": "user", "user_authored": true, "content": [{"text": "same"}]},
        {"id": "repeat2", "type": "message", "role": "user", "user_authored": true, "content": [{"text": "same"}]},
        {"id": "bare1", "type": "message", "role": "user", "content": [{"text": "legacy"}]},
        {"id": "bare2", "type": "message", "role": "user", "content": [{"text": "legacy"}]}
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
        WatchMessage(
          id: "authored", isUser: true, text: "<system-reminder>authored</system-reminder>"),
        WatchMessage(
          id: "attributed", isUser: true, text: "<system-reminder>attributed</system-reminder>"),
        WatchMessage(
          id: "assistant", isUser: false, text: "<system-reminder>quoted</system-reminder>"),
        WatchMessage(id: "repeat1", isUser: true, text: "same"),
        WatchMessage(id: "repeat2", isUser: true, text: "same"),
        WatchMessage(id: "bare1", isUser: true, text: "legacy"),
        WatchMessage(id: "bare2", isUser: true, text: "legacy"),
      ])
    // Both an older repeat and a repeat of the last user message remain visible while queued.
    XCTAssertEqual(
      snapshot.messages(items: Array(page.data.reversed())),
      page.data.bubbles + (snapshot.pendingInputs ?? []).compactMap(\.bubble))
  }

  private func cookie(
    _ name: String, domain: String, path: String, secure: Bool = false, expires: Date? = nil,
    value: String = "1"
  ) -> HTTPCookie {
    var properties: [HTTPCookiePropertyKey: Any] = [
      .name: name, .value: value, .domain: domain, .path: path,
    ]
    if secure { properties[.secure] = "TRUE" }
    if let expires { properties[.expires] = expires }
    return HTTPCookie(properties: properties)!
  }
}

@MainActor
final class PendingWatchTransport: WatchSessionTransport {
  weak var delegate: (any WCSessionDelegate)?
  var activationState = WCSessionActivationState.notActivated
  var isWatchAppInstalled = true
  var failDelivery = false
  var contexts: [[String: Any]] = []

  func activate() {}

  func updateApplicationContext(_ applicationContext: [String: Any]) throws {
    contexts.append(applicationContext)
    if failDelivery { throw URLError(.notConnectedToInternet) }
  }
}

@MainActor
final class PendingWatchCookies: WatchCookieStore {
  let cookies = AsyncStream<[HTTPCookie]>.makeStream()

  func allCookies() async -> [HTTPCookie] {
    var iterator = cookies.stream.makeAsyncIterator()
    return await iterator.next() ?? []
  }
}
