/**
 * Permissions management modal for a session.
 *
 * Displays current grants, allows granting/revoking access, and
 * toggling public visibility. Only accessible to users with
 * manage-level (3) permission on the session.
 */

import { type FormEvent, useCallback, useEffect, useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, LinkIcon, QrCodeIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { AddUserField } from "@/components/AddUserField";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  type Permission,
  useGrantPermission,
  usePermissions,
  useRevokePermission,
} from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { usePublicSharingMaxLevel } from "@/hooks/useSharing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { copyText } from "@/lib/clipboard";
import { updateSession } from "@/lib/sessionsApi";
import { getShareableSessionLink } from "@/lib/shareLink";
import { workspaceSharingBlocked } from "@/lib/permissionsApi";
import { useRebasePath } from "@/lib/routing";

const PUBLIC_USER = "__public__";

/** Numeric permission level → display label for fixed (non-editable) rows. */
const LEVEL_LABELS: Record<number, string> = {
  1: "Read",
  2: "Edit",
  3: "Manage",
  4: "Owner",
};

interface PermissionsModalProps {
  sessionId: string;
  workspace?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PermissionsModal({
  sessionId,
  workspace,
  open,
  onOpenChange,
}: PermissionsModalProps) {
  // Server sharing policy. While the boot probe is in flight we treat the
  // server as "on" (fail open) so the modal renders its full controls; the
  // server-side gate is the real enforcement point regardless.
  const info = useServerInfo();
  const sharingMode = info === "loading" ? "on" : info.sharing_mode;
  const sharingOff = sharingMode === "off";
  const sharingReadOnly = sharingMode === "read_only" || sharingMode === "restricted_read_only";
  const workspaceBlocked =
    sharingMode === "restricted_read_only" && workspaceSharingBlocked(workspace);
  // Disabling public sharing prevents new grants but still permits revocation.
  const publicSharingEnabled = info === "loading" ? true : info.public_sharing_enabled;
  const { data: livePublicMaxLevel } = usePublicSharingMaxLevel(open && !sharingOff);
  const publicMaxLevel =
    (livePublicMaxLevel ?? (info === "loading" ? "read" : info.public_sharing_max_level)) === "edit"
      ? 2
      : 1;
  // In "off" mode never fetch the grant list — the modal short-circuits to a
  // notice below, so the request would be wasted (and the server rejects any
  // grant anyway).
  const { data: permissions, isLoading } = usePermissions(open && !sharingOff ? sessionId : null);
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);
  const [showQr, setShowQr] = useState(false);
  const blockedNoticeId = useId();

  // Whether the owner shared the workspace files with view-level collaborators.
  // Read from the (shared-cache) session snapshot; the toggle PATCHes it and
  // invalidates the snapshot so the rail's file surfaces appear/disappear.
  const { session } = useSession(open && !sharingOff ? sessionId : null);
  const queryClient = useQueryClient();
  const shareWorkspace = useMutation({
    mutationFn: (next: boolean) => updateSession(sessionId, { shareWorkspaceFiles: next }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    },
    onError: (err: Error) => setError(err.message),
  });
  // Only a session with a workspace on disk has files to share; a plain
  // chat agent has none, so the toggle would be inert — hide it there.
  const hasWorkspace = !!session?.workspace;
  const workspaceShared = session?.shareWorkspaceFiles ?? false;

  const userGrants = (permissions ?? []).filter((p) => p.user_id !== PUBLIC_USER);
  const publicGrant = (permissions ?? []).find((p) => p.user_id === PUBLIC_USER);
  const publicLevel = publicGrant ? Math.min(publicGrant.level, publicMaxLevel) : 0;

  function handleGrant(e: FormEvent) {
    e.preventDefault();
    const trimmed = newUserId.trim();
    if (!trimmed || workspaceBlocked) return;
    setError(null);
    grant.mutate(
      { userId: trimmed, level: parseInt(newLevel, 10) },
      {
        onSuccess: () => {
          setNewUserId("");
          setNewLevel("1");
        },
        onError: (err) => setError(err.message),
      },
    );
  }

