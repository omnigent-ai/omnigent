---
name: thingy
description: Build, change and run thingies, the small web apps Omnigent shows beside sessions. Use before any sys_thingy_* work, in a thingy session, and whenever a message starts with "Thingy action".
user-invocable: false
---

# Thingies

A thingy is a small web app (a board, a tracker, a report) that people use in
the browser beside their sessions. It keeps JSON records in collections with
rules, its buttons hand actions to an agent session, and people leave comments
on it. You work on it with the `sys_thingy_*` tools.

Start with `sys_thingy_describe`: it returns the thingy's guide, collection
schemas, rules and declared actions, or says nothing is published yet.

## Working as a thingy session

A thingy session is the session a person sends every action and change request
for one thingy to.

- Read the action ledger with `sys_thingy_action_list` before anything else,
  and again after a restart or compaction. The ledger, not this conversation,
  records what is done. Actions in it that are `pending` or `queued` are yours
  to carry out, even when no message has brought them: one requested before
  this session existed reaches you only through the ledger.
- Action requests arrive as messages that start with `Thingy action`, followed
  by a JSON object with `action_id`, `action` and `params`. For each one:
  1. Set it `running` with `sys_thingy_action_update`.
  2. Carry it out as the action's description in `sys_thingy_describe` says.
  3. Set it `done` or `failed` with a short Markdown `summary`.

  Every update needs the action's current version as `if_version`. Take it from
  `sys_thingy_action_list` for the first update, then use the `version` that
  each update returns. Read the ledger again only when an update fails with
  `CONFLICT`.
- Stay free to take the next request. Hand long or independent work to a child
  session (`sys_session_create`) with the `action_id` and these reporting
  rules, and record that session on the action (`session_id` in
  `sys_thingy_action_update`).
- Run actions that touch the same records one at a time.
- A second message with an `action_id` you already have is the same request
  sent again: do not run it twice. A request with a new `action_id` that repeats
  one still queued or running: set the new one `done` with a plain summary such
  as "Merged into the scan already running", without action IDs, since the page
  shows it to the viewer.
- The page shows each action's status, which Omnigent derives from the sessions
  doing the work. An action whose sessions stop without your report shows as
  `finished_unreported`, so always report how it ended.
- Comments: read them with `sys_thingy_comment_list` (each has the commented
  text in `anchor.text`, and `anchor.ref` naming the record when the page set
  one), address the `draft` ones by changing the thingy or its records, then mark
  each `addressed` with `sys_thingy_comment_update`.

## Records

`sys_thingy_records` reads and changes a published thingy's records, one
collection at a time:

| `op` | Arguments | Result |
| --- | --- | --- |
| `get` | `id` | `{"record": record}`, `{"record": null}` when it does not exist |
| `list` | `limit` (1–200), `cursor` | `{"items": [record, ...], "next_cursor": string or null}`, ordered by ID |
| `create` | `value`, optional `id` | the record |
| `replace` | `id`, `value` (the whole record), `if_version` | the record |
| `delete` | `id`, `if_version` | `{"id", "version", "deleted": true}` |

A record is `{"id", "value", "version", "created_at", "updated_at",
"created_by", "updated_by"}`. Pass a stable `operation_id` for each change and
reuse it when you retry, so the change applies once; a `create` without `id`
gets an ID derived from it.

- `CONFLICT`: the record changed since you read it. Read it again and retry.
- `CONSTRAINT_VIOLATION`: the write would break a rule; `details.constraint`
  names it. Data that already breaks a rule can still be edited toward it.
- `INVALID_ARGUMENT` with `details.path`: the value does not match its schema.
- `UNAVAILABLE` with `details.reason` `busy`: other writers held a collection
  with rules; nothing changed, so retry with the same `operation_id`.

Do not edit or delete records unless the person asks or an action says so.

## Building or changing the thingy

1. For an existing thingy, read the published revision with
   `sys_thingy_revision_get` and change it.
2. Upload a revision with `sys_thingy_revision_create`: `html`, `sdk_version`
   `1`, `capabilities`, `collections`, and optionally `guide`, `constraints`
   and `actions`.
3. Publish it with `sys_thingy_publish`, passing the thingy's current `version`
   from `sys_thingy_get` as `if_version` (`null` for its first publication). A
   stale version fails with `CONFLICT`. Publishing keeps the records.
4. Once you know what a new thingy is for, give it a short name with
   `sys_thingy_rename`. Then tell the person what you built and what they could
   ask for next.

For a new thingy, seed clearly labeled synthetic sample data so it works right
away (from the page on first load, or with `sys_thingy_records`). Do not use
real data unless the person gives it to you.

### What a revision declares

- `capabilities`: only what the page uses, from `data.read`, `data.write` and
  `sessions.read`. Actions need no capability, only an entry in `actions`.
