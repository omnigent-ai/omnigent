# omnigent-client

Python client SDK for the [omnigent](https://github.com/omnigent-ai/omnigent)
server API.

`omnigent-client` is a typed client for driving omnigent sessions over the
server's HTTP + SSE API — creating sessions, sending turns, and streaming
responses. It shares the `StreamEvent` / `SessionStreamEventType` types that the
server emits, so streamed envelopes are validated against a single source of
truth.

It is released in lockstep with the core `omnigent` package at a matching
version:

```bash
pip install omnigent-client
```

See the [omnigent repository](https://github.com/omnigent-ai/omnigent) for full
documentation.
