# Hello Page extension

Reference implementation of an Omnigent V1 browser extension. It contributes a
primary-sidebar item and an isolated page, follows the host theme, navigates
through the parent, and stores a visit count in extension-scoped IndexedDB.

The generated `dist/extension.js` and `dist/extension.css` files are committed so
the Python package is installable without Node.js. Rebuild them after changing
`web/src/`:

```bash
pnpm --filter @omnigent/extension-sdk build
pnpm --filter @omnigent/example-hello-extension build
```

Install the package into an Omnigent development environment and restart the
server so entry-point discovery runs again:

```bash
uv pip install -e examples/extensions/hello-page
uv run omnigent extensions doctor omnigent.hello-page
```

For a faster local UI loop, point the explicit development override at the
Python package root and restart the server after rebuilding:

```bash
export OMNIGENT_EXTENSION_DEV_BUNDLES="{\"omnigent.hello-page\":\"$PWD/examples/extensions/hello-page/src/omnigent_hello_extension\"}"
```

This override trusts local files and logs a warning at server startup. Never set
it from user-controlled configuration.
