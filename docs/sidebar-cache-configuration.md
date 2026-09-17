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

Each refresh requests the loaded window, up to `maxRefreshSessions` (default
100), and merges older loaded rows beyond that cap. Load more appends the next
30 rows. A click during refresh waits for that refresh and uses its resulting
cursor. Archived sessions refresh on entry and have no polling schedule.

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
