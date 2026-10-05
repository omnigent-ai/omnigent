# Host process extensions

A trusted, installed Python package can add host-local background work. This
interface is separate from the server and browser extension catalog.

Register a no-argument `HostExtension` implementation in the
`omnigent.host_extension` entry-point group:

```toml
[project.entry-points."omnigent.host_extension"]
example = "example_host.extension:ExampleHostExtension"
```

Select it before starting the host, including when the CLI starts a host daemon:

```bash
OMNIGENT_HOST_EXTENSION=example omni host
```

The selector must match exactly one installed entry point. Without it, the host
does not load an extension. Entry-point discovery, import, and construction run
synchronously before the host event loop; keep imports and constructors light.

`HostExtension` defines async `start()` and `stop()` callbacks and an
`owned_pids` property. Return the PIDs of direct children whose exit status
the extension will collect. The host excludes those PIDs from its orphan reaper.

The optional synchronous `before_runner_spawn()` callback receives the
server-provided session ID and canonical harness (if present), the validated
workspace, token-bound runner ID, and server URL.
It runs on the runner-spawn worker thread immediately before the runner starts.
Keep it short and local; exceptions are logged and the runner still starts.

The host allows one second for `start()` before connecting and five seconds for
`stop()` during graceful in-process shutdown. It logs and cancels callbacks
that exceed those budgets. Callbacks must keep the event loop responsive and
honor cancellation at await points; an async timeout cannot interrupt blocking
synchronous work. A failed start schedules cleanup without delaying connection.

`stop()` also runs after a failed start. SIGTERM, including the signal sent by
`omni host stop`, terminates the process without invoking `stop()`. Design child
processes and OS resources to handle that exit path.
