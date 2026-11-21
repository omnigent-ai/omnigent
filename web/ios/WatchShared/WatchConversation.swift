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
  private(set) var pollDelay: Duration = .seconds(5)
  /// Suppress stale ordinary prompts; native delivery confirmation remains server-owned.
  private var answered: Set<String> = []
  private var loadGeneration = 0
  private var loadError: String?
  private var resolveError: ResolveFailure?
  var error: String? { resolveError?.message ?? loadError }

  private struct ResolveFailure: Equatable {
    let id = UUID()
    let approvalID: String
    let message: String
  }

  init(session: WatchSession, client: WatchClient) {
    self.session = session
    self.client = client
  }

  func load() async {
    loadGeneration += 1
    let generation = loadGeneration
    defer {
      if generation == loadGeneration { isLoading = false }
    }
    let failureBeforeLoad = resolveError
    let path = "sessions/\(session.id)"
    let snapshotQuery = [
      "include_items": "false", "include_liveness": "false", "include_usage": "false",
    ]
    do {
      let host = try await client.routingHost(for: session.id)
      async let snapshot: WatchSnapshot = client.get(path, snapshotQuery, host: host)
      async let items = recentItems(path + "/items", host: host)
      let (state, transcript) = try await (snapshot, items)
      guard generation == loadGeneration else { return }
      pollDelay = .seconds(5)
      messages = state.messages(items: transcript)
      approvals = (state.pendingElicitations ?? []).filter {
        $0.params.nativeDelivery != nil || !answered.contains($0.id)
      }
      // Only a poll begun after this failure can confirm it was resolved elsewhere.
      if let failureBeforeLoad, failureBeforeLoad == resolveError,
        !approvals.contains(where: { $0.id == failureBeforeLoad.approvalID })
      {
        resolveError = nil
      }
      session.status = state.status ?? session.status
      session.pendingElicitationsCount = approvals.count
      loadError = state.status == "failed" ? state.lastTaskError?.message ?? "Session failed." : nil
    } catch is CancellationError {
    } catch let failure as URLError where failure.code == .cancelled {
    } catch {
      guard generation == loadGeneration else { return }
      loadError = error.localizedDescription
      pollDelay = min(pollDelay * 2, .seconds(120))
    }
  }

  private func recentItems(_ path: String, host: String?) async throws -> [WatchItem] {
    var query = ["order": "desc", "limit": "30"]
    var items: [WatchItem] = []
    var cursors: Set<String> = []
    // Bound polling work even when hundreds of tool/reasoning items precede visible messages.
    for _ in 0..<10 {
      let page: WatchPage<WatchItem> = try await client.get(path, query, host: host)
      items += page.data
      guard items.reversed().bubbles.count < 12, page.hasMore == true, !page.data.isEmpty,
        let cursor = page.lastId, cursors.insert(cursor).inserted
      else { break }
      query["after"] = cursor
    }
    return items
  }

  func resolve(_ approval: WatchElicitation, accept: Bool) async {
    guard approval.isBinary, !isBusy, !answered.contains(approval.id) else { return }
    let target = approval.params.targetSessionId ?? session.id
    isBusy = true
    defer { isBusy = false }
    do {
      let host = try await client.routingHost(for: target)
      try await client.post(
        "sessions/\(target)/elicitations/\(approval.id)/resolve",
        ["action": accept ? "accept" : "decline"],
        host: host)
      resolveError = nil
      answered.insert(approval.id)
      approvals.removeAll { $0.id == approval.id }
      await load()
    } catch {
      resolveError = ResolveFailure(approvalID: approval.id, message: error.localizedDescription)
    }
  }
}
