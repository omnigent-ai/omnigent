import XCTest

@testable import Omnigent

final class WatchCompanionTests: XCTestCase {
  func testGroupsDecodedSessionsByStateAndProject() throws {
    let page: WatchSessionPage = try decode(
      """
      {"data":[
        {"id":"approval","title":"Review","agent_name":"agent","host_id":"host","project_id":"p1","status":"waiting","pending_elicitations_count":1},
        {"id":"active","title":null,"agent_name":"Coder","host_id":null,"project_id":null,"status":"launching","pending_elicitations_count":0},
        {"id":"project","title":"Done","agent_name":"agent","host_id":null,"project_id":"p1","status":"idle","pending_elicitations_count":0},
        {"id":"recent","title":"Old","agent_name":"agent","host_id":null,"project_id":"missing","status":"idle","pending_elicitations_count":0}
      ]}
      """
    )
    let projects: [WatchProjectSummary] = try decode(
      #"[{"id":"p1","name":"Watch"},{"id":null,"name":"Legacy"}]"#
    )
    let grouping = WatchSessionGrouping(
      sessions: page.data,
      projects: projects
    )

    XCTAssertEqual(page.data[1].displayTitle, "Coder")
    XCTAssertEqual(grouping.needsAttention.map(\.id), ["approval"])
    XCTAssertEqual(grouping.active.map(\.id), ["active"])
    XCTAssertEqual(grouping.projects.first?.sessions.map(\.id), ["project"])
    XCTAssertEqual(grouping.recent.map(\.id), ["recent"])
    XCTAssertEqual(grouping.needsAttention.first?.projectName, "Watch")
  }

  func testDecodesSnapshotMessagesAndApprovalCapability() throws {
    let snapshot: WatchSessionSnapshot = try decode(
      """
      {
        "status":"failed",
        "last_task_error":{"code":"runner_disconnected","message":"Runner disconnected unexpectedly."},
        "pending_inputs":[{"pending_id":"pending_1","content":[{"type":"input_text","text":"Reply"}]}],
        "pending_elicitations":[
          {"elicitation_id":"binary","params":{"message":"Run?","mode":"form","requestedSchema":{"properties":{}}}},
          {"elicitation_id":"structured","params":{"message":"Choose","mode":"form","requestedSchema":{"properties":{"answer":{"type":"string"}}}}}
        ]
      }
      """
    )
    let page: WatchSessionItemPage = try decode(
      """
      {"data":[
        {"id":"assistant","type":"message","role":"assistant","content":[{"type":"output_text","text":"Answer"}]},
        {"id":"event","type":"resource_event","role":null,"content":null},
        {"id":"user","type":"message","role":"user","content":[{"type":"input_text","text":"Question"}]}
      ]}
      """
    )

    XCTAssertEqual(snapshot.pendingInputs.first?.transcriptMessage?.text, "Reply")
    XCTAssertEqual(snapshot.lastTaskError?.message, "Runner disconnected unexpectedly.")
    XCTAssertTrue(snapshot.pendingElicitations[0].params.canResolveOnWatch)
    XCTAssertFalse(snapshot.pendingElicitations[1].params.canResolveOnWatch)
    XCTAssertEqual(page.messages.map(\.text), ["Question", "Answer"])
  }

  func testReconcilesPendingInputAndFindsItsReply() {
    let pending = WatchPendingInput(id: "local", text: "Run this")
    let committed = [
      WatchTranscriptMessage(id: "old", role: "user", text: "Earlier"),
      WatchTranscriptMessage(id: "new", role: "user", text: "Run this"),
      WatchTranscriptMessage(id: "answer", role: "assistant", text: "Done"),
    ]

    XCTAssertTrue(
      WatchTranscript.reconcile(
        snapshot: [],
        provisional: pending,
        committed: committed,
        baselineUserIDs: ["old"]
      ).isEmpty
    )
    XCTAssertTrue(
      WatchTranscript.hasReply(
        to: "Run this",
        committed: committed,
        baselineUserIDs: ["old"]
      )
    )
    XCTAssertEqual(
      WatchTranscript.presented(committed: committed, pending: [pending], streaming: []).count,
      4
    )
  }

  func testStreamingTranscriptKeepsAndCommitsKeyedMessages() {
    var transcript = WatchStreamingTranscript()
    transcript.append(
      WatchStreamDelta(text: "Hel", messageID: "a", index: 0, isFinal: false))
    transcript.append(
      WatchStreamDelta(text: "World", messageID: "b", index: 0, isFinal: false))
    transcript.append(
      WatchStreamDelta(text: "duplicate", messageID: "a", index: 0, isFinal: false))
    transcript.append(
      WatchStreamDelta(text: "lo", messageID: "a", index: 1, isFinal: false))

    XCTAssertEqual(transcript.messages.map(\.text), ["Hello", "World"])
    transcript.commit(WatchTranscriptMessage(id: "a", role: "assistant", text: "Hello"))
    XCTAssertEqual(transcript.messages.map(\.text), ["World"])
    transcript.reconcile(
      with: [WatchTranscriptMessage(id: "persisted-b", role: "assistant", text: "World complete")]
    )
    XCTAssertTrue(transcript.messages.isEmpty)
  }

