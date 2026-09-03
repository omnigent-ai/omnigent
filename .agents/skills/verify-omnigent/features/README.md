# Omnigent verification feature map

The primary surface is the web UI backed by the real Omnigent server and runner.
Desktop and mobile wrap or reuse this UI; the CLI and SDKs are additional
surfaces. Drive every affected entry point, not only the easiest one.

| Feature | User entry points | Primary proof |
|---|---|---|
| [Chat and streaming](chat-and-streaming.md) | Existing chat, new-chat prompt, resumed session | User message and streamed assistant response render and persist |
| [Start and manage sessions](session-lifecycle.md) | Landing composer, sidebar, fork/clone/switch | Correct session is created, selected, restored, and stopped |
| [Approvals and policies](approvals-and-policies.md) | Chat card, inbox, standalone approval, agent info | Visible decision drains the real waiting request with correct scope |
| [Files and comments](files-and-comments.md) | Workspace rail, file viewer/editor, comment inbox | Visible edit/comment survives read-back or reload |
| [Collaboration and access](collaboration-and-access.md) | Share modal, link, collaborator session | UI affordance and server authorization agree at every permission level |
| [Optional capabilities](optional-capabilities.md) | Projects, Automations, managed hosts, routing controls | `/v1/info`, visible UI, and callable endpoints agree |
| [Harnesses and clients](harnesses-and-clients.md) | Harness picker, CLI, resumed cross-client session | Real adapter turn persists and renders with clean teardown |

Cross-cutting checks:

- Run chat smoke after changes to API events, SSE, reducers, runner protocol, or
  server startup.
- Run both web and Python reducer tests when event/block semantics change.
- Exercise mobile/desktop wrappers when changing routing, deep links,
  notifications, service workers, or host integration.
- For user-visible performance, compare the same journey's timings and request
  waterfall before and after. Functional success alone is insufficient.
