import SwiftUI

private struct WatchConversationRefresh {
  let status: String?
  let pendingInputs: [WatchPendingInput]
  let messages: [WatchTranscriptMessage]
}

private struct WatchConversationPollRequest {
  let id = UUID()
  let baselineAssistantIDs: Set<String>
  let baselineUserIDs: Set<String>
  let provisionalInput: WatchPendingInput?
  let submittedText: String?
}

struct WatchSessionDetailView: View {
  let session: WatchSessionSummary
  let credentials: WatchCredentials

  @State private var elicitations: [WatchElicitation] = []
  @State private var messages: [WatchTranscriptMessage] = []
  @State private var pendingInputs: [WatchPendingInput] = []
  @State private var streaming = WatchStreamingTranscript()
  @State private var draft = ""
  @State private var isLoading = true
  @State private var isSending = false
  @State private var errorMessage: String?
  @State private var pollingRequest: WatchConversationPollRequest?
  @State private var latestRefreshID = UUID()

  private var client: WatchAPIClient { WatchAPIClient(credentials: credentials) }

  private var presentedMessages: [WatchTranscriptMessage] {
    WatchTranscript.presented(
      committed: messages,
      pending: pendingInputs,
      streaming: streaming.messages
    )
  }

  var body: some View {
    ZStack {
      OmnigentWatchTheme.canvas.ignoresSafeArea()

      ScrollViewReader { proxy in
        List {
          if let errorMessage {
            Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
              .font(.footnote)
              .foregroundStyle(OmnigentWatchTheme.error)
              .listRowBackground(
                OmnigentWatchRowBackground(color: OmnigentWatchTheme.error.opacity(0.14))
              )
              .accessibilityLabel("Error: \(errorMessage)")
          }

          ForEach(elicitations) { elicitation in
            Section {
              VStack(alignment: .leading, spacing: 8) {
                Text(elicitation.params.message)
                  .foregroundStyle(OmnigentWatchTheme.foreground)

                if let preview = elicitation.params.contentPreview, !preview.isEmpty {
                  WatchExpandableText(
                    text: preview,
                    font: .caption2.monospaced(),
                    color: OmnigentWatchTheme.mutedForeground,
                    collapsedLineLimit: 5
                  )
                }

                if elicitation.params.canResolveOnWatch {
                  approvalActions(for: elicitation)
                } else {
                  Label("Open on iPhone", systemImage: "iphone")
                    .font(.footnote)
                    .foregroundStyle(OmnigentWatchTheme.mutedForeground)
                }
              }
              .padding(.vertical, 3)
              .listRowBackground(
                OmnigentWatchRowBackground(color: OmnigentWatchTheme.warning.opacity(0.14))
              )
            } header: {
              Label("Approval", systemImage: "hand.raised.fill")
                .foregroundStyle(OmnigentWatchTheme.warning)
            }
          }

          Section {
            if presentedMessages.isEmpty && !isLoading {
              Text("No messages")
                .foregroundStyle(OmnigentWatchTheme.mutedForeground)
                .listRowBackground(OmnigentWatchRowBackground())
            } else {
              ForEach(presentedMessages) { item in
                WatchTranscriptBubble(item: item, assistantName: session.agentName ?? "Assistant")
                  .listRowBackground(Color.clear)
                  .id(item.id)
              }
            }
          } header: {
            Label("Conversation", systemImage: "bubble.left.and.bubble.right.fill")
              .foregroundStyle(OmnigentWatchTheme.brandForeground)
          }

          Section {
            TextField("Reply", text: $draft)
              .disabled(isSending)
              .listRowBackground(
                OmnigentWatchRowBackground(color: OmnigentWatchTheme.raisedSurface)
              )
            Button {
              Task { await sendMessage() }
            } label: {
              if isSending {
                ProgressView().tint(OmnigentWatchTheme.background)
              } else {
                Label("Send", systemImage: "paperplane.fill")
              }
            }
            .buttonStyle(.borderedProminent)
            .tint(OmnigentWatchTheme.brandAccent)
            .foregroundStyle(OmnigentWatchTheme.background)
            .listRowBackground(Color.clear)
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
          } header: {
            Label("Reply", systemImage: "arrow.up.circle.fill")
              .foregroundStyle(OmnigentWatchTheme.brandForeground)
          }
        }
        .scrollContentBackground(.hidden)
        .onChange(of: streaming.messages) { _, messages in
          guard let message = messages.last else { return }
          withAnimation(.easeOut(duration: 0.15)) {
            proxy.scrollTo(message.id, anchor: .bottom)
          }
        }
      }
    }
    .navigationTitle(session.displayTitle)
    .overlay {
      if isLoading {
        ProgressView()
          .tint(OmnigentWatchTheme.brandAccent)
          .accessibilityLabel("Loading conversation")
      }
    }
    .refreshable { await refresh(preserving: pollingRequest) }
    .task { await refresh(showLoading: true) }
    .task { await streamSession() }
    .task(id: pollingRequest?.id) {
      guard let request = pollingRequest else { return }
      await poll(request)
      if !Task.isCancelled, pollingRequest?.id == request.id {
        pollingRequest = nil
      }
    }
  }

