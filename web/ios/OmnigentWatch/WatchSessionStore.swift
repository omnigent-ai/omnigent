import Foundation

@MainActor
final class WatchSessionStore: ObservableObject {
  @Published private(set) var sessions: [WatchSessionSummary] = []
  @Published private(set) var projects: [WatchProjectSummary] = []
  @Published private(set) var isLoading = false
  @Published var errorMessage: String?

  func refresh(credentials: WatchCredentials) async {
    isLoading = true
    defer { isLoading = false }
    do {
      let client = WatchAPIClient(credentials: credentials)
      async let loadedSessions = client.sessions()
      async let loadedProjects = client.projects()
      sessions = try await loadedSessions
      projects = (try? await loadedProjects) ?? []
      errorMessage = nil
    } catch {
      errorMessage = error.localizedDescription
    }
  }
}
