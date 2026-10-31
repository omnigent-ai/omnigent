# omnigent-client

Python client SDK for the [omnigent](https://github.com/omnigent-ai/omnigent)
server API.

`omnigent-client` is a typed client for driving omnigent sessions over the
server's HTTP + SSE API — creating sessions, sending turns, and streaming
responses. It owns the SSE wire schemas (`omnigent_client.wire`) that the
server re-exports and emits, so streamed envelopes are validated against a
single source of truth.

It depends only on `httpx` and `pydantic`, not on the `omnigent` server
package, so it can be installed on its own in a CI job, an Airflow worker, or
another remote orchestrator:

```bash
pip install omnigent-client
```

Compatibility:

- Python 3.12 or newer, the same range CI tests.
- It is released in lockstep with the core `omnigent` package at a matching
  version. A client works best against the server release with the same
  version.
- Stream events whose `type` the client does not know (for example, from a
  newer server) are logged at debug level and skipped, so the rest of the
  stream still arrives.
- `LocalServer` starts `omnigent server` in a subprocess, so it needs the
  `omnigent` package installed alongside the client.

See the [omnigent repository](https://github.com/omnigent-ai/omnigent) for full
documentation.
