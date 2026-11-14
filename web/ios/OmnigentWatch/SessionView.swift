import SwiftUI

struct SessionView: View {
  @State private var conversation: Conversation

  init(session: WatchSession, client: WatchClient) {
    _conversation = State(initialValue: Conversation(session: session, client: client))
  }

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 8) {
        if let error = conversation.error { ErrorLabel(error) }
        ForEach(conversation.messages) { MessageBubble(message: $0) }
        if conversation.isLoading {
          ProgressView().frame(maxWidth: .infinity)
        } else if conversation.messages.isEmpty {
          Text("No messages yet").font(.footnote).foregroundStyle(.secondary)
        }
        ForEach(conversation.approvals) { ApprovalCard(approval: $0, conversation: conversation) }
      }
    }
    .defaultScrollAnchor(.bottom)
    // Keep the latest in view as cards resolve and replies arrive.
    .defaultScrollAnchor(.bottom, for: .sizeChanges)
    .environment(\.openURL, OpenURLAction { _ in .discarded })
    .navigationTitle(conversation.session.name)
    .containerBackground(for: .navigation) { BrandBackground() }
    .task {
      while !Task.isCancelled {
        await conversation.load()
        try? await Task.sleep(for: .seconds(5))
      }
    }
  }
}

private struct ApprovalCard: View {
  let approval: WatchElicitation
  let conversation: SessionView.Conversation

  var body: some View {
    VStack(alignment: .leading, spacing: 8) {
      Text("Needs response").font(.caption2.weight(.semibold)).foregroundStyle(Color.brand)
      Text(approval.params.message).font(.subheadline.weight(.medium))
      if let preview = approval.params.contentPreview, !preview.isEmpty {
        Text(preview)
          .font(.caption2.monospaced())
          .foregroundStyle(.secondary)
          .padding(6)
          .frame(maxWidth: .infinity, alignment: .leading)
          .background(.black.opacity(0.35), in: .rect(cornerRadius: 10))
      }
      if approval.isBinary {
        HStack {
          button("Decline", "xmark", .white.opacity(0.2), accept: false)
          button("Approve", "checkmark", .brand, accept: true)
        }
        .buttonStyle(.glassProminent)
        .disabled(conversation.isBusy)
      } else {
        Label("Open on iPhone", systemImage: "iphone").font(.caption2).foregroundStyle(.secondary)
      }
    }
    .padding(12)
    .glassEffect(.regular.tint(.brand.opacity(0.25)), in: .rect(cornerRadius: 22))
  }

  private func button(_ title: String, _ symbol: String, _ tint: Color, accept: Bool) -> some View {
    Button {
      Task { await conversation.resolve(approval, accept: accept) }
    } label: {
      Label(title, systemImage: symbol).labelStyle(.iconOnly).frame(maxWidth: .infinity)
    }
    .tint(tint)
  }
}

private struct MessageBubble: View {
  let message: WatchMessage

  var body: some View {
    let markdown = AttributedString.MarkdownParsingOptions(
      interpretedSyntax: .inlineOnlyPreservingWhitespace)
    HStack(alignment: .lastTextBaseline, spacing: 4) {
      Text(
        (try? AttributedString(markdown: message.text, options: markdown)) ?? .init(message.text))
      if message.isError {
        Image(systemName: "exclamationmark.circle").foregroundStyle(.red)
      } else if message.isPending {
        Image(systemName: "clock").font(.caption2)
      }
    }
    .font(.footnote)
    .fontDesign(.default)
    .foregroundStyle(message.isError ? .red : .primary)
    // The web chat: your messages in a glass bubble, the agent's as plain text.
    .padding(.horizontal, message.isUser ? 10 : 0)
    .padding(.vertical, message.isUser ? 7 : 0)
    .glassEffect(message.isUser ? .regular : .identity, in: .rect(cornerRadius: 16))
    .opacity(message.isPending ? 0.65 : 1)
    .frame(maxWidth: .infinity, alignment: message.isUser ? .trailing : .leading)
    .padding(message.isUser ? .leading : .trailing, 14)
  }
}

extension SessionView {
  /// One session's transcript and pending approvals, read from the snapshot and items APIs.
  @MainActor @Observable
  final class Conversation {
    private(set) var session: WatchSession
    private let client: WatchClient
    private(set) var messages: [WatchMessage] = []
    private(set) var approvals: [WatchElicitation] = []
    private(set) var isLoading = true
    private(set) var isBusy = false
    /// Resolved here, so a poll that started before the answer can't bring the card back.
    private var answered: Set<String> = []
    var error: String?

    init(session: WatchSession, client: WatchClient) {
      self.session = session
      self.client = client
    }

    func load() async {
      let path = "sessions/\(session.id)"
      let snapshotQuery = [
        "include_items": "false", "include_liveness": "false", "include_usage": "false",
      ]
      do {
        async let snapshot: WatchSnapshot = client.get(path, snapshotQuery, host: session.hostId)
        async let items: WatchPage<WatchItem> = client.get(
          path + "/items", ["order": "desc", "limit": "30"], host: session.hostId)
        let (state, page) = try await (snapshot, items)
        let committed = Array(page.data.reversed().bubbles.suffix(12))
        // A native harness records the message before its pending copy clears; show it once.
        let recent = Set(committed.suffix(6).filter(\.isUser).map(\.text))
        let queued = (state.pendingInputs ?? []).compactMap(\.bubble).filter {
          !recent.contains($0.text)
        }
        messages = committed + queued
        approvals = (state.pendingElicitations ?? []).filter { !answered.contains($0.id) }
        session.status = state.status ?? session.status
        session.pendingElicitationsCount = approvals.count
        error = state.status == "failed" ? state.lastTaskError?.message ?? "Session failed." : nil
      } catch is CancellationError {
      } catch let failure as URLError where failure.code == .cancelled {
      } catch {
        self.error = error.localizedDescription
      }
      isLoading = false
    }

    func resolve(_ approval: WatchElicitation, accept: Bool) async {
      let target = approval.params.targetSessionId ?? session.id
      isBusy = true
      defer { isBusy = false }
      do {
        // A sub-agent's prompt may live on another host, so only the session's own uses its key.
        try await client.post(
          "sessions/\(target)/elicitations/\(approval.id)/resolve",
          ["action": accept ? "accept" : "decline"],
          host: target == session.id ? session.hostId : nil)
        answered.insert(approval.id)
        approvals.removeAll { $0.id == approval.id }
        await load()
      } catch {
        self.error = error.localizedDescription
      }
    }
  }
}
