import Foundation

/// Hiding the Databricks workspace navigation chrome around a workspace-hosted
/// Omnigent SPA. Kept in its own WebKit-free type so the script is unit-testable
/// (`OmnigentTests/WorkspaceChromeScriptTests.swift`) without a `WKWebView`.
///
/// Ported from `web/electron/src/workspace-chrome.js`; the Android shell carries
/// the same logic in `WorkspaceChromeScript.kt`. Keep the CSS identical in all
/// three so a fix in one shell isn't silently missing from the others.
enum WorkspaceChromeScript {
  /// CSS that hides the Databricks workspace navigation chrome.
  ///
  /// On a workspace the SPA is mounted as a workspace *page*, so Databricks wraps
  /// it in its top-nav shell (the dark bar with the workspace switcher). In a
  /// dedicated app window that chrome is just noise. We promote Omnigent's own
  /// root — `.omnigent-app`, which the embed entry sets (`web/src/embed.tsx`) —
  /// to a full-viewport overlay so it paints over the workspace bar. Keying on
  /// Omnigent's wrapper (defined in THIS repo) rather than the monolith-owned,
  /// unstable workspace nav markup keeps this from silently breaking when
  /// Databricks reshuffles its chrome; on a standalone (non-embed) build there is
  /// no `.omnigent-app`, so the rule is a harmless no-op.
  static let css = """
    .omnigent-app {
      position: fixed !important;
      inset: 0 !important;
      z-index: 2147483647 !important;
    }
    """

  /// JS that installs ``css`` into the current document, at most once.
  ///
  /// The caller injects this on every finished main-frame load of the pinned
  /// origin, and must NOT gate it on the URL's path. An auth redirect can land on
  /// a path other than the `/omnigent` mount, so a path guard leaves the workspace
  /// switcher visible — from there a
  /// user navigates into another workspace app with no way back. Do not
  /// reintroduce a path guard.
  static var source: String {
    """
    (() => {
      if (document.querySelector("style[data-omnigent-workspace-chrome]")) return;
      const style = document.createElement("style");
      style.dataset.omnigentWorkspaceChrome = "true";
      style.textContent = \(WebViewModel.javascriptString(css));
      document.documentElement.appendChild(style);
    })();
    """
  }
}
