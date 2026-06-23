import Foundation
import WebKit

enum WebViewMode: String {
  case chat
  case terminal
}

@MainActor
final class WebViewModel: ObservableObject {
  @Published var currentURL: URL?
  @Published var isLoading = false
  @Published var serverSwitcherHidden = true

  /// Whether the native Chat/Terminal switcher should be shown. The web app owns
  /// this truth and pushes it via `setViewMode`; we only render when it asks us to.
  @Published var bottomBarVisible = false
  /// Currently selected mode, kept in sync with the web app in both directions.
  @Published var viewMode: WebViewMode = .chat
  /// Whether the Terminal option is selectable (web is connected to a session).
  @Published var terminalEnabled = false
  /// Terminal is booting but not yet openable — drives a spinner on the segment.
  @Published var terminalStartingUp = false

  weak var webView: WKWebView?

  func reload() {
    webView?.reload()
  }

  func emitNotificationActivation(_ path: String) {
    guard path.starts(with: "/") else { return }
    let script = "window.__omnigentNativeEmitNotificationActivated?.(\(Self.javascriptString(path)));"
    webView?.evaluateJavaScript(script)
  }

  /// Tell the web app the user tapped a segment in the native switcher.
  func emitViewModeChanged(_ mode: WebViewMode) {
    let script = "window.__omnigentNativeEmitViewModeChanged?.(\(Self.javascriptString(mode.rawValue)));"
    webView?.evaluateJavaScript(script)
  }

  static func javascriptString(_ value: String) -> String {
    guard let data = try? JSONEncoder().encode(value),
          let encoded = String(data: data, encoding: .utf8) else {
      return "\"\""
    }
    return encoded
  }
}
