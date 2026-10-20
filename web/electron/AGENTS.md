# Electron agent guidance

## Cross-version compatibility

The Electron shell and the web app it loads can be released independently. A newer web bundle may run inside an older installed shell, and a newer shell may load an older web bundle. Treat preload methods, IPC handlers, and desktop feature fields as versioned capabilities, not as guarantees of `isElectronShell()`.

When adding or changing a shell-backed web feature:

- Expose new functionality through an optional preload bridge method and, when applicable, a desktop feature flag. Keep existing bridge contracts compatible with older web bundles.
- Gate the web UI and shell-dependent requests on the actual bridge method and enabled feature flags; missing fields mean unsupported. Do not show a control that can only work after a shell upgrade.
- Test both a capable shell and an older/partial bridge (including missing methods or fields). Verify that unsupported shells do not make the feature's requests or show unusable controls.

See `web/src/lib/nativeBridge.ts` for the web-side compatibility boundary.
