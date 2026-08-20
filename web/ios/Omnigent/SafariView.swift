import SafariServices
import SwiftUI

// Presents a URL in an in-app Safari sheet so external links (website, docs,
// privacy policy) open without leaving Omnigent.
struct SafariView: UIViewControllerRepresentable {
  let url: URL

  func makeUIViewController(context: Context) -> SFSafariViewController {
    SFSafariViewController(url: url)
  }

  func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
