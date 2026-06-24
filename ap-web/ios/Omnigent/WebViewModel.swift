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
    let script =
      "window.__omnigentNativeEmitNotificationActivated?.(\(Self.javascriptString(path)));"
    webView?.evaluateJavaScript(script)
  }

  /// Push the footprint (in CSS px, excluding the OS safe area which the web
  /// layer adds via `env()`) of the native floating bars to the web app. The
  /// web side folds these into its `--omnigent-inset-*` variables so page
  /// content reserves the right amount of space — making native bar dimensions
  /// the single source of truth instead of magic numbers duplicated in CSS.
  func emitInsets(topBar: CGFloat, bottomBar: CGFloat) {
    let script =
      "window.__omnigentNativeEmitInsets?.(\(jsNumber(topBar)), \(jsNumber(bottomBar)));"
    webView?.evaluateJavaScript(script)
  }

  /// Tell the web app the user tapped a segment in the native switcher.
  func emitViewModeChanged(_ mode: WebViewMode) {
    let script =
      "window.__omnigentNativeEmitViewModeChanged?.(\(Self.javascriptString(mode.rawValue)));"
    webView?.evaluateJavaScript(script)
  }

  func emitSidebarDrag(phase: String, progress: Double) {
    let clamped = max(0, min(1, progress))
    let script =
      "window.__omnigentNativeEmitSidebarDrag?.(\(Self.javascriptString(phase)), \(clamped));"
    webView?.evaluateJavaScript(script)
  }

  /// Format a CGFloat as a bare JS number literal (no units, finite-guarded).
  private func jsNumber(_ value: CGFloat) -> String {
    guard value.isFinite else { return "0" }
    return String(format: "%g", Double(value))
  }

  static func javascriptString(_ value: String) -> String {
    guard let data = try? JSONEncoder().encode(value),
      let encoded = String(data: data, encoding: .utf8)
    else {
      return "\"\""
    }
    return encoded
  }
}
