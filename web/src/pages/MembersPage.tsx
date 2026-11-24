/**
 * Admin members management page (``/settings/members``; the legacy
 * ``/members`` path redirects here). Rendered as a Settings sub-category.
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
import { CopyIcon, KeyRoundIcon, RefreshCwIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
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
  listUsers,
  resetUserPassword,
} from "@/lib/accountsApi";
import {
  getCurrentIsAdmin,
  getCurrentUserId,
  resolveIdentity,
  subscribeIdentity,
} from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isBootFallbackInfo } from "@/lib/bootCapabilities";
import { withBasePath } from "@/lib/basePath";
import { isSingleUserMode } from "@/lib/capabilities";
import { MEMBERS_INVITE_ENTRY, MEMBERS_LIST_ENTRY } from "@/pages/settings/search/catalog/members";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import {
  SettingsDataRegion,
  settingsOverlayProps,
  settingsRowOverlayProps,
} from "@/pages/settings/search/settingsOwnership";

export function MembersPage() {
  const info = useServerInfo();
  const capabilitiesLoading = info === "loading" || isBootFallbackInfo(info);
  // Password-based management (invite / reset / remove) only exists in
  // accounts mode — OIDC identities are owned by the IdP, so under OIDC
  // this page is a read-only user list (no action column, no modals).
  const manageable = info !== "loading" && info.accounts_enabled;
  // Plain header/single-user mode: no auth endpoints exist. The nav + route
  // already hide Members here; this placeholder is a fallback for a direct
  // hit before the redirect resolves.
  const isSingleUser = isSingleUserMode(info);
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
      setLoadError(
        "Could not load members. You may not have admin permission, or the server is unreachable.",
      );
      setUsers([]);
      return;
    }
    setLoadError(null);
    setUsers(list);
  }, []);

  // Follow identity recovery while this page remains open.
  useEffect(() => {
    if (isSingleUser || capabilitiesLoading) return;
    let active = true;
    const update = (userId: string | null) => {
      if (!active || userId === null) return;
      setMeId(userId);
      setMeIsAdmin(getCurrentIsAdmin());
    };
    const unsubscribe = subscribeIdentity(() => update(getCurrentUserId()));
    void resolveIdentity().then(update);
    return () => {
      active = false;
      unsubscribe();
    };
  }, [isSingleUser, capabilitiesLoading]);

  useEffect(() => {
    if (!isSingleUser && !capabilitiesLoading && meIsAdmin) void refresh();
  }, [refresh, isSingleUser, capabilitiesLoading, meId, meIsAdmin]);

  if (isSingleUser) {
    return (
      <PageScroll
        maxWidthClassName="max-w-4xl"
        contentClassName="px-4 md:px-8"
        extraBottom="2.5rem"
      >
        <h1 className="mb-2 text-2xl font-medium">Members</h1>
        <p className="text-ui text-muted-foreground">
          Member management is not available in single-user mode.
        </p>
      </PageScroll>
    );
  }

  // Pre-admin-check render: blank loading state. min-h-full so the
  // AppShell's outlet container governs height — we're a child view,
  // not a full-page replacement. min-h-full so the
  // AppShell's outlet container governs height — we're a child view,
  // not a full-page replacement.
  if (meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-ui text-muted-foreground">
        Loading…
      </div>
    );
  }

  // Non-admin: hard stop. Server would also 403, this is just UX.
  if (meIsAdmin === false) {
    return (
      <PageScroll
        maxWidthClassName="max-w-4xl"
        contentClassName="px-4 md:px-8"
        extraBottom="2.5rem"
      >
        <h1 className="mb-2 text-2xl font-medium">Members</h1>
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage members.
        </p>
      </PageScroll>
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
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-medium">Members</h1>
        {/* Invite mints a password-backed account — accounts mode only.
        Under OIDC, accounts are provisioned by the IdP on first login, so
        there's nothing to invite here. */}
        {manageable && (
          <SettingsItem entry={MEMBERS_INVITE_ENTRY}>
            <Button
              {...settingsControlProps(MEMBERS_INVITE_ENTRY, "invite")}
              onClick={() => setShowCreateInvite(true)}
            >
              <UserPlusIcon /> Invite member
            </Button>
          </SettingsItem>
        )}
      </div>

      <SettingsItem entry={MEMBERS_LIST_ENTRY}>
        {!manageable && (
          <p className="mb-4 text-sm text-muted-foreground">
            Users are added on first sign-in through your identity provider. This list is read-only.
          </p>
        )}

        {loadError !== null && (
          <div
            role="alert"
            className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
          >
            {loadError}
          </div>
        )}

        {users !== null && users.length > 0 && (
          <SettingsDataRegion
            entry={MEMBERS_LIST_ENTRY}
            region="members"
            className="overflow-hidden rounded-md border border-border"
          >
            <table className="w-full text-ui">
              <thead className="bg-muted/40 text-left text-sm uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Username</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Last login</th>
                  {manageable && <th className="px-3 py-2 text-right font-medium">Actions</th>}
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} className="border-t border-border">
                    <td className="px-3 py-2 align-middle">
                      <span className="font-medium">{u.id}</span>
                      {u.id === meId && (
                        <span className="ml-2 text-sm text-muted-foreground">(you)</span>
                      )}
                      {!u.has_password && (
                        <Badge variant="outline" className="ml-2">
                          External
                        </Badge>
                      )}
                    </td>
                    <td className="px-3 py-2 align-middle">
                      {u.is_admin ? (
                        <Badge>Admin</Badge>
                      ) : (
                        <Badge variant="secondary">Member</Badge>
                      )}
                    </td>
                    <td className="px-3 py-2 align-middle text-muted-foreground">
                      {formatEpoch(u.last_login_at)}
                    </td>
                    {manageable && (
                      <td className="px-3 py-2 text-right">
                        <div className="flex justify-end gap-1">
                          <Button
                            data-settings-row-action="resetPassword"
                            variant="ghost"
                            size="xs"
                            title="Reset password"
                            onClick={() => void onResetPassword(u.id)}
                            disabled={pendingAction || !u.has_password}
                          >
                            <KeyRoundIcon /> Reset
                          </Button>
                          <Button
                            data-settings-row-action="remove"
                            variant="ghost"
                            size="xs"
                            title="Remove user"
                            onClick={() => setDeleteCandidate(u.id)}
                            disabled={pendingAction || u.id === meId}
                          >
                            <Trash2Icon /> Remove
                          </Button>
                        </div>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </SettingsDataRegion>
        )}

        {users !== null && users.length === 0 && (
          <p className="text-ui text-muted-foreground">No members yet.</p>
        )}

        <div className="mt-3 flex items-center justify-end">
          <Button
            {...settingsControlProps(MEMBERS_LIST_ENTRY, "refresh")}
            variant="ghost"
            size="sm"
            onClick={() => void refresh()}
          >
            <RefreshCwIcon /> Refresh
          </Button>
        </div>
      </SettingsItem>

      {/* ── Create invite modal ───────────────────────────────────── */}
      <Dialog
        open={showCreateInvite}
        onOpenChange={(open) => {
          if (pendingAction) return;
          setShowCreateInvite(open);
          if (!open) setActionError(null);
        }}
      >
        <DialogContent {...settingsOverlayProps(MEMBERS_INVITE_ENTRY, "invite")}>
          <DialogHeader>
            <DialogTitle>Invite a member</DialogTitle>
            <DialogDescription>
              A single-use invite URL will be created. Share it with the person you want to add.
              They'll choose their own username and password when they redeem it.
            </DialogDescription>
          </DialogHeader>
          <label className="flex items-center gap-2 text-ui">
            <input
              {...settingsControlProps(MEMBERS_INVITE_ENTRY, "grantAdmin")}
              type="checkbox"
              checked={inviteAsAdmin}
              onChange={(e) => setInviteAsAdmin(e.target.checked)}
              disabled={pendingAction}
            />
            Grant admin privileges
          </label>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              data-settings-dialog-action="Cancel"
              variant="ghost"
              onClick={() => setShowCreateInvite(false)}
              disabled={pendingAction}
            >
              Cancel
            </Button>
            <Button
              {...settingsControlProps(MEMBERS_INVITE_ENTRY, "create")}
              onClick={() => void onCreateInvite()}
              loading={pendingAction}
            >
              Create invite
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
        <DialogContent {...settingsOverlayProps(MEMBERS_INVITE_ENTRY, "invite")}>
          <DialogHeader>
            <DialogTitle>Invite URL</DialogTitle>
            <DialogDescription>
              Send this URL to the new member. It expires in {formatTtl(inviteResult?.expires_at)}{" "}
              and is single-use — once they redeem it, it can't be used again. This URL is shown
              only once.
            </DialogDescription>
          </DialogHeader>
          {inviteResult !== null && <InviteUrlField value={rebaseUrl(inviteResult.register_url)} />}
          <DialogFooter>
            <Button data-settings-dialog-action="Done" onClick={() => setInviteResult(null)}>
              Done
            </Button>
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
        <DialogContent {...settingsRowOverlayProps(MEMBERS_LIST_ENTRY, "members", "resetPassword")}>
          <DialogHeader>
            <DialogTitle>New password for {resetResult?.id}</DialogTitle>
            <DialogDescription>
              Send this password to the user out-of-band (e.g. Slack DM). It is shown only once.
            </DialogDescription>
          </DialogHeader>
          {resetResult !== null && (
            <NewPasswordField userId={resetResult.id} value={resetResult.new_password} />
          )}
          <DialogFooter>
            <Button data-settings-row-action="done" onClick={() => setResetResult(null)}>
              Done
            </Button>
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
        <DialogContent {...settingsRowOverlayProps(MEMBERS_LIST_ENTRY, "members", "remove")}>
          <DialogHeader>
            <DialogTitle>Remove {deleteCandidate}?</DialogTitle>
            <DialogDescription>
              This deletes the user account and revokes all their session permissions. Sessions they
              own become inaccessible unless another user has manage rights on them. This action
              cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              data-settings-row-action="cancel"
              variant="ghost"
              onClick={() => setDeleteCandidate(null)}
              disabled={pendingAction}
            >
              Cancel
            </Button>
            <Button
              data-settings-row-action="remove"
              variant="destructive"
              onClick={() => void onConfirmDelete()}
              loading={pendingAction}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}

