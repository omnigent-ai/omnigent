import AuthenticationServices
import Foundation

/// The part of `ASWebAuthenticationSession` the browser drives; tests substitute a fake.
@MainActor
protocol WebAuthenticationSession: AnyObject {
  func start() -> Bool
  func cancel()
}

extension ASWebAuthenticationSession: WebAuthenticationSession {}

enum WebAuthenticationBrowserError: Error, Equatable {
  /// Another sign-in sheet is already presented by this browser.
  case inProgress
  /// The system refused to present the sheet.
  case unavailable
  /// The sheet ended with an error other than the user cancelling.
  case failed
  /// The sheet completed without a callback URL.
  case missingCallback
}

/// Presents one system web-authentication sheet at a time and returns its callback URL.
///
/// Cancelling (``cancel()`` or the calling task) resumes the caller immediately and ignores any
/// later completion from the cancelled sheet. A user-cancelled sheet throws `CancellationError`.
@MainActor
final class WebAuthenticationBrowser {
  typealias SessionFactory = (
    URL, ASWebAuthenticationSession.Callback, ASWebAuthenticationPresentationContextProviding,
    @escaping ASWebAuthenticationSession.CompletionHandler
  ) -> any WebAuthenticationSession

  private struct Run {
    let id: UUID
    var session: (any WebAuthenticationSession)?
    var presentationContext: WebAuthenticationPresentationContext?
    var continuation: CheckedContinuation<URL, Error>?
  }

  private let makeSession: SessionFactory
  private var run: Run?

  var isPresenting: Bool { run != nil }

  init(sessionFactory: SessionFactory? = nil) {
    makeSession =
      sessionFactory ?? { url, callback, provider, completion in
        let session = ASWebAuthenticationSession(
          url: url, callback: callback, completionHandler: completion)
        session.presentationContextProvider = provider
        // Share Safari's cookies so existing IdP sessions, passkeys and saved passwords apply.
        session.prefersEphemeralWebBrowserSession = false
        return session
      }
  }

  func callbackURL(
    for url: URL, callback: ASWebAuthenticationSession.Callback, anchor: ASPresentationAnchor
  ) async throws -> URL {
    try Task.checkCancellation()
    guard run == nil else { throw WebAuthenticationBrowserError.inProgress }
    let id = UUID()
    run = Run(id: id)
    return try await withTaskCancellationHandler {
      try await withCheckedThrowingContinuation { continuation in
        guard run?.id == id else {
          continuation.resume(throwing: CancellationError())
          return
        }
        let context = WebAuthenticationPresentationContext(anchor: anchor)
        let session = makeSession(url, callback, context) { [weak self] url, error in
          Task { @MainActor in self?.complete(id: id, url: url, error: error) }
        }
        run?.session = session
        run?.presentationContext = context
        run?.continuation = continuation
        if !session.start() {
          run = nil
          continuation.resume(throwing: WebAuthenticationBrowserError.unavailable)
        }
      }
    } onCancel: {
      Task { @MainActor [weak self] in self?.cancel(id: id) }
    }
  }

  func cancel() {
    guard let run else { return }
    cancel(id: run.id)
  }

  private func cancel(id: UUID) {
    guard let current = run, current.id == id else { return }
    run = nil
    current.session?.cancel()
    // Programmatic browser cancellation need not invoke its completion handler.
    current.continuation?.resume(throwing: CancellationError())
  }

  private func complete(id: UUID, url: URL?, error: Error?) {
    guard let current = run, current.id == id, let continuation = current.continuation else {
      return
    }
    run = nil
    if let error {
      let error = error as NSError
      if error.domain == ASWebAuthenticationSessionErrorDomain,
        error.code == ASWebAuthenticationSessionError.Code.canceledLogin.rawValue
      {
        continuation.resume(throwing: CancellationError())
      } else {
        continuation.resume(throwing: WebAuthenticationBrowserError.failed)
      }
    } else if let url {
      continuation.resume(returning: url)
    } else {
      continuation.resume(throwing: WebAuthenticationBrowserError.missingCallback)
    }
  }
}

@MainActor
private final class WebAuthenticationPresentationContext: NSObject,
  ASWebAuthenticationPresentationContextProviding
{
  let anchor: ASPresentationAnchor

  init(anchor: ASPresentationAnchor) {
    self.anchor = anchor
  }

  func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
    anchor
  }
}
