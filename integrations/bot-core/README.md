# omnigent-bot-core

The Omnigent client the chat-bot integrations share: SSE event parsing, the
HTTP/SSE client and its turn loop, and the browserless login flows.

`omnigent-slack` and `omnigent-discord` both depend on this package. Neither
imports `omnigent` core — a bot drives the server over HTTP, so it needs the
API contract, not the server implementation. That constraint is why this is a
separate distribution rather than part of `omnigent-client`, which does depend
on core.

| module | responsibility |
| --- | --- |
| `events.py` | Pure SSE parsing, event DTOs, and extractors. No I/O, no state. |
| `omnigent.py` | `OmnigentClient`, the connection pool, the `run_turn` stream loop and turn-end detection, and the error subclasses. |
| `oauth.py` | Device-grant and OIDC-ticket login, token refresh and revocation. |

Nothing here knows what a Slack thread or a Discord channel is. Anything that
does belongs in the bot package.

## Why it exists

These three modules were duplicated between the two bots. The cost showed up
immediately: a turn-hang fix — a harness that cannot launch leaving the user on
an unchanging placeholder for the full idle grace — landed in one copy and left
the other broken, because nothing tied them together. One copy, one fix.

## Tests

The tests for this code live here, next to it, rather than being repeated in
each bot's suite:

```bash
uv run --no-sync pytest integrations/bot-core/tests
```
