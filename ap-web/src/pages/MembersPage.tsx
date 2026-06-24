/**
 * Admin members management page (``/members``).
 *
 * Lists every account on the server and lets admins:
 *
 * - Mint a single-use invite URL to share out-of-band.
 * - Reset a member's password (server generates a fresh random
 *   one and returns it exactly once — admin DMs it to the user).
 * - Remove a member entirely (cascades grants via the existing
 *   ``ON DELETE CASCADE`` on session_permissions).
 *
 * Gated on the client by an early "not an admin → render nothing"
 * check AND on the server by the route handlers themselves —
 * client-side gating is just UX so non-admins don't see useless
 * buttons; the server is what actually enforces.
 *
 * The "reset password" and "create invite" flows display the
 * sensitive value EXACTLY ONCE in a modal with a Copy button.
 * There is intentionally no way to retrieve them later — admins
 * who lose them just reset again. This matches the field
 * convention (GitLab, n8n, Coolify all do the same) and avoids
 * accidentally caching secrets in a list endpoint.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "@/lib/routing";
import { CopyIcon, KeyRoundIcon, RefreshCwIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  type AccountListEntry,
  type InviteCreated,
  type PasswordReset,
  createInvite,
  deleteUser,
  getMe,
  listUsers,
  resetUserPassword,
} from "@/lib/accountsApi";

export function MembersPage() {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const [meId, setMeId] = useState<string | null>(null);
  const [users, setUsers] = useState<AccountListEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Modal state (only one open at a time — keeps the render simple).
  const [inviteResult, setInviteResult] = useState<InviteCreated | null>(null);
  const [showCreateInvite, setShowCreateInvite] = useState(false);
  const [inviteAsAdmin, setInviteAsAdmin] = useState(false);
  const [resetResult, setResetResult] = useState<PasswordReset | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const list = await listUsers();
    if (list === null) {
      setLoadError(t("couldNotLoadMembers"));
      setUsers([]);
      return;
    }
    setLoadError(null);
    setUsers(list);
  }, [t]);

  // Initial load: identity probe + members list. The identity probe
  // gates the UI (non-admins see "no access"); the list is what we
  // render the table from.
  useEffect(() => {
    void (async () => {
      const me = await getMe();
      if (me === null) {
        // Not authenticated — bounce to login. Shouldn't happen
        // because identity.ts redirects on 401, but defensive.
        navigate("/login", { replace: true });
        return;
      }
      setMeId(me.id);
      setMeIsAdmin(me.is_admin);
      if (me.is_admin) await refresh();
    })();
  }, [navigate, refresh]);

  // Pre-admin-check render: blank loading state. min-h-full so the
  // AppShell's outlet container governs height — we're a child view,
  // not a full-page replacement.
  if (meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        {t("loading")}
      </div>
    );
  }

  // Non-admin: hard stop. Server would also 403, this is just UX.
  if (meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">{t("members")}</h1>
        <p className="text-sm text-muted-foreground">{t("noPermissionToManage")}</p>
      </div>
    );
  }

  async function onCreateInvite() {
    setPendingAction(true);
    setActionError(null);
    const result = await createInvite(inviteAsAdmin);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setShowCreateInvite(false);
    setInviteResult(result);
    setInviteAsAdmin(false);
    // Refresh the list — a new user will appear once the invite is
    // redeemed, but the count stays the same now.
  }

  async function onConfirmDelete() {
    if (deleteCandidate === null) return;
    setPendingAction(true);
    setActionError(null);
    const result = await deleteUser(deleteCandidate);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setDeleteCandidate(null);
    await refresh();
  }

  async function onResetPassword(userId: string) {
    setPendingAction(true);
    setActionError(null);
    const result = await resetUserPassword(userId);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setResetResult(result);
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8 pt-14">
      {/* pt-14 leaves room for the AppShell's absolute-positioned
          header (sidebar toggle / account menu) so the page title
          isn't tucked under it. */}
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">{t("members")}</h1>
        <Button onClick={() => setShowCreateInvite(true)}>
          <UserPlusIcon /> {t("inviteMember")}
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

      {users !== null && users.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">{t("usernameColumn")}</th>
                <th className="px-3 py-2 font-medium">{t("roleColumn")}</th>
                <th className="px-3 py-2 font-medium">{t("lastLoginColumn")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("actionsColumn")}</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-t border-border">
                  <td className="px-3 py-2 align-middle">
                    <span className="font-medium">{u.id}</span>
                    {u.id === meId && (
                      <span className="ml-2 text-xs text-muted-foreground">{t("youSelf")}</span>
                    )}
                    {!u.has_password && (
                      <Badge variant="outline" className="ml-2">
                        {t("externalBadge")}
                      </Badge>
                    )}
                  </td>
                  <td className="px-3 py-2 align-middle">
                    {u.is_admin ? <Badge>{t("adminRole")}</Badge> : <Badge variant="secondary">{t("memberRole")}</Badge>}
                  </td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {formatEpoch(u.last_login_at, t("never"))}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="xs"
                        title={t("resetPassword")}
                        onClick={() => void onResetPassword(u.id)}
                        disabled={pendingAction || !u.has_password}
                      >
                        <KeyRoundIcon /> {t("resetPassword")}
                      </Button>
                      <Button
                        variant="ghost"
                        size="xs"
                        title={t("removeUser")}
                        onClick={() => setDeleteCandidate(u.id)}
                        disabled={pendingAction || u.id === meId}
                      >
                        <Trash2Icon /> {t("removeUser")}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {users !== null && users.length === 0 && (
        <p className="text-sm text-muted-foreground">{t("noMembersYet")}</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void refresh()}>
          <RefreshCwIcon /> {t("refresh")}
        </Button>
      </div>

      {/* ── Create invite modal ───────────────────────────────────── */}
      <Dialog
        open={showCreateInvite}
        onOpenChange={(open) => {
          if (pendingAction) return;
          setShowCreateInvite(open);
          if (!open) setActionError(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("inviteMemberTitle")}</DialogTitle>
            <DialogDescription>{t("inviteDesc")}</DialogDescription>
          </DialogHeader>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={inviteAsAdmin}
              onChange={(e) => setInviteAsAdmin(e.target.checked)}
              disabled={pendingAction}
            />
            {t("grantAdminPrivileges")}
          </label>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setShowCreateInvite(false)}
              disabled={pendingAction}
            >
              {t("cancel")}
            </Button>
            <Button onClick={() => void onCreateInvite()} disabled={pendingAction}>
              {pendingAction ? t("creating") : t("createInvite")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Invite-created modal (shows the URL exactly once) ─────── */}
      <Dialog
        open={inviteResult !== null}
        onOpenChange={(open) => {
          if (!open) setInviteResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("inviteUrl")}</DialogTitle>
            <DialogDescription>
              {t("inviteUrlDesc", { expiry: formatTtl(inviteResult?.expires_at) })}
            </DialogDescription>
          </DialogHeader>
          {inviteResult !== null && <CopyableValue value={rebaseUrl(inviteResult.register_url)} />}
          <DialogFooter>
            <Button onClick={() => setInviteResult(null)}>{t("done")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Reset password modal (shows the new password once) ───── */}
      <Dialog
        open={resetResult !== null}
        onOpenChange={(open) => {
          if (!open) setResetResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("newPasswordFor", { user: resetResult?.id })}</DialogTitle>
            <DialogDescription>{t("resetPasswordDesc")}</DialogDescription>
          </DialogHeader>
          {resetResult !== null && <CopyableValue value={resetResult.new_password} />}
          <DialogFooter>
            <Button onClick={() => setResetResult(null)}>{t("done")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete confirmation ────────────────────────────────── */}
      <Dialog
        open={deleteCandidate !== null}
        onOpenChange={(open) => {
          if (pendingAction) return;
          if (!open) {
            setDeleteCandidate(null);
            setActionError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("removeUserConfirmTitle", { user: deleteCandidate })}</DialogTitle>
            <DialogDescription>{t("removeUserConfirmDesc")}</DialogDescription>
          </DialogHeader>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setDeleteCandidate(null)}
              disabled={pendingAction}
            >
              {t("cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => void onConfirmDelete()}
              disabled={pendingAction}
            >
              {pendingAction ? t("removing") : t("remove")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * A read-only field paired with a one-click copy button.
 *
 * Used for both invite URLs and reset-issued passwords; both are
 * single-use sensitive values that need a frictionless copy path
 * since the user typically pastes them into Slack within seconds.
 */
function CopyableValue({ value }: { value: string }) {
  const { t } = useTranslation("common");
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // No clipboard permission — the input is still selectable.
    }
  };
  return (
    <div className="flex items-center gap-2">
      <Input
        value={value}
        readOnly
        className="font-mono text-xs"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button variant="outline" size="sm" onClick={() => void onCopy()} aria-label={t("copy")}>
        <CopyIcon /> {copied ? t("copied") : t("copy")}
      </Button>
    </div>
  );
}

/**
 * Replace the origin of a server-returned URL with the current browser
 * origin so invite links work regardless of how the admin reached the
 * app (e.g. via a reverse proxy or non-loopback address).
 */
function rebaseUrl(serverUrl: string): string {
  try {
    const parsed = new URL(serverUrl);
    return `${window.location.origin}${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return serverUrl;
  }
}

function formatEpoch(epoch: number | null, never: string): string {
  if (epoch === null) return never;
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

function formatTtl(expiresAt: number | undefined): string {
  if (expiresAt === undefined) return "soon";
  const secs = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
  const hours = Math.round(secs / 3600);
  if (hours >= 1) return `${hours}h`;
  return `${Math.max(1, Math.round(secs / 60))}m`;
}
