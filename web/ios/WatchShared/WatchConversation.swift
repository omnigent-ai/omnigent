import Foundation
import Observation

/// One session's transcript and pending approvals, read from the snapshot and items APIs.
@MainActor @Observable
final class WatchConversation {
  private(set) var session: WatchSession
  private let client: WatchClient
  private(set) var messages: [WatchMessage] = []
  private(set) var approvals: [WatchElicitation] = []
  private(set) var isLoading = true
  private(set) var isBusy = false
  /// Resolved here, so a poll that started before the answer can't bring the card back.
  private var answered: Set<String> = []
  private var loadError: String?
  private var resolveError: String?
  var error: String? { resolveError ?? loadError }

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
      messages = state.messages(items: page.data)
      approvals = (state.pendingElicitations ?? []).filter { !answered.contains($0.id) }
      session.status = state.status ?? session.status
      session.pendingElicitationsCount = approvals.count
      loadError = state.status == "failed" ? state.lastTaskError?.message ?? "Session failed." : nil
    } catch is CancellationError {
    } catch let failure as URLError where failure.code == .cancelled {
    } catch {
      loadError = error.localizedDescription
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
      resolveError = nil
      answered.insert(approval.id)
      approvals.removeAll { $0.id == approval.id }
      await load()
    } catch {
      resolveError = error.localizedDescription
    }
  }
}
