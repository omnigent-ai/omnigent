# Design page server deck index: implementation plan

Contract: `designs/DESIGN_PAGE.md`, `## Later phases` > `### Server deck index`.
Only that section is in scope. `kind` accepts `deck` and `wireframe`.

## Shape

- **Kind helper** `omnigent/entities/design_artifact.py`: `DesignArtifact`
  dataclass and `design_artifact_kind(path)` (`.slides.html` is `deck`,
  `.wireframe.html` is `wireframe`, else `None`). Shared by runner and server.
- **Table** `design_artifacts` on `ConversationBase` (same DB as
  `conversations`, so split-DB `create_all` covers it): `workspace_id`,
  `session_id` (`Uuid16`), `path` (`String(512)`), `kind` (`String(16)`),
  `updated_at` (int epoch), `deleted` (bool). PK `(workspace_id, session_id,
  path)`, index `ix_design_artifacts_session (workspace_id, session_id)`, no
  foreign key. Alembic revision on top of `mm1a2b3c4d5e`.
- **Store** (conversation store, `_conv_session` named sessions):
  `record_design_artifact(session_id, path, kind, deleted)` (upsert live;
  `deleted=True` only marks an existing row), `list_design_artifacts(kind)`
  (live rows, newest first), `replace_design_artifacts(session_id, paths)`.
  `list_conversations` gains `conversation_ids` so the list endpoint reuses its
  ACL and archived/kind filters unchanged.
- **Runner**: `publish_design_artifact_change` beside
  `_maybe_signal_changed_files` in `runner/tool_dispatch.py`; called next to
  every `record_change` (sys_os_write, sys_os_edit, file PUT/PATCH/DELETE
  routes, native file observer). Emits
  `{"type": "session.design_artifact.changed", "session_id", "path",
  "deleted"}` with the workspace-relative path.
- **Server relay**: the runner relay consumes the event and calls
  `session_live_state.persist_design_artifact`, which submits the store write
  on the ordered off-loop worker (same as `persist_pending_count`). Enabled
  only when the `design` flag is on (`configure(..., design_index=...)`).
  The file read route marks a deck deleted when the runner answers 404.
- **Routes** `omnigent/server/routes/design.py`, behind `design` (404 when off):
  `GET /v1/design/artifacts?kind=deck|wireframe` and
  `PUT /v1/sessions/{id}/design-artifacts` (`{"paths": [...]}`, edit access).
- **Web**: `fetchDesignIndex` (null on 404) and `reconcileDesignIndex` in
  `designDeckApi.ts`; `indexDesignGroups` in `designDecks.ts` builds groups for
  workspaces the live scan does not cover, marked unavailable. The landing
  queries the index first; without it the phase 1 scan runs unchanged. With
  it, only live sessions are scanned and each successful scan is reconciled.

## Tasks (tests first, one commit each)

1. Plan (this file).
2. Entity, model, migration, store methods, `conversation_ids` filter.
   Tests: `tests/stores/test_design_artifacts.py`,
   `tests/db/test_migration_design_artifacts.py`.
3. Runner emits the event from every write path. Tests: dispatch write/edit in
   `tests/runner/test_runner_dispatch.py`, routes in
   `tests/runner/test_environment_filesystem.py`, native observer.
4. Server relay persist, flag wiring, 404 delete on file read. Tests:
   `tests/server/test_session_live_state.py`, relay and read route tests.
5. List and reconcile routes, mounted in `app.py`, `openapi.json` regenerated.
   Tests: `tests/server/routes/test_design_artifacts.py` (owner, shared, not
   shared, flag off, reconcile replace).
6. Web index-first landing with fallback. Tests: `designDeckApi.test.ts`,
   `designDecks.test.ts`, `DesignPage.test.tsx`.
7. Feature map and spec status notes.

## Known ceilings

- The list reads every live row in the workspace (no paging); add a cursor when
  a workspace holds thousands of decks.
- Upsert is `session.merge`; concurrent first writes of one path from two
  replicas can raise and drop one best-effort write.