function useCopyValue(value: string) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // No clipboard permission — the field is still selectable.
    }
  };
  return { copied, copy };
}

/** The invite's single-use URL, read-only and selected on focus, with its copy button. */
function InviteUrlField({ value }: { value: string }) {
  const { copied, copy } = useCopyValue(value);
  return (
    <div className="flex items-center gap-2">
      <Input
        {...settingsControlProps(MEMBERS_INVITE_ENTRY, "inviteUrl")}
        value={value}
        readOnly
        className="font-mono text-sm"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button
        {...settingsControlProps(MEMBERS_INVITE_ENTRY, "copyInviteUrl")}
        variant="outline"
        size="sm"
        onClick={() => void copy()}
      >
        <CopyIcon /> {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

/** A reset's new password for `userId`, read-only and selected on focus, with its copy button. */
function NewPasswordField({ userId, value }: { userId: string; value: string }) {
  const { copied, copy } = useCopyValue(value);
  return (
    <div className="flex items-center gap-2">
      <Input
        data-settings-row-field="password"
        aria-label={`New password for ${userId}`}
        value={value}
        readOnly
        className="font-mono text-sm"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button
        data-settings-row-action="copy"
        aria-label="Copy password"
        variant="outline"
        size="sm"
        onClick={() => void copy()}
      >
        <CopyIcon /> {copied ? "Copied" : "Copy"}
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
    // Apply the deployment base path (e.g. `/proxy/6767`) so the invite link
    // resolves under a subpath proxy. `withBasePath` is idempotent, so a
    // server that already emits a prefixed path (via
    // OMNIGENT_ACCOUNTS_BASE_URL) is left unchanged.
    return `${window.location.origin}${withBasePath(parsed.pathname)}${parsed.search}${parsed.hash}`;
  } catch {
    return serverUrl;
  }
}

function formatEpoch(epoch: number | null): string {
  if (epoch === null) return "Never";
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
