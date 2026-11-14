import SwiftUI

struct SessionListView: View {
  let client: WatchClient
  @State private var sessions: [WatchSession]?
  @State private var groups: [WatchSessionGroup] = []
  @State private var error: String?

  var body: some View {
    NavigationStack {
      List {
        if let error { ErrorLabel(error) }
        ForEach(groups) { group in
          Section {
            ForEach(group.sessions) { session in
              NavigationLink(value: session) { SessionRow(session: session) }
                .glassRow()
            }
          } header: {
            Group {
              if group.symbol.isEmpty {
                Text(group.title)
              } else {
                Label(group.title, systemImage: group.symbol)
              }
            }
            .font(.footnote.weight(.medium))
            .foregroundStyle(.secondary)
            .lineLimit(1)
          }
        }
      }
      .overlay {
        if sessions?.isEmpty == true {
          ContentUnavailableView("No sessions", systemImage: "bubble.left.and.bubble.right")
        }
      }
      .toolbar { LoadingIndicator(isLoading: sessions == nil && error == nil) }
      .navigationTitle("Omnigent")
      .navigationDestination(for: WatchSession.self) { SessionView(session: $0, client: client) }
      .containerBackground(for: .navigation) { BrandBackground() }
      .refreshable { await load() }
      .task {
        while !Task.isCancelled {
          await load()
          try? await Task.sleep(for: .seconds(30))
        }
      }
    }
  }

  private func load() async {
    do {
      let query = ["order": "desc", "sort_by": "updated_at", "limit": "25", "visibility": "all"]
      async let page: WatchPage<WatchSession> = client.get("sessions", query)
      let projects: [WatchProject] = (try? await client.get("sessions/projects")) ?? []
      let loaded = try await page.data
      (sessions, groups) = (loaded, loaded.grouped(projects: projects))
      error = nil
    } catch is CancellationError {
    } catch {
      self.error = error.localizedDescription
    }
  }
}

/// A sidebar row: the title, with the web's trailing state marker and nothing when idle.
/// A small spinner in the top-left corner while the list loads.
struct LoadingIndicator: ToolbarContent {
  let isLoading: Bool

  var body: some ToolbarContent {
    ToolbarItem(placement: .topBarLeading) {
      if isLoading { ProgressView().controlSize(.small) }
    }
  }
}

private struct SessionRow: View {
  let session: WatchSession

  var body: some View {
    VStack(alignment: .leading, spacing: 3) {
      HStack {
        Text(session.name).lineLimit(1)
        Spacer(minLength: 4)
        if session.approvals == 0 { SessionMarker(session: session) }
      }
      if session.approvals > 0 {
        Text("Needs response")
          .font(.caption2.weight(.medium))
          .foregroundStyle(Color.brand)
          .padding(.horizontal, 6)
          .padding(.vertical, 2)
          .background(Color.brand.opacity(0.15), in: .capsule)
      }
    }
  }
}

/// `SessionStateBadge`'s markers: a spinner while running, an alert on failure, a dot when unread.
struct SessionMarker: View {
  let session: WatchSession

  var body: some View {
    if session.isActive {
      Image(systemName: "progress.indicator")
        .foregroundStyle(.secondary)
        .symbolEffect(.variableColor.iterative)
        .accessibilityLabel("Session running")
    } else if session.status == "failed" {
      Image(systemName: "exclamationmark.circle").foregroundStyle(.red)
        .accessibilityLabel("Latest message is an error")
    } else if session.viewerUnread == true {
      Circle().fill(Color.brand).frame(width: 6, height: 6).accessibilityLabel("New messages")
    }
  }
}

extension View {
  /// A list row on an interactive Liquid Glass platter, like the iOS app's floating controls.
  func glassRow(tint: Color? = nil) -> some View {
    padding(.horizontal, 14)
      .padding(.vertical, 12)
      .frame(maxWidth: .infinity, alignment: .leading)
      .glassEffect(.regular.tint(tint).interactive(), in: .rect(cornerRadius: 22))
      .listRowBackground(Color.clear)
      .listRowInsets(EdgeInsets(top: 3, leading: 0, bottom: 3, trailing: 0))
  }
}

struct ErrorLabel: View {
  let message: String
  init(_ message: String) { self.message = message }

  var body: some View {
    Label(message, systemImage: "exclamationmark.triangle.fill")
      .font(.footnote)
      .foregroundStyle(.red)
  }
}
