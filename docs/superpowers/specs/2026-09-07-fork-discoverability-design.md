# Fork Discoverability Design

## Goal

Make conversation forking discoverable from session menus and keep the final
assistant response's actions visible once the session is confirmed idle.

## Session menus

Add a `Fork` item with the existing fork icon to both sidebar menu variants and
the header session menu. Selecting it opens the existing fork dialog as a
full-history fork, which copies through the source's last saved message.

Fork remains available to any viewer with read access, including shared and
child sessions, matching the current per-message action. If a session does not
have the owner-management header menu, the header exposes a minimal actions
menu containing Fork.

The sidebar row owns its fork-dialog open state and passes the row's existing
session metadata to `ForkSessionDialog`. The active session's header continues
to use the AppShell-owned fork dialog.

## Message actions

The transcript identifies the final real message bubble while ignoring routing
and compaction markers. Its action footer remains visible without hover only
when that message is an assistant response and the session status is exactly
`idle`.

User-message actions always remain hover-only. Assistant actions also remain
hover-only during loading, startup, streaming, waiting, and failure states.
Fork itself remains unavailable while a response is streaming, preserving the
current safety gate.

All earlier message footers keep the existing hover/focus behavior.

## Testing

Component tests will cover:

- Fork in the sidebar kebab and right-click menu, opening the existing dialog
  for the selected row.
- Fork in the owner header menu and the fallback header menu.
- Persistent actions on the final assistant bubble only while the session is
  confirmed idle.
- Hover-only actions on a final user bubble and during non-idle session states.
- Hover-only actions on earlier messages.

No API or server changes are required.