- `collections`: collection name (`[a-z0-9][a-z0-9_-]*`, at most 16) to the
  JSON Schema each record must match. Supported keywords: `type`,
  `properties`, `required`, `additionalProperties`, `items`, `enum`,
  `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `minLength`,
  `maxLength`, `minItems`, `maxItems`, `minProperties`, `maxProperties`,
  `title`, `description`, `default` and `$schema`. `format`, `pattern`, `$ref`
  and combinators such as `oneOf` are refused; leave them out and keep the
  other checks. A published collection's schema cannot change: add a new
  collection instead.
- `constraints`: rules across records, each with a `name`, a `kind` and an
  optional `message` shown when a write breaks it:
  - `max_records`: `collection`, `max`, optional `where` (`{field: value}`).
  - `unique`: `collection`, `fields` (1–3 top-level fields).
  - `reference`: `collection`, `field`, `target` (a collection), `on_delete`
    `restrict`, `cascade` or `unset`.
- `actions`: what a person can ask the thingy session for, each
  `{"name", "description", "params"}`. `name` is like `run_card`;
  `description` tells the agent what to do; `params` is an optional JSON Schema.
- `guide`: Markdown for agents: what the collections mean, conventions and
  common tasks.

### Rules for the page

The page is one self-contained HTML document with inline script and styles. It
runs in a sandboxed frame without an origin of its own and without network
access, which silently blocks:

- form submission: use `type="button"` buttons with click handlers, and handle
  Enter in inputs with `keydown`;
- `alert`, `confirm` and `prompt` (`confirm` returns false): ask inline;
- `localStorage`, `sessionStorage` and cookies, which throw: keep state in
  memory or in records;
- `window.open` and links that navigate: use `client.navigation`;
- any network request, external script, font or image: inline them or use
  `data:` URIs.

And:

- Every data call goes to the server and can take a second or more. From the
  click until the call returns, disable the control and show a pending state (an
  added item appears as "Adding…" at once), then update the page from the
  returned record. Show failures next to the item with a retry.
- Pick up other people's changes with `collection.watch`, not tight polling.
- For an action, follow it with `client.agent.watch`. Keep a spinner only while
  its status is `pending`, `queued`, `running` or `needs_input`. `done`,
  `failed`, `cancelled`, `finished_unreported` and `interrupted` mean it ended,
  even when the session never reported, so stop the spinner, show the
  `summary`, and offer a retry after `interrupted`. Treat any other status as
  ended.
- Put `data-omnigent-ref` (for example `cards/c1`) and `data-omnigent-label`
  on each record's element, so a selection or comment names the exact record.
- Others may see the thingy read-only. Put `data-omnigent-requires="write"` on
  every control that changes records and `data-omnigent-requires="action"` on
  every control that requests an action; the SDK disables them for people
  without that access and shows why.

## SDK reference

`window.omnigent` is defined before the page's scripts run. Types below are
TypeScript; every promise rejects with an `OmnigentError`.

```ts
window.omnigent.connect(options?: { timeoutMs?: number }): Promise<Client>
window.omnigent.isOmnigentError(error: unknown): boolean

interface Client {
  context: {
    app: { id: string; name: string };
    revisionId: string;
    viewer: { id: string; displayName: string }; // store viewer.id, not the name
    permissions: { writeData: boolean; publish: boolean };
    preview: boolean; // template preview: data in memory, actions off
  };
  capabilities: string[];
  hasCapability(capability: string): boolean;
  can(requirement: "write" | "action"): boolean;
  data: { collection(name: string): Collection };
  agent: Agent;
  navigation: Navigation;
  sessions: Sessions; // needs the sessions.read capability
  ui: {
    colorScheme: "light" | "dark";
    blockedReason(requirement: "write" | "action"): string | null;
  };
  dispose(): void;
}

// Every call also takes { signal?: AbortSignal; timeoutMs?: number } in its options.
interface Collection {
  get(id: string): Promise<Doc | null>;
  list(page?: { limit?: number /* 1–100 */; cursor?: string }):
    Promise<{ items: Doc[]; nextCursor: string | null }>;
  create(value: Json, options?: { id?: string; idempotencyKey?: string }): Promise<Doc>;
  replace(id: string, value: Json, options: { ifVersion: string; idempotencyKey?: string }):
    Promise<Doc>; // the whole value, not a merge
  delete(id: string, options: { ifVersion: string; idempotencyKey?: string }):
    Promise<{ id: string; version: string; deleted: true }>;
  // Polls the first page; calls listener with each snapshot, starting with "loading".
  watch(listener: (snapshot: {
    status: "loading" | "live" | "reconnecting" | "stale";
    items: Doc[]; nextCursor: string | null; updatedAt: string | null;
    error: OmnigentError | null;
  }) => void, options?: { intervalMs?: number /* ≥ 1000 */; limit?: number }):
    { dispose(): void };
}

interface Doc {
  id: string; value: Json; version: string; // pass version back as ifVersion
  createdAt: string; updatedAt: string; // ISO 8601
  createdBy: string; updatedBy: string;
}