  function handleRevoke(userId: string) {
    setError(null);
    revoke.mutate(userId, {
      onError: (err) => setError(err.message),
    });
  }

  function handleChangeLevel(userId: string, level: number) {
    if (workspaceBlocked) return;
    setError(null);
    grant.mutate({ userId, level }, { onError: (err) => setError(err.message) });
  }

  function handlePublicLevel(value: string) {
    const level = Number(value);
    if (level > 0 && (workspaceBlocked || !publicSharingEnabled)) return;
    setError(null);
    if (level > 0) {
      grant.mutate({ userId: PUBLIC_USER, level }, { onError: (err) => setError(err.message) });
    } else {
      revoke.mutate(PUBLIC_USER, {
        onError: (err) => setError(err.message),
      });
    }
  }

  // "off" short-circuit: sharing is disabled server-wide, so skip the whole
  // grant UI and show a plain notice instead. Mirrors the server's 403.
  if (sharingOff) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">Sharing unavailable</DialogTitle>
            <DialogDescription>
              Sharing has been disabled for this Omnigent server.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Done
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">Share this session</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-3">
              <p>
                {sharingReadOnly
                  ? "This server allows read-only sharing — invite others to view this session."
                  : "Invite others to view or collaborate on this session."}
              </p>
              {sharingReadOnly && (
                <p>
                  Please be careful when sharing. Read access will allow for reading of all session
                  outputs.
                </p>
              )}
            </div>
          </DialogDescription>
        </DialogHeader>

