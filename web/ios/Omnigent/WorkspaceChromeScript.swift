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
  /// no `.omnigent-app`, so the rules are a harmless no-op.
  ///
  /// `position: fixed` only escapes to the viewport if no ancestor is a containing
  /// block or stacking context for it. A transform, filter, `contain`, or z-index on
  /// a workspace wrapper traps the overlay inside the page card: the header stays
  /// visible, and the full-viewport app shell gets pushed below the screen by the
  /// header's height, so the composer is cut off (OMNI-14862). The second rule
  /// clears those properties on every ancestor of `.omnigent-app`. It still keys only
  /// on Omnigent's wrapper, so it doesn't depend on the workspace's markup.
  static let css = """
    .omnigent-app {
      position: fixed !important;
      inset: 0 !important;
      z-index: 2147483647 !important;
    }
    :has(.omnigent-app) {
      transform: none !important;
      translate: none !important;
      rotate: none !important;
      scale: none !important;
      perspective: none !important;
      transform-style: flat !important;
      filter: none !important;
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
      contain: none !important;
      container-type: normal !important;
      content-visibility: visible !important;
      will-change: auto !important;
      clip-path: none !important;
      mask: none !important;
      z-index: auto !important;
      isolation: auto !important;
      opacity: 1 !important;
      mix-blend-mode: normal !important;
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
