---
name: investigate
description: Delegate read-only investigation, debugging, audit, search, or code-understanding tasks to sub-agents; synthesize only from their structured reports.
---

# investigate — delegated read-only work

Use for any read-only task: investigation, debugging, audit, search, code
understanding, architecture comparison, failure analysis, or answering a
repository-specific technical question.

## Procedure
1. Decompose the question into one or more bounded investigation tasks. Prefer
   two independent lenses for ambiguous or high-stakes questions.
2. Dispatch each task to `claude_code`, `codex`, or `pi`:
   `sys_session_send(agent="claude_code"|"codex"|"pi",
   title="explore-<task_slug>", args={purpose: "explore", input: "<question +
   exact scope + evidence requested>"})`. Use a task-based title such as
   `explore-ci-flake`, never the raw vendor name. Use `purpose: "search"` only
   when the task is primarily external/document search. Prefer `pi` when a
   third lens or a non-Claude/GPT model is wanted. Any worker takes an optional
   `args.model` (`sys_list_models` shows what each worker can run; an invalid
   model/worker combination fails loud at dispatch, and `model` only applies on
   the dispatch that CREATES the session — a send that continues an existing
   title rejects it).
   Tell the worker to edit nothing and return file,
   command, URL, or line evidence. Emit these `sys_session_send` calls in the
   SAME turn — do not end a turn having only said you will dispatch.
3. End your turn AFTER the dispatch tool calls are in flight (never before).
   Do not inspect files, logs, terminals, docs, or connector output yourself
   while the workers run.
4. When workers finish, collect their completion results with
   `sys_read_inbox`. Synthesize only from those inbox-delivered reports. Use
   `sys_session_get_history` only to debug an empty or unclear worker result; if
   reports conflict or are incomplete, dispatch a follow-up `explore` task
   rather than resolving the conflict from your own direct inspection.
5. If the investigation uncovers required code changes, switch to `fanout` /
   `cross-review`: dispatch an `implement` worker, then verify with the
   opposite-vendor `review` worker.

## Notes
- The orchestrator may use its own tools only to create task packets, maintain
  the registry, or check deterministic external status. It must not answer the
  user's substantive question from its own direct file reads, shell output,
  connector fetches, or terminal scrollback.
- Keep task scopes narrow enough that each worker can return a concise report
  with evidence. Broad investigations should be split into parallel subtasks.
