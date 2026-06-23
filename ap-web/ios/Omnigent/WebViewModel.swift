import Foundation
import WebKit

@MainActor
final class WebViewModel: ObservableObject {
  @Published var currentURL: URL?
  @Published var isLoading = false
  @Published var serverSwitcherHidden = true

  weak var webView: WKWebView?

  func reload() {
    webView?.reload()
  }

  func emitNotificationActivation(_ path: String) {
    guard path.starts(with: "/") else { return }
    let script = "window.__omnigentNativeEmitNotificationActivated?.(\(Self.javascriptString(path)));"
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
