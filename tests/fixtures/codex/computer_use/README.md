# Codex Computer Use fixtures

These fixtures were recorded from Codex CLI/app-server `0.145.0` with the
`computer-use@openai-bundled` plugin `1.0.1000621`, through an Omnigent-native
session on macOS. The target was a blank TextEdit window.

Sanitization preserves field names and JSON types while replacing thread, turn,
and item identifiers and lifecycle timestamps. Image base64 is replaced with
`<base64 omitted>` and its original encoded length is recorded in the
fixture-only `dataEncodedLength` field. A long TextEdit accessibility tree is
replaced with a marker and its original character count.

The subscribed client observed no `item/mcpToolCall/progress` notifications in
the successful image or interrupted calls. The interrupted call emitted
`item/started` and a terminal `turn/completed` with `status: "interrupted"`, but
no `item/completed`; `thread/read` continued to expose the item as
`status: "inProgress"`.

These payloads contain no credentials, local paths, proprietary runtime code, or
raw screen bytes.
