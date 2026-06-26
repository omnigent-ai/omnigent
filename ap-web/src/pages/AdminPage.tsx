/**
 * Admin page (``/admin``).
 *
 * The OIDC/SSO admin surface: a list of every user, and — on
 * selecting one — that user's sessions, each a link into the normal
 * chat view. Admins already hold owner-level access to any session
 * (the server short-circuits ``check_session_access`` for admins), so
 * opening a listed session Just Works.
 *
 * Gated on the client by an "is_admin → else no access" check AND on
 * the server by the ``/v1/admin/*`` route handlers — client gating is
 * only UX so non-admins don't see a broken page; the server enforces.
 */

import { useCallback, useEffect, useState } from "react";
import { RefreshCwIcon } from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { getCurrentIsAdmin, getCurrentUserId, resolveIdentity } from "@/lib/identity";
import {
  type AdminSession,
  type AdminUser,
  type UsageTotals,
  listAllUsers,
  listUserSessions,
} from "@/lib/adminApi";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

export function AdminPage() {
  const navigate = useNavigate();
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);
  const [meId, setMeId] = useState<string | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedUser, setSelectedUser] = useState<string | null>(null);
  const [sessions, setSessions] = useState<AdminSession[] | null>(null);
  const [sessionTotals, setSessionTotals] = useState<UsageTotals | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  const refreshUsers = useCallback(async () => {
    const list = await listAllUsers();
    if (list === null) {
      setLoadError(
        "Could not load users. You may not have admin permission, or the server is unreachable.",
      );
      setUsers([]);
      return;
    }
    setLoadError(null);
    setUsers(list);
  }, []);

  // Initial load: resolve identity to gate the page, then list users.
  useEffect(() => {
    void (async () => {
      await resolveIdentity();
      const admin = getCurrentIsAdmin();
      setMeId(getCurrentUserId());
      setIsAdmin(admin);
      if (admin) await refreshUsers();
    })();
  }, [refreshUsers]);

  const onSelectUser = useCallback(async (userId: string) => {
    setSelectedUser(userId);
    setSessions(null);
    setSessionTotals(null);
    setSessionsLoading(true);
    const result = await listUserSessions(userId);
    setSessionsLoading(false);
    setSessions(result?.sessions ?? []);
    setSessionTotals(result?.totals ?? null);
  }, []);

  if (isAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (isAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Admin</h1>
        <p className="text-sm text-muted-foreground">You don't have admin access.</p>
      </div>
    );
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Admin</h1>
        <Button variant="ghost" size="sm" onClick={() => void refreshUsers()}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>

      {loadError !== null && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      <h2 className="mb-2 text-sm font-medium text-muted-foreground">Users</h2>
      {users !== null && users.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">User</th>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 text-right font-medium">Sessions</th>
                <th className="px-3 py-2 text-right font-medium">Tokens</th>
                <th className="px-3 py-2 text-right font-medium">Cost</th>
                <th className="px-3 py-2 text-right font-medium" aria-label="actions" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr
                  key={u.user_id}
                  data-testid="admin-user-row"
                  className={
                    "cursor-pointer border-t border-border hover:bg-muted/40" +
                    (selectedUser === u.user_id ? " bg-muted/60" : "")
                  }
                  onClick={() => void onSelectUser(u.user_id)}
                >
                  <td className="px-3 py-2 align-middle">
                    <span className="font-medium">{u.user_id}</span>
                    {u.user_id === meId && (
                      <span className="ml-2 text-xs text-muted-foreground">(you)</span>
                    )}
                  </td>
                  <td className="px-3 py-2 align-middle">
                    {u.is_admin ? <Badge>Admin</Badge> : <Badge variant="secondary">Member</Badge>}
                  </td>
                  <td className="px-3 py-2 text-right align-middle tabular-nums text-muted-foreground">
                    {u.session_count}
                  </td>
                  <td className="px-3 py-2 text-right align-middle tabular-nums text-muted-foreground">
                    {formatTokens(u.total_tokens)}
                  </td>
                  <td className="px-3 py-2 text-right align-middle tabular-nums font-medium">
                    {formatUsd(u.cost_usd)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        void onSelectUser(u.user_id);
                      }}
                    >
                      View
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {users !== null && users.length === 0 && loadError === null && (
        <p className="text-sm text-muted-foreground">No users yet.</p>
      )}

      {/* ── Selected user's sessions ─────────────────────────────── */}
      {selectedUser !== null && (
        <div className="mt-8">
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">
              Sessions for <span className="font-semibold text-foreground">{selectedUser}</span>
            </h2>
            {sessionTotals !== null && (
              <span className="text-xs text-muted-foreground tabular-nums">
                {sessionTotals.session_count} sessions · {formatTokens(sessionTotals.total_tokens)}{" "}
                tokens ·{" "}
                <span className="font-medium text-foreground">
                  {formatUsd(sessionTotals.cost_usd)}
                </span>
              </span>
            )}
          </div>
          {sessionsLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {!sessionsLoading && sessions !== null && sessions.length === 0 && (
            <p className="text-sm text-muted-foreground">This user has no sessions.</p>
          )}
          {!sessionsLoading && sessions !== null && sessions.length > 0 && (
            <div className="overflow-hidden rounded-md border border-border">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">Title</th>
                    <th className="px-3 py-2 font-medium">Updated</th>
                    <th className="px-3 py-2 text-right font-medium">Tokens</th>
                    <th className="px-3 py-2 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr
                      key={s.id}
                      data-testid="admin-session-row"
                      className="cursor-pointer border-t border-border hover:bg-muted/40"
                      onClick={() => navigate(`/c/${s.id}`)}
                    >
                      <td className="px-3 py-2 align-middle font-medium">
                        {s.title ?? <span className="text-muted-foreground">Untitled</span>}
                      </td>
                      <td className="px-3 py-2 align-middle text-muted-foreground">
                        {formatEpoch(s.updated_at)}
                      </td>
                      <td className="px-3 py-2 text-right align-middle tabular-nums text-muted-foreground">
                        {formatTokens(s.total_tokens)}
                      </td>
                      <td className="px-3 py-2 text-right align-middle tabular-nums font-medium">
                        {formatUsd(s.cost_usd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </PageScroll>
  );
}

function formatEpoch(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}

/**
 * Format a USD cost. Sub-cent spend still shows as `$0.00` rather than
 * being hidden, so a session with negligible-but-nonzero cost reads as
 * "cheap", not "free".
 */
function formatUsd(cost: number): string {
  return `$${cost.toFixed(2)}`;
}

/** Compact token count: 1234 → "1.2K", 1500000 → "1.5M". */
function formatTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(1)}K`;
  return `${(tokens / 1_000_000).toFixed(1)}M`;
}
