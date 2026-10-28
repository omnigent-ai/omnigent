# Electron agent guidance

## Cross-version compatibility

The Electron shell and the web app it loads can be released independently. A newer web bundle may run inside an older installed shell, and a newer shell may load an older web bundle. Treat preload methods, IPC handlers, and desktop feature fields as versioned capabilities, not as guarantees of `isElectronShell()`.

When adding or changing a shell-backed web feature:

- Expose new functionality through an optional preload bridge method and, when applicable, a desktop feature flag. Keep existing bridge contracts compatible with older web bundles.
- Gate shell-only UI and requests on the actual bridge method and enabled feature flags; missing fields mean that shell path is unsupported. Check for independent server or remote-host paths before hiding a feature entirely.
- Test a capable shell, an older/partial bridge (including missing methods or fields), and any supported non-shell fallback. Do not show a control when none of its paths can work.

See `web/src/lib/nativeBridge.ts` for the web-side compatibility boundary.
