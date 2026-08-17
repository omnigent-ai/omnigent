import SwiftUI

struct WatchContentView: View {
  @EnvironmentObject private var connection: WatchConnectionManager
  @StateObject private var store = WatchSessionStore()

  var body: some View {
    NavigationStack {
      ZStack {
        OmnigentWatchTheme.canvas.ignoresSafeArea()
        if let credentials = connection.credentials {
          sessionList(credentials: credentials)
        } else {
          ContentUnavailableView(
            "Connect on iPhone",
            systemImage: "iphone.and.arrow.forward",
            description: Text("Open Omnigent on your iPhone.")
          )
          .foregroundStyle(OmnigentWatchTheme.foreground)
        }
      }
      .navigationTitle("Omnigent")
    }
  }

  private func sessionList(credentials: WatchCredentials) -> some View {
    let grouping = WatchSessionGrouping(sessions: store.sessions, projects: store.projects)
    return List {
      if let errorMessage = store.errorMessage {
        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
          .font(.footnote)
          .foregroundStyle(OmnigentWatchTheme.error)
          .listRowBackground(
            OmnigentWatchRowBackground(color: OmnigentWatchTheme.error.opacity(0.14))
          )
          .accessibilityLabel("Error: \(errorMessage)")
      }
      sessionSection(
        "Needs approval",
        icon: "hand.raised.fill",
        color: OmnigentWatchTheme.warning,
        items: grouping.needsAttention,
        credentials: credentials
      )
      sessionSection(
        "Active",
        icon: "waveform",
        color: OmnigentWatchTheme.active,
        items: grouping.active,
        credentials: credentials
      )
      ForEach(grouping.projects) { project in
        sessionSection(
          project.name,
          icon: "folder.fill",
          color: OmnigentWatchTheme.brandForeground,
          items: project.sessions,
          credentials: credentials,
          showsProject: false
        )
      }
      sessionSection(
        "Recent",
        icon: "clock.fill",
        color: OmnigentWatchTheme.brandForeground,
        items: grouping.recent,
        credentials: credentials
      )
      if store.sessions.isEmpty && store.errorMessage == nil && !store.isLoading {
        ContentUnavailableView("No sessions", systemImage: "bubble.left.and.bubble.right")
          .foregroundStyle(OmnigentWatchTheme.mutedForeground)
          .listRowBackground(Color.clear)
      }
    }
    .scrollContentBackground(.hidden)
    .overlay {
      if store.isLoading && store.sessions.isEmpty {
        ProgressView().accessibilityLabel("Loading sessions")
      }
    }
    .refreshable { await store.refresh(credentials: credentials) }
    .onAppear { Task { await store.refresh(credentials: credentials) } }
  }

  @ViewBuilder
  private func sessionSection(
    _ title: String,
    icon: String,
    color: Color,
    items: [WatchSessionDisplayItem],
    credentials: WatchCredentials,
    showsProject: Bool = true
  ) -> some View {
    if !items.isEmpty {
      Section {
        ForEach(items) { item in
          NavigationLink {
            WatchSessionDetailView(session: item.session, credentials: credentials)
          } label: {
            WatchSessionRow(
              session: item.session,
              projectName: showsProject ? item.projectName : nil
            )
          }
          .listRowBackground(OmnigentWatchRowBackground())
        }
      } header: {
        Label(title, systemImage: icon).foregroundStyle(color)
      }
    }
  }
}

private struct WatchSessionRow: View {
  let session: WatchSessionSummary
  let projectName: String?

  var body: some View {
    let style = appearance
    HStack(spacing: 8) {
      Image(systemName: style.icon)
        .frame(width: 26, height: 26)
        .foregroundStyle(style.color)
        .background(style.color.opacity(0.16), in: Circle())
        .accessibilityHidden(true)
      VStack(alignment: .leading, spacing: 2) {
        Text(session.displayTitle)
          .foregroundStyle(OmnigentWatchTheme.foreground)
          .lineLimit(2)
        HStack(spacing: 4) {
          Text(session.statusLabel).foregroundStyle(style.color)
          if let projectName {
            Label(projectName, systemImage: "folder.fill")
              .lineLimit(1)
              .foregroundStyle(OmnigentWatchTheme.brandForeground)
          }
        }
        .font(.caption2)
      }
    }
    .accessibilityElement(children: .combine)
  }

  private var appearance: (color: Color, icon: String) {
    if session.pendingElicitationsCount > 0 {
      return (OmnigentWatchTheme.warning, "hand.raised.fill")
    }
    switch session.status {
    case "launching", "running": return (OmnigentWatchTheme.active, "waveform")
    case "waiting": return (OmnigentWatchTheme.active, "hourglass")
    case "failed": return (OmnigentWatchTheme.error, "exclamationmark.circle.fill")
    case "idle": return (OmnigentWatchTheme.success, "checkmark.circle.fill")
    default: return (OmnigentWatchTheme.brandForeground, "sparkles")
    }
  }
}