  private func approvalActions(for elicitation: WatchElicitation) -> some View {
    ViewThatFits(in: .horizontal) {
      HStack {
        declineButton(for: elicitation)
        approveButton(for: elicitation)
      }
      .fixedSize(horizontal: true, vertical: false)
      VStack {
        approveButton(for: elicitation)
        declineButton(for: elicitation)
      }
    }
    .disabled(isSending)
  }

  private func declineButton(for elicitation: WatchElicitation) -> some View {
    Button("Decline", role: .destructive) {
      Task { await resolve(elicitation, action: "decline") }
    }
    .buttonStyle(.borderedProminent)
    .tint(OmnigentWatchTheme.error)
    .foregroundStyle(OmnigentWatchTheme.background)
  }

  private func approveButton(for elicitation: WatchElicitation) -> some View {
    Button("Approve") {
      Task { await resolve(elicitation, action: "accept") }
    }
    .buttonStyle(.borderedProminent)
    .tint(OmnigentWatchTheme.success)
    .foregroundStyle(OmnigentWatchTheme.background)
  }

  @discardableResult
  private func refresh(
    showLoading: Bool = false,
    preserving request: WatchConversationPollRequest? = nil
  ) async -> WatchConversationRefresh? {
    let refreshID = UUID()
    let baselineAssistantIDs =
      request?.baselineAssistantIDs ?? messageIDs(role: "assistant", in: messages)
    latestRefreshID = refreshID
    if showLoading { isLoading = true }
    defer { if showLoading { isLoading = false } }

    do {
      async let snapshot = client.session(id: session.id, hostID: session.hostID)
      async let recentMessages = client.messages(sessionID: session.id, hostID: session.hostID)
      let loaded = try await (snapshot, recentMessages)
      guard !Task.isCancelled, latestRefreshID == refreshID else { return nil }
      elicitations = loaded.0.pendingElicitations
      messages = loaded.1
      if let request {
        pendingInputs = WatchTranscript.reconcile(
          snapshot: loaded.0.pendingInputs,
          provisional: request.provisionalInput,
          committed: loaded.1,
          baselineUserIDs: request.baselineUserIDs
        )
      } else {
        pendingInputs = loaded.0.pendingInputs
      }
      streaming.reconcile(
        with: loaded.1.filter {
          $0.role == "assistant" && !baselineAssistantIDs.contains($0.id)
        })
      errorMessage =
        loaded.0.status == "failed"
        ? loaded.0.lastTaskError?.message ?? "Session failed."
        : nil
      return WatchConversationRefresh(
        status: loaded.0.status,
        pendingInputs: pendingInputs,
        messages: loaded.1
      )
    } catch {
      guard !Task.isCancelled, latestRefreshID == refreshID else { return nil }
      errorMessage = error.localizedDescription
      return nil
    }
  }

  private func poll(_ request: WatchConversationPollRequest) async {
    for delay: UInt64 in [0, 1, 2, 4, 8, 15, 30, 30] {
      guard await wait(delay) else { return }
      guard let loaded = await refresh(preserving: request) else { continue }
      if hasSettled(request, after: loaded) { return }
    }
  }

  private func streamSession() async {
    let delays: [UInt64] = [0, 1, 2, 4, 8, 15, 30]
    var failures = 0
    while !Task.isCancelled {
      let delay = delays[min(failures, delays.count - 1)]
      guard await wait(delay) else { return }
      streaming.reset()
      do {
        let events = try client.stream(sessionID: session.id, hostID: session.hostID)
        var refreshedForConnection = false
        for try await event in events {
          guard !Task.isCancelled else { return }
          if case .heartbeat = event {
            failures = 0
            if !refreshedForConnection {
              refreshedForConnection = true
              _ = await refresh(preserving: pollingRequest)
            }
          } else if case .streamFinished = event {
            return
          } else {
            await apply(event)
          }
        }
        failures += 1
      } catch {
        guard !(error is CancellationError || (error as? URLError)?.code == .cancelled) else {
          return
        }
        if case .authenticationExpired = error as? WatchAPIError {
          errorMessage = error.localizedDescription
          return
        }
        failures += 1
      }
    }
  }

