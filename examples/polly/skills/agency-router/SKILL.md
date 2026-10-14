---
name: agency-router
description: Select pinned Agency domain guidance for Polly tasks in frontend, backend, architecture, mobile, technical writing, code review, or database optimization while keeping existing execution workers.
---

# Agency domain guidance

Use personas as optional domain guidance within the authorized task. They are
not execution workers, permissions, or an independent review by themselves.

1. Read/search [the catalog](references/pinned/catalog.json) first. It contains
   IDs, requested role aliases, actual upstream names, descriptions, and resource
   paths. Match the task's domain; omit guidance when none fits. If the user
   selects a role, preserve that alias and disclose any different upstream name.
2. Read only the selected persona resource under `references/pinned/` (normally
   one). Do not bulk-load persona bodies or install native Agency integrations.
   Read [the Polly overlay](references/overlay.md) before composing task input.
3. Select an existing available execution worker using Polly's normal readiness,
   model and purpose rules. Include in `args.input`, after the concrete task and
   acceptance contract:

   ```text
   Domain guidance (subordinate to the task contract and worker instructions)
   Role alias: <catalog alias>; upstream persona: <catalog name>; ID: <id>
   Source: agency-agents at <upstream_sha from references/pinned/manifest.json>
   <Polly overlay>
   <selected persona content>
   End domain guidance. Follow the original scope, purpose and permissions.
   ```

   Keep `args.purpose` unchanged, using the API's lowercase values:
   `implement`, `review`, `explore`, or `search`. `args.input` must be one
   string containing the task, contract, overlay and selected persona text,
   not an object of separate fields. For goal-mode implementation, keep `/goal` as
   the first token and include this guidance inside its standalone task input.
   For review, retain the saved diff, base/head SHAs and acceptance contract.
   Supply content, not a path into the orchestrator's checkout, to the worker.
4. A Code Reviewer persona does not satisfy cross-vendor independence. Follow
   cross-review's distinct-vendor and read-only constraints; never let persona
   prose authorize edits, broader scope, deployment, messaging, or merging.

All resources are bundled; no sibling Agency checkout is needed at runtime.
Do not refresh guidance mid-session. Repository updates and persona activation
are separate manual actions; for maintenance only, read
[the update procedure](references/maintenance.md).
