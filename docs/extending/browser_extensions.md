# Browser extensions

A V1 browser extension packages these files inside the Python import package
that owns its `omnigent.extensions` entry point:

```text
dist/extension.js
# optional
dist/extension.css
```

The files must be self-contained. Bundle all dependencies, including
`@omnigent/extension-sdk`; runtime imports, Node.js APIs, source maps, and
additional assets are not supported. The SDK is currently a private workspace
package for internal authors and is not published to npm.

The server reads and hashes the bundle once at startup. Digest URLs provide
immutable caching and detect a stale catalog across server restarts; they are
not a separate integrity control. Missing, oversized, escaping, or unowned
assets mark the extension unavailable and suppress its pages and navigation.

## Runtime boundary

The host injects the bundle into a `srcdoc` iframe with `sandbox="allow-scripts"`
without `allow-same-origin`. Its CSP denies network connections, forms, remote
images, ambient fonts, and base URL changes. Extensions may use inline styles,
`data:`/`blob:` images, and bundled frameworks.

The parent transfers one nonce-bound `MessagePort`. The protocol enforces
identity, API version, message budgets, method capabilities, cancellation,
timeouts, and disposal. Removing the iframe explicitly deactivates the SDK.

## SDK

```ts
import { defineExtension } from "@omnigent/extension-sdk";

defineExtension({
  async activate(context) {
    const theme = await context.theme.getCurrent();
    const value = await context.storage.user.get("key");
    await context.navigation.openPage("publisher.extension.page", {
      tab: "one",
    });
  },
  deactivate() {},
});
```

Available V1 methods:

- `navigation.openPage`, `openSession`, `openNewSession` (optionally filed
  under a `projectId`), and `openExternal` (only for URLs the host returned,
  such as a pull request) with the `navigation` permission;
- `theme.getCurrent` and `theme.subscribe`;
- `storage.user.get`, `set`, and `delete` with the `storage.user` permission;
- `sessions.listPage`, the SDK's `sessions.listAll` helper, and
  `sessions.pullRequest` (the PR filed from a session's branch, via the same
  GitHub lookup as the shell's GitHub tab) with the `sessions.read` permission;
- `projects.list` with the `projects.read` permission and `projects.create`
  with the `projects.write` permission.

The sessions API exposes only top-level, non-archived sessions the current user
can already read. Because operator-installed extension bundles are trusted code,
requesting `sessions.read` grants access to session titles and absolute working
directory paths visible to that user. Summaries contain ID, title, status, an `unread` flag (a finished turn the
current user has not viewed yet, using the sidebar's unread rule), a
`titleProvisional` flag (the title is the shell's first-message placeholder
until the server names the session), working
directory, worktree branch, project ID, and created/updated timestamps. Pages default to 25 rows and accept up to
1,000; the host shortens unusually large pages to stay within the RPC response budget. The SDK drains at most
200 pages or 5,000 sessions. Extensions receive neither raw
authenticated fetch nor the internal session WebSocket.

The projects API returns the current user's projects as ID, name, and icon.
`projects.create` makes an empty project from a trimmed name of at most 100
characters — the same operation as the sidebar's **New project** button — and
the host refreshes the shell's project list afterwards.

Storage uses parent-owned IndexedDB, not `localStorage`: 32 KB per value,
256 KB and 128 keys per extension namespace. Writes are paced and quota errors
are explicit; data is not evicted or automatically removed on uninstall.
Standalone scopes storage by server origin and resolved user. Embedded hosts
must provide a stable `serverIdentity`; storage reports `Unavailable` otherwise.

## Development override

`OMNIGENT_EXTENSION_DEV_BUNDLES` is an operator-only JSON map from extension ID
to an existing absolute package root. It bypasses installed-package ownership,
logs a startup warning, and is intended only for local rebuild loops:

```bash
export OMNIGENT_EXTENSION_DEV_BUNDLES='{"publisher.extension":"/absolute/package/root"}'
```

The iframe document inherits any future parent CSP. Changes to the app-wide CSP
must therefore include extension-host compatibility tests.