        {(publicSharingEnabled || publicGrant) && (
          <div className="flex min-w-0 items-center justify-between gap-3 rounded-lg border px-3 py-2">
            <div className="min-w-0">
              <p className="text-ui font-medium">General access</p>
              <p className="text-sm text-muted-foreground">
                Anyone with the link, sign-in required
              </p>
            </div>
            <Select
              value={String(publicLevel)}
              onValueChange={handlePublicLevel}
              disabled={
                isLoading ||
                grant.isPending ||
                revoke.isPending ||
                (workspaceBlocked && !publicGrant)
              }
              componentId="diagnostics.permissions.public_level"
              valueHasNoPii
            >
              <SelectTrigger className="h-8 w-28 shrink-0" aria-label="General access">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="0">No access</SelectItem>
                <SelectItem value="1" disabled={workspaceBlocked || !publicSharingEnabled}>
                  Read
                </SelectItem>
                {publicMaxLevel === 2 && (
                  <SelectItem
                    value="2"
                    disabled={sharingReadOnly || workspaceBlocked || !publicSharingEnabled}
                  >
                    Edit
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>
        )}

        {/* Workspace-files toggle — off by default so a view-only share never
            leaks the session's files (which routinely hold secrets). Only the
            file surfaces open up; edit collaborators already have them. Shown
            only for sessions that actually have a workspace on disk. */}
        {hasWorkspace && (
          <div
            className="flex items-center justify-between rounded-lg border px-3 py-2"
            data-testid="share-workspace-files"
          >
            <div>
              <p className="text-ui font-medium">Workspace files</p>
              <p className="text-sm text-muted-foreground">
                Let people with view access browse this session's files
              </p>
            </div>
            <Switch
              checked={workspaceShared}
              onCheckedChange={(checked) => {
                setError(null);
                shareWorkspace.mutate(checked);
              }}
              disabled={shareWorkspace.isPending}
              componentId="diagnostics.permissions.share_workspace_toggle"
            />
          </div>
        )}

        {/* Current grants. DialogContent is a grid, and grid items default to
            min-width:auto — without min-w-0 a long nowrap email sets the whole
            track's min-content and pushes every row past the dialog edge. */}
        <div className="min-w-0" data-testid="share-grants">
          {isLoading ? (
            <p className="text-ui text-muted-foreground py-2">Loading…</p>
          ) : userGrants.length === 0 ? (
            <p className="text-ui text-muted-foreground py-2">No grants yet.</p>
          ) : (
            <>
              {/* Column headers */}
              <div className="flex items-center gap-2 px-2 pb-0.5">
                <span className="flex-1 text-sm font-medium uppercase tracking-wide text-muted-foreground">
                  Name
                </span>
                <span className="w-28 text-sm font-medium uppercase tracking-wide text-muted-foreground">
                  Permission
                </span>
                <span className="size-7 shrink-0" aria-hidden="true" />
              </div>
              <div className="max-h-48 overflow-y-auto">
                {userGrants.map((p) => (
                  <GrantRow
                    key={p.user_id}
                    permission={p}
                    onRevoke={handleRevoke}
                    onChangeLevel={handleChangeLevel}
                    busy={grant.isPending || revoke.isPending}
                    readOnly={sharingReadOnly}
                  />
                ))}
              </div>
            </>
          )}
        </div>

        {/* Shown above the disabled add-grant form so it reads as the reason the
            controls are inert, before the user tries them. */}
        {workspaceBlocked && (
          <p id={blockedNoticeId} className="text-sm text-destructive">
            This session&apos;s working directory (a home or root directory) cannot be shared on
            this Omnigent server.
          </p>
        )}

        {/* Add grant form */}
        <form onSubmit={handleGrant} className="flex items-end gap-2">
          <div className="flex-1">
            <label htmlFor="perm-user" className="text-sm font-medium text-muted-foreground">
              User ID
            </label>
            <AddUserField
              id="perm-user"
              value={newUserId}
              onChange={setNewUserId}
              disabled={workspaceBlocked}
            />
          </div>
          <div>
            <label htmlFor="perm-level" className="text-sm font-medium text-muted-foreground">
              Level
            </label>
            <Select
              value={newLevel}
              onValueChange={setNewLevel}
              disabled={workspaceBlocked}
              componentId="diagnostics.permissions.grant_level"
              valueHasNoPii
            >
              <SelectTrigger className="mt-1 w-24" aria-label="New user permission">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="1">Read</SelectItem>
                {/* Read-only sharing caps new grants at view; hide Edit. */}
                {!sharingReadOnly && <SelectItem value="2">Edit</SelectItem>}
              </SelectContent>
            </Select>
          </div>
          <Button
            type="submit"
            size="sm"
            loading={grant.isPending}
            disabled={workspaceBlocked || !newUserId.trim()}
            aria-describedby={workspaceBlocked ? blockedNoticeId : undefined}
            componentId="diagnostics.permissions.grant"
          >
            <UserPlusIcon className="mr-1 size-3.5" />
            Grant
          </Button>
        </form>

        {error && <p className="text-sm text-destructive">{error}</p>}

        {/* Keep the default stacking below sm: one row of Copy link + QR +
            Done overflows a phone-width dialog and widens the whole grid. */}
        <DialogFooter className="sm:justify-between">
          <div className="flex items-center gap-2">
            <CopyLinkButton sessionId={sessionId} />
            <Button
              variant="ghost"
              onClick={() => setShowQr(true)}
              className="gap-1.5 text-primary"
            >
              <QrCodeIcon className="size-3.5" />
              Open in mobile app
            </Button>
          </div>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>

      {/* Separate modal for the QR code so the share dialog stays compact. */}
      <QrCodeDialog sessionId={sessionId} open={showQr} onOpenChange={setShowQr} />
    </Dialog>
  );
}

/**
 * The `omnigent://<host>/c/<session_id>` deep link encoded into the share QR
 * code. The host (with port when non-default) is parsed from the same shareable
 * URL `getShareableSessionLink` resolves — so standalone and embedded (host-transformed)
 * origins agree on the same server the desktop shell's deep-link handler keys
 * off of (see `electron/src/deepLink.js`). The path is always basename-less
 * `/c/<id>`; the workspace mount is server-determined and intentionally absent.
 */
function getDeepLink(sessionId: string, rebasePath: (path: string) => string): string {
  const url = getShareableSessionLink(sessionId, rebasePath);
  try {
    const { host } = new URL(url);
    return `omnigent://${host}/c/${sessionId}`;
  } catch {
    // Unparseable transform output: fall back to the current origin's host.
    return `omnigent://${window.location.host}/c/${sessionId}`;
  }
}

function CopyLinkButton({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState(false);
  const rebasePath = useRebasePath();

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);

  const handleCopy = useCallback(() => {
    const url = getShareableSessionLink(sessionId, rebasePath);
    copyText(url).then(
      () => setCopied(true),
      (err) => {
        console.warn("Failed to copy link to clipboard", err);
      },
    );
  }, [sessionId, rebasePath]);

  return (
    <Button
      variant="ghost"
      onClick={handleCopy}
      className="gap-1.5 text-primary"
      componentId="diagnostics.permissions.copy_link"
    >
      {copied ? <CheckIcon className="size-3.5" /> : <LinkIcon className="size-3.5" />}
      {copied ? "Copied!" : "Copy link"}
    </Button>
  );
}

/**
 * A separate modal showing a QR code encoding the session's
 * `omnigent://<host>/c/<id>` deep link so a user can scan it with their phone
 * to open the session in the Omnigent app. The code is rendered on a fixed
 * white tile so it stays scannable regardless of the app's dark/light theme
 * (a dark-on-dark QR won't read). Error correction is bumped to M for
 * resilience against partial occlusion.
 */
function QrCodeDialog({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rebasePath = useRebasePath();
  const deepLink = getDeepLink(sessionId, rebasePath);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <QrCodeIcon className="size-4" />
            Open in mobile app
          </DialogTitle>
          <DialogDescription>
            Scan with your phone's camera to open this session in the Omnigent app.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-center">
          <div className="rounded-lg bg-white p-3">
            <QRCodeSVG
              value={deepLink}
              size={200}
              level="M"
              // White tile + explicit module colors keep the code scannable in dark
              // mode; the padding also serves as the QR quiet zone.
              bgColor="#ffffff"
              fgColor="#000000"
              aria-label="QR code to open this session in the Omnigent app"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function GrantRow({
  permission,
  onRevoke,
  onChangeLevel,
  busy,
  readOnly,
}: {
  permission: Permission;
  onRevoke: (userId: string) => void;
  onChangeLevel: (userId: string, level: number) => void;
  busy: boolean;
  readOnly: boolean;
}) {
  const isOwner = permission.level === 4;
  // Manage is not grantable from the UI, so a pre-existing manage grant
  // renders as a fixed label rather than a dropdown choice. Unlike the
  // owner row it can still be revoked.
  const isManage = permission.level === 3;
  // Read-only sharing mode: existing grants can't be re-leveled, so the level
  // shows as a fixed label (like owner/manage) — but the row stays revocable.
  const fixedLevel = isOwner || isManage || readOnly;

  return (
    <div className="flex items-center gap-2 rounded-md px-2 py-0.5 hover:bg-muted/50">
      {/* Tail truncation keeps the local part — the distinguishing half when
          every grantee shares one company domain — and the title tooltip
          carries the full id. */}
      <span className="flex-1 truncate text-ui" title={permission.user_id}>
        {permission.user_id}
      </span>
      {fixedLevel ? (
        <span className="flex h-8 w-28 items-center px-3 text-ui text-muted-foreground">
          {LEVEL_LABELS[permission.level] ?? "Read"}
        </span>
      ) : (
        <Select
          value={String(permission.level)}
          onValueChange={(v) => onChangeLevel(permission.user_id, parseInt(v, 10))}
          disabled={busy}
        >
          <SelectTrigger
            className="h-8 w-28"
            aria-label={`Permission level for ${permission.user_id}`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            <SelectItem value="1">Read</SelectItem>
            <SelectItem value="2">Edit</SelectItem>
          </SelectContent>
        </Select>
      )}
      {isOwner ? (
        <span className="size-7 shrink-0" aria-hidden="true" />
      ) : (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => onRevoke(permission.user_id)}
          disabled={busy}
          className="shrink-0 text-muted-foreground hover:text-destructive"
          componentId="diagnostics.permissions.revoke"
        >
          <Trash2Icon className="size-3.5" />
          <span className="sr-only">Revoke</span>
        </Button>
      )}
    </div>
  );
}