  private func wait(_ seconds: UInt64) async -> Bool {
    guard seconds > 0 else { return true }
    return (try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)) != nil
  }

  private func apply(_ event: WatchSessionStreamEvent) async {
    switch event {
    case .heartbeat, .streamFinished:
      return
    case .responseStarted(let responseID):
      streaming.start(responseID: responseID)
    case .textDelta(let delta):
      streaming.append(delta)
    case .inputConsumed(let input):
      latestRefreshID = UUID()
      upsert(input.message)
      if let pendingID = input.pendingID,
        let index = pendingInputs.firstIndex(where: { $0.id == pendingID })
      {
        pendingInputs.remove(at: index)
      } else {
        pendingInputs = WatchTranscript.reconcile(
          snapshot: pendingInputs,
          provisional: nil,
          committed: [input.message],
          baselineUserIDs: []
        )
      }
    case .messageCommitted(let message):
      latestRefreshID = UUID()
      streaming.commit(message)
      upsert(message)
    case .responseEnded:
      streaming.reset()
      let request = pollingRequest
      settle(request, after: await refresh(preserving: request))
    case .failed(let message):
      streaming.reset()
      let failure = message?.trimmingCharacters(in: .whitespacesAndNewlines)
      let request = pollingRequest
      settle(request, after: await refresh(preserving: request))
      errorMessage = failure?.isEmpty == false ? failure : "Response failed."
    }
  }

  private func upsert(_ message: WatchTranscriptMessage) {
    if let index = messages.firstIndex(where: { $0.id == message.id }) {
      messages[index] = message
    } else {
      messages.append(message)
    }
    messages = Array(messages.suffix(8))
  }

  private func settle(
    _ request: WatchConversationPollRequest?,
    after loaded: WatchConversationRefresh?
  ) {
    guard let request, let loaded, pollingRequest?.id == request.id,
      hasSettled(request, after: loaded)
    else { return }
    pollingRequest = nil
  }

  private func hasSettled(
    _ request: WatchConversationPollRequest,
    after loaded: WatchConversationRefresh
  ) -> Bool {
    if loaded.status == "failed" { return true }
    guard loaded.status == "idle", loaded.pendingInputs.isEmpty else { return false }
    if let submittedText = request.submittedText {
      return WatchTranscript.hasReply(
        to: submittedText,
        committed: loaded.messages,
        baselineUserIDs: request.baselineUserIDs
      )
    }
    return !messageIDs(role: "assistant", in: loaded.messages)
      .isSubset(of: request.baselineAssistantIDs)
  }

  private func resolve(_ elicitation: WatchElicitation, action: String) async {
    guard !isSending else { return }
    isSending = true
    latestRefreshID = UUID()
    defer { isSending = false }
    do {
      let baseline = messages
      try await client.resolve(
        sessionID: elicitation.params.targetSessionID ?? session.id,
        elicitationID: elicitation.id,
        action: action,
        hostID: session.hostID
      )
      elicitations.removeAll { $0.id == elicitation.id }
      errorMessage = nil
      pollingRequest = WatchConversationPollRequest(
        baselineAssistantIDs: messageIDs(role: "assistant", in: baseline),
        baselineUserIDs: messageIDs(role: "user", in: baseline),
        provisionalInput: nil,
        submittedText: nil
      )
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func sendMessage() async {
    guard !isSending else { return }
    let submittedDraft = draft
    let text = submittedDraft.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return }

    let baseline = messages
    let localInput = WatchPendingInput(id: UUID().uuidString, text: text)
    pendingInputs.append(localInput)
    isSending = true
    latestRefreshID = UUID()
    defer { isSending = false }

    do {
      let acknowledgement = try await client.sendMessage(
        sessionID: session.id,
        text: text,
        hostID: session.hostID
      )
      var provisional: WatchPendingInput?
      if let index = pendingInputs.firstIndex(where: { $0.id == localInput.id }) {
        provisional = WatchPendingInput(
          id: acknowledgement.pendingID ?? localInput.id,
          text: text
        )
        pendingInputs[index] = provisional!
      }
      if draft == submittedDraft { draft = "" }
      errorMessage = nil
      pollingRequest = WatchConversationPollRequest(
        baselineAssistantIDs: messageIDs(role: "assistant", in: baseline),
        baselineUserIDs: messageIDs(role: "user", in: baseline),
        provisionalInput: provisional,
        submittedText: text
      )
    } catch {
      pendingInputs.removeAll { $0.id == localInput.id }
      errorMessage = error.localizedDescription
    }
  }

  private func messageIDs(
    role: String,
    in messages: [WatchTranscriptMessage]
  ) -> Set<String> {
    Set(messages.lazy.filter { $0.role == role }.map(\.id))
  }
}

