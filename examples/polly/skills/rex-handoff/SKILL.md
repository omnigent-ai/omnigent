---
name: rex-handoff
description: Inventory Rex sessions across configured machines and recover unfinished coding work for an Omnigent handoff.
---

# Rex handoff

Read the locally installed Rex guide with `rex __internal skill --feature-session-naming=false`. If unavailable, start with `rex whoami --json` and the installed CLI's help. Use configured authenticated connections. Verify server identity before relying on a result. Rex TUI workspace endpoints can differ from the CLI host list. A connected client describes a connection to a server. Count sessions by verified server and report unreachable hosts separately.

For a possible handoff, recover the last concrete result and confirm the task, checkout, branch, revision, and dirty state. Check the current PR and default branch for prior shipment or a newer owner. A quiet pane or a quota error alone is insufficient. Preserve the original pane and checkout. Establish that the receiving runner can access the exact revision and any required patch before assigning it. Report the exact missing artifact or access blocker when that fails.

Use Polly's Omnigent delegation path for coding work. Give the receiving worker the project, verified source revision, last completed gate, expected outcome, and next check. Record the new session so the same work is assigned only once.
