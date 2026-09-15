# Chat and streaming

## Sub-features

- Composer send, queued/steered messages, stop and resume
- SSE history hydration and live assistant output
- Approval, elicitation, tool, reasoning, and native-harness blocks
- Python SDK and TypeScript reducer behavioral parity

## How to get to it

Open an existing `/c/{session_id}` chat or type the first prompt on `/`. Send a
message from the composer and observe user and assistant bubbles.

## Driving it with Playwright

Start with `scripts/verify.sh smoke`. Use the composer placeholder
`Ask the agent anything…`, the exact `Send` button name, and
`[data-testid="message-bubble"][data-role="assistant"]`. Assert non-empty
assistant content, then reload and verify history when persistence is affected.
Use the specialized tests under `tests/e2e_ui/chat`, `messages`, and `approvals`
for changed event types.

The observable end state is a rendered user action and assistant result backed
by the session snapshot or SSE stream, not a spinner. For protocol changes,
also prove a supported older client can ignore new fields or events safely.

## Gotchas

- The "Working…" shimmer is not an assistant response.
- The UI reducer mirrors Python SDK behavior but has intentional web-only
  context fields; preserve behavior, not byte-for-byte structure.
- Mock the LLM boundary, not the server, runner, stream, or reducer.
- A same-version pass does not prove staggered client/server rollout safety.