private struct WatchTranscriptBubble: View {
  let item: WatchTranscriptMessage
  let assistantName: String

  private var isUser: Bool { item.role == "user" }
  private var isPending: Bool { item.id.hasPrefix("pending:") }
  private var isStreaming: Bool { item.id.hasPrefix("streaming:") }

  var body: some View {
    HStack {
      if isUser { Spacer(minLength: 16) }

      VStack(alignment: .leading, spacing: 4) {
        Label(
          isUser ? (isPending ? "Sending…" : "You") : (isStreaming ? "Live" : assistantName),
          systemImage: isPending
            ? "clock.fill" : (isUser ? "person.fill" : (isStreaming ? "waveform" : "sparkles"))
        )
        .font(.caption2)
        .foregroundStyle(
          isUser ? OmnigentWatchTheme.brandForeground : OmnigentWatchTheme.active
        )
        .accessibilityHidden(!isStreaming)

        WatchExpandableText(
          text: item.text,
          font: .footnote,
          color: OmnigentWatchTheme.foreground,
          collapsedLineLimit: 8,
          rendersMarkdown: true,
          accessibilityPrefix: isStreaming ? nil : (isUser ? "You" : assistantName)
        )
      }
      .padding(8)
      .background {
        RoundedRectangle(cornerRadius: 10, style: .continuous)
          .fill(
            isUser
              ? OmnigentWatchTheme.brandAccent.opacity(0.15)
              : OmnigentWatchTheme.raisedSurface
          )
      }
      .overlay {
        RoundedRectangle(cornerRadius: 10, style: .continuous)
          .stroke(
            isUser
              ? OmnigentWatchTheme.brandAccent.opacity(0.45)
              : OmnigentWatchTheme.active.opacity(0.22),
            lineWidth: 1
          )
      }

      if !isUser { Spacer(minLength: 16) }
    }
  }
}

private struct WatchExpandableText: View {
  let text: String
  let font: Font
  let color: Color
  let collapsedLineLimit: Int
  var rendersMarkdown = false
  var accessibilityPrefix: String? = nil

  @State private var isExpanded = false
  @State private var markdownSnapshot: WatchMarkdownSnapshot?

  private var offersExpansion: Bool {
    let lines = text.split(separator: "\n", omittingEmptySubsequences: false).count
    return lines > collapsedLineLimit || text.count > collapsedLineLimit * 20
  }

  private var markdownSource: String? {
    rendersMarkdown && WatchMarkdown.canRender(text) ? text : nil
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 4) {
      if markdownSource != nil {
        markdownText
      } else {
        styled(Text(text)).accessibilityLabel(accessibilityText(text))
      }

      if offersExpansion {
        Button(isExpanded ? "Show less" : "Show more") {
          withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() }
        }
        .buttonStyle(.plain)
        .font(.caption2)
        .foregroundStyle(OmnigentWatchTheme.brandForeground)
      }
    }
    .task(id: markdownSource) {
      guard let source = markdownSource else {
        markdownSnapshot = nil
        return
      }
      guard let rendered = await WatchMarkdownRenderer.shared.render(source), !Task.isCancelled
      else { return }
      markdownSnapshot = WatchMarkdownSnapshot(
        source: source,
        renderedText: rendered,
        visibleText: String(rendered.characters)
      )
    }
  }

  @ViewBuilder
  private var markdownText: some View {
    if let snapshot = markdownSnapshot,
      let remainder = WatchMarkdown.streamingRemainder(
        after: snapshot.source,
        renderedText: snapshot.visibleText,
        in: text
      )
    {
      styled(Text(snapshot.renderedText + AttributedString(remainder)))
        .accessibilityLabel(accessibilityText(snapshot.visibleText + remainder))
    } else {
      styled(Text(text)).accessibilityLabel(accessibilityText(text))
    }
  }

  private func styled(_ text: Text) -> some View {
    text
      .font(font)
      .foregroundStyle(color)
      .tint(OmnigentWatchTheme.brandForeground)
      .lineLimit(isExpanded ? nil : collapsedLineLimit)
  }

  private func accessibilityText(_ text: String) -> String {
    accessibilityPrefix.map { "\($0): \(text)" } ?? text
  }
}

private struct WatchMarkdownSnapshot {
  let source: String
  let renderedText: AttributedString
  let visibleText: String
}