  func testStreamingTranscriptSplitsFinalIDLessMessagesAndCapsPresentation() {
    var transcript = WatchStreamingTranscript()
    transcript.start(responseID: "response")
    transcript.append(
      WatchStreamDelta(text: "First", messageID: nil, index: 0, isFinal: false))
    transcript.append(
      WatchStreamDelta(text: "", messageID: nil, index: 1, isFinal: true))
    transcript.append(
      WatchStreamDelta(text: "Second", messageID: nil, index: 0, isFinal: false))

    XCTAssertEqual(transcript.messages.map(\.text), ["First", "Second"])
    let committed = (0..<7).map {
      WatchTranscriptMessage(id: "\($0)", role: "assistant", text: "\($0)")
    }
    XCTAssertEqual(
      WatchTranscript.presented(committed: committed, pending: [], streaming: transcript.messages)
        .map(\.text),
      ["1", "2", "3", "4", "5", "6", "First", "Second"]
    )
  }

  func testSSEParserDecodesConversationEvents() throws {
    assertEvent(#"{"type":"session.heartbeat"}"#, .heartbeat)
    assertEvent(
      #"{"type":"response.output_text.delta","delta":"Café ⌚️","message_id":"m1","index":2,"final":true}"#,
      .textDelta(
        WatchStreamDelta(text: "Café ⌚️", messageID: "m1", index: 2, isFinal: true))
    )
    assertEvent(
      #"{"type":"session.input.consumed","data":{"item_id":"u1","type":"message","data":{"role":"user","content":[{"type":"input_text","text":"Go"}]},"cleared_pending_id":"p1"}}"#,
      .inputConsumed(
        WatchStreamInput(
          message: WatchTranscriptMessage(id: "u1", role: "user", text: "Go"),
          pendingID: "p1"
        )
      )
    )
    assertEvent(
      #"{"type":"response.output_item.done","item":{"id":"a1","type":"message","role":"assistant","content":[{"type":"output_text","text":"Done"}]}}"#,
      .messageCommitted(WatchTranscriptMessage(id: "a1", role: "assistant", text: "Done"))
    )
    assertEvent(
      #"{"type":"response.failed","response":{"last_error":{"message":"Lost"}}}"#,
      .failed("Lost")
    )
    assertEvent("[DONE]", .streamFinished)
  }

  func testSSEParserJoinsDataLinesAndIgnoresUnknownEvents() {
    var parser = WatchSessionSSEParser()
    XCTAssertEqual(
      parser.feed(
        Data(
          "event: response.output_text.delta\r\ndata: {\"type\":\"response.output_text.delta\",\r\ndata: \"delta\":\"joined\"}\r\n\r\n"
            .utf8
        )
      ),
      [
        .textDelta(
          WatchStreamDelta(text: "joined", messageID: nil, index: nil, isFinal: false))
      ]
    )
    XCTAssertNil(event(#"{"type":"session.presence"}"#))
  }

  func testSSEParserPreservesFrameBoundariesAndStopsAtDone() {
    var parser = WatchSessionSSEParser()
    let wire =
      "data: {\"type\":\"session.heartbeat\"}\n\n"
      + "data: {\"type\":\"response.output_text.delta\",\"delta\":\"Café ⌚️\"}\n\n"
      + "data: [DONE]\n\n"
      + "data: {\"type\":\"session.heartbeat\"}\n\n"

    XCTAssertEqual(
      parser.feed(Data(wire.utf8)),
      [
        .heartbeat,
        .textDelta(
          WatchStreamDelta(text: "Café ⌚️", messageID: nil, index: nil, isFinal: false)),
        .streamFinished,
      ]
    )
    XCTAssertTrue(parser.isFinished)
  }

  func testSSEParserDropsOversizedInputAndRecoversAtNextFrame() {
    let limit = 1_048_576
    var parser = WatchSessionSSEParser()
    let oversizedLine = "data: " + String(repeating: "x", count: limit) + "\n\n"
    let frameChunk = String(repeating: " ", count: limit / 2 + 1)
    let oversizedFrame = "data: \(frameChunk)\ndata: \(frameChunk)\n\n"

    XCTAssertTrue(parser.feed(Data(oversizedLine.utf8)).isEmpty)
    XCTAssertTrue(parser.feed(Data(oversizedFrame.utf8)).isEmpty)
    XCTAssertEqual(
      parser.feed(Data("data: {\"type\":\"session.heartbeat\"}\n\n".utf8)),
      [.heartbeat]
    )
  }

  func testMarkdownRendersInlineAndBlockSyntax() {
    let rendered = WatchMarkdown.attributedString(
      from: """
        # Status

        - **Ready**
        - Waiting

        > Quoted

        ```swift
        let watch = "online"
        ```

        | State | Count |
        | --- | ---: |
        | Ready | 2 |
        """
    )

    XCTAssertEqual(
      String(rendered.characters),
      "Status\n\n• Ready\n• Waiting\n\n│ Quoted\n\nlet watch = \"online\"\n\nState: Ready\nCount: 2"
    )
    XCTAssertTrue(rendered.hasIntent(.stronglyEmphasized, on: "Status"))
    XCTAssertTrue(rendered.hasIntent(.stronglyEmphasized, on: "State:"))
    XCTAssertTrue(rendered.hasIntent(.code, on: "let watch = \"online\""))
  }

  func testMarkdownSanitizesLinksAndKeepsPartialText() {
    let rendered = WatchMarkdown.attributedString(
      from: "[Docs](https://example.com) [Unsafe](javascript:alert(1))"
    )
    let links = rendered.runs.compactMap(\.link)

    XCTAssertEqual(String(rendered.characters), "Docs Unsafe")
    XCTAssertEqual(links.map(\.absoluteString), ["https://example.com"])
    XCTAssertEqual(
      String(WatchMarkdown.attributedString(from: "**Working").characters),
      "**Working"
    )
    XCTAssertEqual(
      WatchMarkdown.streamingRemainder(
        after: "- first\n",
        renderedText: "• first",
        in: "- first\n- second"
      ),
      "\n- second"
    )
    XCTAssertTrue(WatchMarkdown.canRender(String(repeating: "a", count: 50_000)))
    XCTAssertFalse(WatchMarkdown.canRender(String(repeating: "a", count: 50_001)))
  }

  func testMapsWorkspaceMountAndBuildsEndpoint() throws {
    let workspace = try url("https://dbc.example.com/omnigent/")
    let api = WatchEndpoint.apiBaseURL(for: workspace, workspaceUIPath: "/omnigent")

    XCTAssertEqual(api.absoluteString, "https://dbc.example.com/api/2.0/omnigent")
    XCTAssertEqual(
      WatchEndpoint.apiBaseURL(
        for: try url("https://dbc.example.com/omnigent/c/session"),
        workspaceUIPath: "/omnigent"
      ).absoluteString,
      api.absoluteString
    )
    XCTAssertEqual(
      WatchEndpoint.url(baseURL: api, path: "/v1/sessions").absoluteString,
      "https://dbc.example.com/api/2.0/omnigent/v1/sessions"
    )
    let standalone = try url("http://localhost:6767")
    XCTAssertEqual(
      WatchEndpoint.apiBaseURL(for: standalone, workspaceUIPath: "/omnigent"),
      standalone
    )
    XCTAssertEqual(
      WatchEndpoint.apiBaseURL(
        for: try url("http://localhost:6767/c/session"),
        workspaceUIPath: "/omnigent"
      ),
      standalone
    )
  }

  func testFiltersCookiesByDomainPathAndTransport() throws {
    let secure = try XCTUnwrap(
      HTTPCookie(properties: [
        .domain: ".example.com",
        .path: "/api",
        .name: "session",
        .value: "value",
        .secure: "TRUE",
      ])
    )

    XCTAssertTrue(try applies(secure, to: "https://app.example.com/api/v1"))
    XCTAssertFalse(try applies(secure, to: "http://app.example.com/api/v1"))
    XCTAssertFalse(try applies(secure, to: "https://app.example.com/other"))
    XCTAssertFalse(try applies(secure, to: "https://app.example.com/apiary"))
  }

  private func decode<T: Decodable>(_ json: String) throws -> T {
    try JSONDecoder().decode(T.self, from: Data(json.utf8))
  }

  private func event(_ json: String) -> WatchSessionStreamEvent? {
    var parser = WatchSessionSSEParser()
    return parser.feed(Data("data: \(json)\n\n".utf8)).first
  }

  private func assertEvent(
    _ json: String,
    _ expected: WatchSessionStreamEvent?,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    XCTAssertEqual(event(json), expected, file: file, line: line)
  }

  private func url(_ value: String) throws -> URL {
    try XCTUnwrap(URL(string: value))
  }

  private func applies(_ cookie: HTTPCookie, to value: String) throws -> Bool {
    WatchBridge.cookie(cookie, appliesTo: try url(value))
  }
}

extension AttributedString {
  fileprivate func hasIntent(_ intent: InlinePresentationIntent, on text: String) -> Bool {
    runs.contains {
      String(self[$0.range].characters) == text
        && $0.inlinePresentationIntent?.contains(intent) == true
    }
  }
}
