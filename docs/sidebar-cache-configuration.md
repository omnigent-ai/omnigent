# Sidebar cache configuration

Embedded hosts can pass `sidebarConfig` to `OmnigentApp`. The prop accepts a
`Partial<SidebarConfig>` and can change at runtime. A host that renders
`SidebarDataProvider` directly passes a complete configuration through its
`config` prop. Defaults live in `web/src/appConfig.ts`.

```tsx
<OmnigentApp
  {...hostProps}
  sidebarConfig={{
    sharedAvailable: true,
    inboxIncludesShared: false,
    pinsIncludeShared: false,
    mineRefreshMs: 60_000,
    sharedRefreshMs: 180_000,
  }}
/>
```

With this configuration:

| Selected view | Mine cache | Shared cache |
| --- | --- | --- |
| My sessions | Active | Inactive |
| All sessions | Active | Active; merged with Mine |
| Shared sessions | Active for Inbox | Active; displayed |
| Archived sessions | Active for Inbox | Inactive |

`sharedAvailable` controls whether Shared can be used at all. Set it to `false`
to hide the Shared view and disable Shared inclusion in all consumers.
Otherwise, the mounted sidebar's persisted All/Shared selection activates the
Shared list. Unmounting the sidebar releases that demand.

`inboxIncludesShared` also activates Shared when enabled. When false, Inbox's
rows, comments, badge, loading, errors, retry, and pagination use owned sessions
only, including loaded owned pins and project-folder rows.

`pinsIncludeShared` controls the separate pinned query. Pins never activate the
general Shared list. When false, pins request only `pinned=true&visibility=mine`;
cached rows, optimistic updates, and legacy migration respect ownership.

All three booleans default to `true`, preserving the OSS Inbox and pin behavior.
To avoid Shared-list requests in My sessions, set `inboxIncludesShared: false`.
Set `pinsIncludeShared: false` as well to avoid the separate shared-pin request.

Directory-conflict warnings in new, clone, and resume dialogs read the loaded
Mine cache, including pages added with Load more. They make no separate session
list request and do not activate Shared. These advisory warnings cover only
loaded owned sessions; unloaded and shared sessions are outside their coverage.

## Display pagination

Set `sharedDisplayPageSize: 30` to show at most 30 rows in the Shared view's
Sessions section on entry, with a manual **Load more** button. This disables
automatic scrolling loads for that view. Each click reveals up to 30 additional
cached rows; when none remain hidden, it fetches the next backend page (`sessionPageSize`, default 30).
Pins and project folders keep their independent display and pagination.

```tsx
<OmnigentApp
  {...hostProps}
  sidebarConfig={{ sharedDisplayPageSize: 30 }}
/>
```

`displayPageSize` applies to all sidebar session views; `sharedDisplayPageSize`
overrides it for Shared. Both accept positive integers or `false`. Omitted
settings preserve the existing automatic loading behavior; `false` explicitly
disables the display limit. Changes can be passed at runtime through the embed
or provider, and standalone defaults can be set in `web/src/appConfig.ts`.

The visible window resets on view entry or a page-size change. Background
refresh keeps the number of rows the user has revealed, while updating and
reordering their contents. Display limits do not shrink caches, change API
page sizes or refresh caps, or restrict Inbox, pins, or agent discovery.

## Refresh and pagination

Mine and Shared polling default to 60 and 180 seconds. Set either interval to
`false` to disable its timer. Initial loading, explicit refresh, and view-entry
refresh remain enabled. Standalone builds also accept `VITE_MINE_REFRESH_MS`
and `VITE_SHARED_REFRESH_MS`; the strings `false` and `0` disable polling.

Inactive Shared retains its data but stops polling, focus/reconnect/invalidation
refetches, and pending pagination. Its rows leave background subscriptions.
Re-entry refreshes immediately, even inside the normal stale time, and resumes
the configured interval. Directly opened shared conversations retain their
independent session updates, runner health, and permissions.

`sessionPageSize` controls initial session-list requests and each Load more
request (default 30). Each fetched page grows the Mine/Shared refresh window by
that configured size, even if optimistic filtering hides some or all rows.
`maxRefreshSessions` caps each subsequent refresh request (default 100); older
loaded rows remain cached beyond the cap. Both settings are injectable at runtime:

```tsx
<OmnigentApp
  {...hostProps}
  sidebarConfig={{ sessionPageSize: 50, maxRefreshSessions: 100 }}
/>
```

Use positive integers. With these values, initial/Load more requests use 50;
refresh requests grow from 50 to 100, then stay capped at 100. The initial request
uses `sessionPageSize` even when the refresh cap is smaller. Pins retain their
separate `pinCap`, and project folders retain their independent 20-row pages.
Archived lists use the configured session page size, refresh on entry, and have
no polling schedule. Display page sizes remain independent of API page sizes.

A click during refresh waits for that refresh and uses its resulting cursor.
Opaque `last_id` values round-trip unchanged as `after`, including when optimistic
archive/delete filtering empties a page; filtering preserves `has_more`. Only a
cursor exactly matching an ID in the original unfiltered page qualifies for
legacy row-ID repair. Cursors are never decoded.

Managed hosts resolve their own feature flags and pass these configuration
values through the embed integration; the OSS client has no SAFE dependency.

## Verify in a browser

Use the example configuration and inspect `/v1/sessions` requests in Network:

1. Open My sessions, then Inbox and Load more. There should be no
   `visibility=shared` requests, including pins.
2. Select All sessions or Shared sessions. Each entry should trigger one Shared
   refresh and display the corresponding rows.
3. Return to My sessions and wait longer than the Shared interval. Shared should
   make no requests. Re-enter Shared and confirm one immediate refresh.
4. Set both intervals to `false`. Reload and switch views: loading still works,
   but waiting does not cause periodic list requests.
5. Open a shared conversation URL while My sessions is selected. Confirm its
   live updates and edit permissions still work.
6. With a running owned session visible in My sessions, open the new-session
   directory picker on the same host and folder. Confirm the conflict warning
   appears. Opening or reopening the picker should add no session-list request
   and no unscoped `limit=200` scan.
7. Set `sharedDisplayPageSize: 30` and enter Shared with more than 30 sessions.
   Scroll to the bottom: it should stop at 30 until Load more is clicked.
   If older rows are already cached, the click reveals them without a request.
   Wait for a refresh after expanding the list: the display should keep its
   expanded size. Leave and re-enter Shared: it should start at 30 again.
8. Set `sessionPageSize: 50` and `maxRefreshSessions: 100`. Verify initial and
   Load more list requests use `limit=50`; after two pages, refresh uses 100 and
   remains capped after further pagination. For a backend with opaque cursors,
   archive/delete the last visible row and verify the next request's decoded
   `after` query parameter exactly equals the preceding response's `last_id`.
