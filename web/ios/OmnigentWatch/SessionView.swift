import SwiftUI

struct SessionView: View {
  @State private var conversation: WatchConversation

  init(session: WatchSession, client: WatchClient) {
    _conversation = State(initialValue: WatchConversation(session: session, client: client))
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
  let conversation: WatchConversation

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
