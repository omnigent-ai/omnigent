# Docloop notebook pane

The optional notebook pane edits the document used by an existing Docloop session.
Chat keeps its normal composer, model selection, turn stream and tool dispatcher.
On narrow screens, Chat and Notebook are separate views; on wider screens they
appear together. Switching views preserves the composer and unsaved notebook drafts.

```mermaid
flowchart LR
    Browser[Native chat and notebook pane] --> AP[Session permission check]
    AP --> Runner[Assigned runner tunnel]
    Runner --> Harness[Live Docloop harness]
    Harness --> Document[Shared notebook Store]
    Harness --> Executor[Existing workspace executor]
```

## Configuration

This is an opt-in source integration. The central server and runner need the
`docloop_notebook` entry in `OMNIGENT_FEATURES`. Other enabled entries remain in
that comma-separated list. The Docloop harness needs a release containing the
shared notebook binding, with `DOCLOOP_NATIVE_NOTEBOOK=1` in its launch environment.
It also uses its existing `DOCLOOP_DOCUMENT` and workspace configuration.

All flags default off. A pane request cannot initialize a new agent process or
select a runner. Start the session normally and send an initial instruction to
initialize its notebook. Existing runner and harness authentication are reused;
no additional browser token or notebook path is accepted.

## Save and execution behavior

Both reads and edits require the session's Edit permission. GET and PATCH on
`/v1/sessions/{id}/docloop/document` follow existing session affinity and the live
harness client. The central server has no second document Store. Limits apply
while reading requests and responses; relay redirects and automatic write retries
are disabled.

Edits carry the document binding, revision and a UUID change identifier. Stale
revisions retain the user's draft for comparison. A response lost after dispatch
leaves the save outcome unknown. The user can retry the identical save to obtain
its receipt; this is not an exactly-once delivery guarantee. Docloop retains a
bounded history of edit receipts. A committed edit whose refreshed snapshot is
unavailable is reported as applied, without suggesting a new change identifier.

Use Chat for arbitrary instructions to modify the document, execute cells, or
work with files. The pane is an addressed source editor for Org and ipynb. Full
JupyterLab and kernel controls remain available in Docloop's standalone browser
service; this host pane does not yet embed that Jupyter UI.

## Verification

The relay and permission tests use real host route factories and SQLite session
permissions. Cross-project Docloop acceptance uses the real SDK, Engine, Store,
local evaluator and both host relay adapters. Its provider and live process
registry entry are fixtures, and its HTTP hops use ASGI transports.

After adopting both reviewed components, open an initialized Docloop session,
select Notebook, change and save a note, then ask in Chat to use that note and
execute a saved cell. Confirm the output and created file, reload the page, and
verify that both survive. Check phone-width switching and a desktop split view.
Local source tests and UI previews do not establish a live deployment receipt.
