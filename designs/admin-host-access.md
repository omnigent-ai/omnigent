# Admin Shared-Host Access & Per-User Host Access Management

## Current behavior (this change)

Hosts are owned by the user who registered them (`hosts.user_id`). Every
host-facing route checked `host.user_id == current_user_id` before acting.

This change makes the **admin role** able to see and control **every** host,
matching a shared-host deployment model:

- `GET /v1/hosts` — admins list all hosts (via `HostStore.list_all_hosts`);
  non-admins list only their own.
- All single-host routes (`get`, `model-options`, `runners`, `filesystem`,
  `directories`, `harness install/credential`, `detected`, `worktrees`) — admins
  bypass the ownership check.
- `resolve_host_owner` (used by runner launch and the session-create workspace
  probe) accepts an `is_admin` flag and waives ownership for admins.

Non-admin behavior is unchanged: a non-admin only sees and controls hosts they
own, and still gets 403/404 on anyone else's.

### Where the admin flag lives

`PermissionStore.is_admin(user_id)` (the existing admin flag on the account).
When auth is disabled (`permission_store is None`) the admin path is inert and
behavior is unchanged.

## Future plan: per-user host access management

The admin role is a coarse binary grant. A future refinement is **explicit,
per-user host access**: an admin UI where an administrator grants or revokes a
specific user's access to a specific host.

### Design

Reuse the existing permission machinery that already supports resource grants
(`permission_store.grant` / `resolve_access`), generalising hosts as a
grantable resource:

1. **Schema**: a `host_grants` table — `(host_id, user_id, level)`.

2. **Access resolution**: replace the simple `owner or admin` check with
   `owner or admin or resolved_access(host)`:
   - owner → level `OWNER`
   - admin → full access (implicit)
   - explicit grant row → the stored level
   - otherwise → none

3. **Admin API** (admin-only):
   - `GET /v1/admin/hosts/{host_id}/grants` — list who can use the host.
   - `PUT /v1/admin/hosts/{host_id}/grants/{user_id}` — set a level.
   - `DELETE /v1/admin/hosts/{host_id}/grants/{user_id}` — revoke.
   All gated behind `PermissionStore.is_admin`.

4. **Admin UI**: a page under the admin section listing every host and every
   user, with per-user access rows that can be added/removed. This is the
   "another place where you can add or remove access to hosts for each user"
   the product owner asked for.

5. **Tests**: admin CRUD on grants; non-admin cannot grant; access resolution
   combines owner + admin + grant.

Because `resolve_host_owner` is the single chokepoint for host access, the
grant-aware check plugs into exactly one place — the access predicate already
introduced here (`is_admin`) simply becomes `admin or granted` without touching
route handlers.