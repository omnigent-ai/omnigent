# Fork Discoverability Design

## Goal

Make conversation forking discoverable from session menus and keep the final
message's actions visible without requiring hover.

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

The transcript identifies the final real message bubble, considering user and
assistant bubbles while ignoring routing and compaction markers. That bubble's
action footer remains visible without hover.

For a final user bubble, the footer remains hover-only while the agent is
working and becomes persistent once activity ends, including after a failed
turn. A final assistant bubble is persistent; Fork itself remains unavailable
while that response is streaming, preserving the current safety gate.

All earlier message footers keep the existing hover/focus behavior.

## Testing

Component tests will cover:

- Fork in the sidebar kebab and right-click menu, opening the existing dialog
  for the selected row.
- Fork in the owner header menu and the fallback header menu.
- Persistent actions on the final assistant bubble.
- Persistent actions on a settled final user bubble, but not while work is in
  progress.
- Hover-only actions on earlier messages.

No API or server changes are required.
