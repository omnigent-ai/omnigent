import Foundation
import Observation

@MainActor @Observable
final class WatchSessionList {
  private let client: WatchClient
  private(set) var sessions: [WatchSession]?
  private(set) var groups: [WatchSessionGroup] = []
  private(set) var error: String?
  private var loadGeneration = 0

  init(client: WatchClient) {
    self.client = client
  }

  func load() async {
    loadGeneration += 1
    let generation = loadGeneration
    do {
      let query = ["order": "desc", "sort_by": "updated_at", "limit": "25", "visibility": "all"]
      async let page: WatchPage<WatchSession> = client.get(["sessions"], query)
      let projects: [WatchProject] = (try? await client.get(["sessions", "projects"])) ?? []
      let loaded = try await page.data
      guard generation == loadGeneration else { return }
      (sessions, groups) = (loaded, loaded.grouped(projects: projects))
      error = nil
    } catch is CancellationError {
    } catch let failure as URLError where failure.code == .cancelled {
    } catch {
      guard generation == loadGeneration else { return }
      self.error = error.localizedDescription
    }
  }
}