interface Agent {
  // Call from a click handler inside the page; needs Can edit access.
  request(action: string, params?: object):
    Promise<{ actionId: string; delivery: "sent" | "starting" | "merged" }>;
  progress(actionIds?: string[]): Promise<{ items: ActionProgress[] }>;
  // The listener comes first and the IDs go in options. Each snapshot lists every
  // watched action: find yours in snapshot.items by id. Not an async iterator.
  watch(listener: (snapshot: {
    status: "loading" | "live" | "reconnecting" | "stale";
    items: ActionProgress[]; updatedAt: string | null; error: OmnigentError | null;
  }) => void, options?: { actionIds?: string[]; intervalMs?: number }):
    { dispose(): void; refresh(): void };
}

interface ActionProgress {
  id: string; // the actionId from request
  status: string; // pending, queued, running, needs_input, done, failed, cancelled,
                  // finished_unreported, interrupted, not_found
  reason: string | null; action: string | null; params: object | null;
  summary: string | null; // the agent's Markdown summary
  sessionId: string | null; sessionIds: string[];
}

// Each opens a page after a click inside the app; display "tab" (default), "panel" or "page".
interface Navigation {
  newSession(params?: { prompt?: string; display?: string }): Promise<{ opened: true }>;
  openSession(sessionId: string, options?: { display?: string }): Promise<{ opened: true }>;
  newAutomation(params: { name: string; prompt: string; schedule?: string; display?: string }):
    Promise<{ opened: true }>;
}

interface OmnigentError extends Error {
  code: string; // CONFLICT, CONSTRAINT_VIOLATION, NOT_FOUND, PERMISSION_DENIED,
                // INVALID_ARGUMENT, UNAVAILABLE, REVISION_CHANGED, ...
  retryable: boolean;
  operationId?: string; // for a write: pass it back as idempotencyKey to retry
  requestId?: string; // for logs; keep it out of messages
}
```

`REVISION_CHANGED` means a new version was published; reload the page. For a
write that failed with `UNAVAILABLE`, retry with `idempotencyKey` set to the
error's `operationId`.

## Example page

A list with an add field and one action button that follows its action. Upload
it with `capabilities` `["data.read", "data.write"]`, collection
`{"ideas": {"type": "object", "required": ["title"], "additionalProperties":
false, "properties": {"title": {"type": "string", "minLength": 1,
"maxLength": 200}}}}` and action `{"name": "summarize", "description":
"Summarize the ideas; put the summary on the action."}`.

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font: 14px system-ui, sans-serif; margin: 16px; }
  .pending { opacity: 0.6; }
  .error { color: #b00020; }
</style>
</head>
<body>
<h1>Ideas</h1>
<input id="title" placeholder="New idea" data-omnigent-requires="write">
<button type="button" id="add" data-omnigent-requires="write">Add</button>
<p id="error" class="error" role="alert"></p>
<ul id="list"></ul>
<button type="button" id="summarize" data-omnigent-requires="action">Summarize</button>
<p id="status" role="status"></p>
<script>
(async () => {
  const $ = (id) => document.getElementById(id);
  const OPEN = ["pending", "queued", "running", "needs_input"];
  const client = await window.omnigent.connect();
  const ideas = client.data.collection("ideas");
  let docs = [];
  const adding = [];

  function render() {
    const rows = docs.map((doc) => {
      const li = document.createElement("li");
      li.textContent = doc.value.title;
      li.dataset.omnigentRef = "ideas/" + doc.id;
      li.dataset.omnigentLabel = doc.value.title;
      return li;
    });
    for (const title of adding) {
      const li = document.createElement("li");
      li.className = "pending";
      li.textContent = title + " (Adding…)";
      rows.push(li);
    }
    $("list").replaceChildren(...rows);
  }

  ideas.watch((snapshot) => {
    if (snapshot.status === "live") {
      docs = snapshot.items;
      render();
    }
  });

  async function add() {
    const title = $("title").value.trim();
    if (!title) return;
    $("title").value = "";
    adding.push(title);
    render();
    try {
      const doc = await ideas.create({ title });
      docs = [...docs.filter((item) => item.id !== doc.id), doc];
      $("error").textContent = "";
    } catch (error) {
      $("error").textContent = "Could not add “" + title + "”: " + error.message;
    } finally {
      adding.splice(adding.indexOf(title), 1);
      render();
    }
  }
  $("add").addEventListener("click", add);
  $("title").addEventListener("keydown", (event) => {
    if (event.key === "Enter") add();
  });

  let following = null;
  $("summarize").addEventListener("click", async () => {
    $("summarize").disabled = true;
    $("status").textContent = "Requesting…";
    try {
      const { actionId } = await client.agent.request("summarize", {});
      if (following) following.dispose();
      following = client.agent.watch((snapshot) => {
        const progress = snapshot.items.find((item) => item.id === actionId);
        if (!progress) return;
        const open = OPEN.includes(progress.status);
        $("summarize").disabled = open;
        $("status").textContent = open
          ? progress.status + "…"
          : progress.status + (progress.summary ? ": " + progress.summary : "");
        if (!open) following.dispose();
      }, { actionIds: [actionId] });
    } catch (error) {
      $("status").textContent = "Could not request it: " + error.message;
      $("summarize").disabled = false;
    }
  });
})();
</script>
</body>
</html>
```
