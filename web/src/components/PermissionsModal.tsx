/**
 * Permissions management modal for a session, an automation, a workflow or a gadget.
 *
 * Displays current grants, allows granting/revoking access, and
 * toggling public visibility. Only accessible to users with
 * manage-level (3) permission on the session. An automation or a workflow
 * is shared by its owner at read access only, with no public or link sharing.
 * A gadget is shared by its managers at Can view, Can edit or Can manage, also
 * without public or link sharing.
 */

import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, InfoIcon, LinkIcon, QrCodeIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  AutomationCallout,
  AutomationLink,
} from "@/components/scheduled/AutomationSourceDeleteNotice";
import {
  type ShareGrant,
  type ShareTarget,
  useGrantList,
  useGrantPermission,
  useRevokePermission,
} from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { usePublicSharingMaxLevel } from "@/hooks/useSharing";
import { useUserSearch } from "@/hooks/useUserSearch";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { SHARING_OFF_REASON } from "@/lib/capabilities";
import { copyText } from "@/lib/clipboard";
import { updateSession } from "@/lib/sessionsApi";
import { getOmnigentUserSearch } from "@/lib/host";
import { getSessionSharePath, getShareableSessionLink } from "@/lib/shareLink";
import { workspaceSharingBlocked } from "@/lib/permissionsApi";
import { useRebasePath } from "@/lib/routing";
import { automationSharePath } from "@/lib/automationFromSession";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import type { Session } from "@/lib/types";
import type { Gadget } from "@/gadgets/api/gadgetApi";
import { gadgetLink } from "@/gadgets/ui/gadgetNavigation";
import { cn } from "@/lib/utils";

const PUBLIC_USER = "__public__";

/** Numeric permission level → display label for fixed (non-editable) rows. */
const LEVEL_LABELS: Record<number, string> = {
  1: "Read",
  2: "Edit",
  3: "Manage",
  4: "Owner",
};

/** The automation fields its share dialog shows. */
export type SharedAutomation = Pick<ScheduledTask, "id" | "name" | "sessionMode">;

/** The workflow fields its share dialog shows. */
export type SharedWorkflow = Pick<SavedWorkflow, "id" | "name">;

/** The gadget fields its share dialog shows; with its owner and viewer, it lists the owner. */
export type SharedGadget = Pick<Gadget, "id" | "name"> &
  Partial<Pick<Gadget, "owner_id" | "viewer_id">>;

/** The levels a gadget can be shared at (read, edit, manage), as its share dialog names them. */
const GADGET_LEVELS = [
  { value: 1, label: "Can view" },
  { value: 2, label: "Can edit" },
  { value: 3, label: "Can manage" },
] as const;
const GADGET_LEVELS_WITH_OWNER = [...GADGET_LEVELS, { value: 4, label: "Owner" }] as const;

type PermissionsModalProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
} & (
  | {
      sessionId: string;
      workspace?: string | null;
      filePath?: string;
      automation?: undefined;
      workflow?: undefined;
      gadget?: undefined;
    }
  | {
      automation: SharedAutomation;
      sessionId?: undefined;
      workspace?: undefined;
      filePath?: undefined;
      workflow?: undefined;
      gadget?: undefined;
    }
  | {
      workflow: SharedWorkflow;
      sessionId?: undefined;
      workspace?: undefined;
      filePath?: undefined;
      automation?: undefined;
      gadget?: undefined;
    }
  | {
      gadget: SharedGadget;
      sessionId?: undefined;
      workspace?: undefined;
      filePath?: undefined;
      automation?: undefined;
      workflow?: undefined;
    }
);

function shareTarget(props: PermissionsModalProps): ShareTarget {
  if (props.automation !== undefined) return { kind: "automation", id: props.automation.id };
  if (props.workflow !== undefined) return { kind: "workflow", id: props.workflow.id };
  if (props.gadget !== undefined) return { kind: "gadget", id: props.gadget.id };
  return { kind: "session", id: props.sessionId };
}

export function PermissionsModal(props: PermissionsModalProps) {
  const { workspace, filePath, open, onOpenChange } = props;
  const target = shareTarget(props);
  // An automation has no public access, workspace files or link: its grantees
  // read the sessions its runs create. Nor does a workflow: its grantees run it.
  const automation = props.automation ?? null;
  const workflow = props.workflow ?? null;
  const gadget = props.gadget ?? null;
  // Only a session has public access, workspace files and a link.
  const readOnlyShare = target.kind !== "session";
  // An automation or a workflow is shared at read access only; a gadget at any of its levels.
  const fixedReadShare = readOnlyShare && gadget === null;
  const sessionId = target.kind === "session" ? target.id : null;
  // Server sharing policy. While the boot probe is in flight we treat the
  // server as "on" (fail open) so the modal renders its full controls; the
  // server-side gate is the real enforcement point regardless.
  const info = useServerInfo();
  const sharingMode = info === "loading" ? "on" : info.sharing_mode;
  const sharingOff = sharingMode === "off";
  const sharingReadOnly = sharingMode === "read_only" || sharingMode === "restricted_read_only";
  const { session, error: sessionError } = useSession(open && !sharingOff ? sessionId : null);
  const sharingWorkspace = workspace ?? (filePath ? session?.workspace : undefined);
  const workspaceBlocked =
    sharingMode === "restricted_read_only" && workspaceSharingBlocked(sharingWorkspace);
  // Disabling public sharing prevents new grants but still permits revocation.
  const publicSharingEnabled = info === "loading" ? true : info.public_sharing_enabled;
  const { data: livePublicMaxLevel } = usePublicSharingMaxLevel(
    open && !sharingOff && !readOnlyShare,
  );
  const publicMaxLevel =
    (livePublicMaxLevel ?? (info === "loading" ? "read" : info.public_sharing_max_level)) === "edit"
      ? 2
      : 1;
  // In "off" mode never fetch the grant list — the modal short-circuits to a
  // notice below. Closing keeps the loaded list through the close animation.
  // With sharing off, a gadget's managers still see who has access, and can take it away.
  const revokeOnly = sharingOff && gadget !== null;
  const {
    data: grantList,
    isLoading,
    isError: grantsFailed,
    error: grantsError,
    refetch: refreshPermissions,
  } = useGrantList(target, open && (!sharingOff || revokeOnly));
  const permissions = grantList?.permissions;
  // The server names an automation only to its owner, while it replaces this
  // session's shares on each run.
  const managingAutomation = grantList?.managedByAutomation ?? null;
  const grant = useGrantPermission(target);
  const revoke = useRevokePermission(target);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);
  const [showQr, setShowQr] = useState(false);
  const [checkingPermissions, setCheckingPermissions] = useState(false);
  // A change that takes the viewer's own Can manage away waits for them to confirm it.
  const [selfChange, setSelfChange] = useState<{ level: number } | null>(null);
  const blockedNoticeId = useId();

  // Whether the owner shared the workspace files with view-level collaborators.
  // Read from the (shared-cache) session snapshot; the toggle PATCHes it and
  // invalidates the snapshot so the rail's file surfaces appear/disappear.
  const queryClient = useQueryClient();
  const shareWorkspace = useMutation({
    // Only a session has workspace files, so `target` is that session.
    mutationFn: (next: boolean) => updateSession(target.id, { shareWorkspaceFiles: next }),
    onSuccess: (updated) => {
      queryClient.setQueryData<Session>(["session", sessionId], (previous) =>
        previous ? { ...previous, shareWorkspaceFiles: updated.shareWorkspaceFiles } : previous,
      );
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    },
    onError: (err: Error) => setError(err.message),
  });
  // Only a session with a workspace on disk has files to share; a plain
  // chat agent has none, so the toggle would be inert — hide it there.
  const hasWorkspace = !!session?.workspace;
  const workspaceShared = session?.shareWorkspaceFiles ?? false;
  const fileShareUnavailable = !!filePath && (!session || workspaceBlocked);
  const shareBusy =
    checkingPermissions || grant.isPending || revoke.isPending || shareWorkspace.isPending;
  const visibleError = error ?? (fileShareUnavailable ? sessionError?.message : null);

  async function prepareFileShare() {
    if (!filePath) return;
    if (fileShareUnavailable) {
      const message = "Workspace files are unavailable for sharing.";
      setError(message);
      throw new Error(message);
    }
    if (!workspaceShared) await shareWorkspace.mutateAsync(true);
  }

  async function openMobileLink() {
    setError(null);
    try {
      await prepareFileShare();
      setShowQr(true);
    } catch {
      // The workspace mutation displays its error in the dialog.
    }
  }

  const userGrants = (permissions ?? []).filter((p) => p.user_id !== PUBLIC_USER);
  // A gadget's owner is not a grant; the list names them first, fixed, for its other managers.
  const gadgetOwner =
    gadget?.owner_id !== undefined
      ? {
          user_id: gadget.owner_id,
          level: 4,
          display_name: gadget.owner_id === gadget.viewer_id ? "You" : gadget.owner_id,
        }
      : null;
  const publicGrant = (permissions ?? []).find((p) => p.user_id === PUBLIC_USER);
  const publicLevel = publicGrant ? Math.min(publicGrant.level, publicMaxLevel) : 0;

  async function grantAccess(
    userId: string,
    level: number,
    existingLevel = 0,
    ensureMinimum = false,
  ) {
    let accessGranted = existingLevel >= level;
    try {
      if (!accessGranted) {
        const saved = await grant.mutateAsync({ userId, level, ensureMinimum });
        accessGranted = true;
        // The server kept a higher grant than the one asked for: say so instead of lowering it.
        if (gadget && saved.level > level) {
          const name = saved.display_name ?? saved.user_id;
          const current = GADGET_LEVELS.find((l) => l.value === saved.level)?.label;
          setError(`${name} already has ${current ?? "more access"}. Change it in the list above.`);
        }
      }
      await prepareFileShare();
      return true;
    } catch (err) {
      const message = (err as Error).message;
      setError(
        filePath && accessGranted
          ? `Session access is granted, but file sharing failed: ${message}`
          : message,
      );
      return false;
    }
  }

  async function handleGrant(e: FormEvent) {
    e.preventDefault();
    const trimmed = newUserId.trim();
    if (
      !trimmed ||
      shareBusy ||
      workspaceBlocked ||
      fileShareUnavailable ||
      (!!filePath && permissions === undefined)
    )
      return;
    setError(null);
    setCheckingPermissions(!!filePath);
    try {
      const currentPermissions = filePath
        ? (await refreshPermissions({ throwOnError: true })).data?.permissions
        : undefined;
      const existingGrant = currentPermissions?.find((p) => p.user_id === trimmed);
      // Granting a gadget grantee less than they have would quietly take access away.
      const listed = gadget ? permissions?.find((p) => p.user_id === trimmed) : undefined;
      if (listed && listed.level > parseInt(newLevel, 10)) {
        const name = listed.display_name ?? listed.user_id;
        const current = GADGET_LEVELS.find((level) => level.value === listed.level)?.label;
        setError(`${name} already has ${current ?? "more access"}. Change it in the list above.`);
        return;
      }
      // An add never lowers access: the server keeps a higher grant, even one this list hasn't
      // loaded yet or one another manager just made.
      if (
        await grantAccess(
          trimmed,
          parseInt(newLevel, 10),
          existingGrant?.level,
          !!filePath || !!gadget,
        )
      ) {
        setNewUserId("");
        setNewLevel("1");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCheckingPermissions(false);
    }
  }

  // The viewer appears in a gadget's list only as a grantee, never as its owner.
  const viewerId = gadget?.viewer_id;
  const isViewerManage = (userId: string) =>
    viewerId !== undefined &&
    userId === viewerId &&
    (permissions?.find((p) => p.user_id === userId)?.level ?? 0) >= 3;
  // Losing Can manage closes the dialog, whose list only managers may read, and refreshes the
  // gadget, which shows the viewer's new access.
  function afterSelfChange() {
    setSelfChange(null);
    void queryClient.invalidateQueries({ queryKey: ["gadgets"] });
    onOpenChange(false);
  }

  function handleRevoke(userId: string, confirmed = false) {
    setError(null);
    if (!confirmed && isViewerManage(userId)) {
      setSelfChange({ level: 0 });
      return;
    }
    revoke.mutate(userId, {
      onSuccess: () => {
        if (userId === viewerId) afterSelfChange();
      },
      onError: (err) => setError(err.message),
    });
  }

  async function handleChangeLevel(userId: string, level: number, confirmed = false) {
    if (workspaceBlocked || fileShareUnavailable) return;
    setError(null);
    if (!confirmed && level < 3 && isViewerManage(userId)) {
      setSelfChange({ level });
      return;
    }
    if ((await grantAccess(userId, level)) && confirmed) afterSelfChange();
  }

  async function handlePublicLevel(value: string) {
    const level = Number(value);
    if (level > 0 && (workspaceBlocked || !publicSharingEnabled)) return;
    setError(null);
    if (level > 0) {
      await grantAccess(PUBLIC_USER, level);
    } else {
      revoke.mutate(PUBLIC_USER, {
        onError: (err) => setError(err.message),
      });
    }
  }

  let description = "Invite others to view or collaborate on this session.";
  if (automation) {
    description = "Each run's session is shared with the people below, with Read access.";
  } else if (workflow) {
    description =
      "The people below can view this workflow, run it and make a copy. Their runs stay private.";
  } else if (gadget && revokeOnly) {
    description = `${SHARING_OFF_REASON} The people below keep their access until you remove it.`;
  } else if (gadget) {
    description = sharingReadOnly
      ? "This server allows read-only sharing — invite others to view this gadget."
      : "Can view lets people use it; Can edit also lets them change its data and publish versions, which decide what its actions ask an agent to do; Can manage also lets them share and delete it.";
  } else if (filePath) {
    description = "This link opens the selected file in full screen.";
  } else if (sharingReadOnly) {
    description = "This server allows read-only sharing — invite others to view this session.";
  }

  // "off" short-circuit: sharing is disabled server-wide, so skip the whole
  // grant UI and show a plain notice instead. Mirrors the server's 403. A gadget
  // keeps its list, so existing grants can still be removed.
  if (sharingOff && !revokeOnly) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">Sharing unavailable</DialogTitle>
            <DialogDescription>{SHARING_OFF_REASON}</DialogDescription>
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
          <DialogTitle className="flex items-center gap-2">
            {automation
              ? "Share this automation"
              : workflow
                ? "Share this workflow"
                : gadget
                  ? "Share this gadget"
                  : filePath
                    ? "Share this file"
                    : "Share this session"}
          </DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-3">
              <p>{description}</p>
              {automation?.sessionMode === "continue" && (
                <p>
                  Sharing it replaces the shares on the session it continues with this list,
                  including people added there by hand, even at Edit or Manage. The owner and
                  General access are kept.
                </p>
              )}
              {sharingReadOnly && !filePath && !gadget && (
                <p>
                  Please be careful when sharing. Read access will allow for reading of all session
                  outputs.
                </p>
              )}
            </div>
          </DialogDescription>
        </DialogHeader>
        {managingAutomation && (
          <AutomationCallout
            title="Sharing is managed by an automation"
            testId="share-managed-by-automation"
          >
            Shares added or removed here are replaced with the automation&apos;s list on each run of{" "}
            <AutomationLink
              task={managingAutomation}
              to={automationSharePath(info, managingAutomation)}
              onNavigate={() => onOpenChange(false)}
            />{" "}
            and whenever its sharing changes. General access isn&apos;t affected.
          </AutomationCallout>
        )}
        {filePath && (
          <Alert role="note" className="bg-muted/50">
            <InfoIcon />
            <AlertTitle>Shares the entire session and all workspace files</AlertTitle>
            <AlertDescription>
              Includes all messages. Sharing enables files for every session reader. New invites
              default to read-only; existing permissions stay.
            </AlertDescription>
          </Alert>
        )}

        {!readOnlyShare && (publicSharingEnabled || publicGrant) && (
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
                shareBusy ||
                ((workspaceBlocked || fileShareUnavailable) && !publicGrant)
              }
              componentId="diagnostics.permissions.public_level"
              valueHasNoPii
            >
              <SelectTrigger className="h-8 w-28 shrink-0" aria-label="General access">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                <SelectItem value="0">No access</SelectItem>
                <SelectItem
                  value="1"
                  disabled={workspaceBlocked || fileShareUnavailable || !publicSharingEnabled}
                >
                  Read
                </SelectItem>
                {publicMaxLevel === 2 && (
                  <SelectItem
                    value="2"
                    disabled={
                      sharingReadOnly ||
                      workspaceBlocked ||
                      fileShareUnavailable ||
                      !publicSharingEnabled
                    }
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
          <div className="rounded-lg border px-3 py-2" data-testid="share-workspace-files">
            {filePath && <p className="mb-2 break-all font-mono text-sm">{filePath}</p>}
            <div className="flex items-center justify-between">
              <div>
                <p className="text-ui font-medium">
                  {filePath ? "All workspace files" : "Workspace files"}
                </p>
                <p className="text-sm text-muted-foreground">
                  {filePath
                    ? "Let everyone with Read access browse all workspace files"
                    : "Let people with view access browse this session's files"}
                </p>
              </div>
              <Switch
                checked={workspaceShared}
                onCheckedChange={(checked) => {
                  setError(null);
                  shareWorkspace.mutate(checked);
                }}
                disabled={shareBusy}
                componentId="diagnostics.permissions.share_workspace_toggle"
              />
            </div>
          </div>
        )}

        {/* Current grants. DialogContent is a grid, and grid items default to
            min-width:auto — without min-w-0 a long nowrap email sets the whole
            track's min-content and pushes every row past the dialog edge. */}
        <div className="min-w-0" data-testid="share-grants">
          {isLoading ? (
            <p className="text-ui text-muted-foreground py-2">Loading…</p>
          ) : grantsFailed && !permissions ? (
            <p role="alert" className="text-ui text-destructive py-2">
              Couldn&apos;t load who has access: {grantsError.message}
            </p>
          ) : userGrants.length === 0 && !gadgetOwner ? (
            <p className="text-ui text-muted-foreground py-2">No grants yet.</p>
          ) : (
            <>
              {/* Column headers */}
              <div className="flex items-center gap-2 px-2 pb-0.5">
                <span className="flex-1 text-sm font-medium uppercase tracking-wide text-muted-foreground">
                  Name
                </span>
                <span
                  className={cn(
                    "text-sm font-medium uppercase tracking-wide text-muted-foreground",
                    gadget ? "w-32" : "w-28",
                  )}
                >
                  Permission
                </span>
                <span className="size-7 shrink-0" aria-hidden="true" />
              </div>
              <div className="max-h-48 overflow-y-auto">
                {gadgetOwner && (
                  <GrantRow
                    permission={gadgetOwner}
                    onRevoke={handleRevoke}
                    onChangeLevel={handleChangeLevel}
                    busy={shareBusy}
                    levelDisabled
                    readOnly
                    levels={GADGET_LEVELS_WITH_OWNER}
                  />
                )}
                {userGrants.map((p) => (
                  <GrantRow
                    key={p.user_id}
                    permission={p}
                    onRevoke={handleRevoke}
                    onChangeLevel={handleChangeLevel}
                    busy={shareBusy}
                    levelDisabled={fileShareUnavailable}
                    readOnly={revokeOnly || (sharingReadOnly && !gadget) || fixedReadShare}
                    levels={
                      gadget
                        ? sharingReadOnly
                          ? // Read-only sharing still lets a grant go down to Can view.
                            GADGET_LEVELS.filter(
                              (level) => level.value === 1 || level.value === p.level,
                            )
                          : GADGET_LEVELS
                        : undefined
                    }
                    youLabel={p.user_id === viewerId ? "you" : undefined}
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

        {selfChange && (
          <Alert role="alertdialog" aria-label="Change your own access" className="bg-muted/50">
            <InfoIcon />
            <AlertTitle>
              {selfChange.level === 0 ? "Remove your own access?" : "Give up Can manage?"}
            </AlertTitle>
            <AlertDescription>
              <p>
                {selfChange.level === 0
                  ? "You won’t be able to open this gadget again unless someone shares it with you."
                  : "You won’t be able to share or delete this gadget, or see who has access, unless someone gives you Can manage again."}
              </p>
              <div className="mt-2 flex gap-2">
                <Button
                  size="sm"
                  variant="destructive"
                  loading={shareBusy}
                  componentId="diagnostics.permissions.confirm_self_change"
                  onClick={() => {
                    if (viewerId === undefined) return;
                    if (selfChange.level === 0) handleRevoke(viewerId, true);
                    else void handleChangeLevel(viewerId, selfChange.level, true);
                  }}
                >
                  {selfChange.level === 0 ? "Remove my access" : "Give up Can manage"}
                </Button>
                <Button size="sm" variant="outline" onClick={() => setSelfChange(null)}>
                  Cancel
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        {/* Add grant form */}
        {!revokeOnly && (
          <form onSubmit={handleGrant} className="flex items-end gap-2">
            <div className="flex-1">
              <label htmlFor="perm-user" className="text-sm font-medium text-muted-foreground">
                User ID
              </label>
              <AddUserField value={newUserId} onChange={setNewUserId} disabled={workspaceBlocked} />
            </div>
            {fixedReadShare ? (
              <div>
                <span className="text-sm font-medium text-muted-foreground">Level</span>
                <p className="mt-1 flex h-8 w-24 items-center px-3 text-ui text-muted-foreground">
                  Read
                </p>
              </div>
            ) : (
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
                  <SelectTrigger
                    className={cn("mt-1", gadget ? "w-32" : "w-24")}
                    aria-label="New user permission"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper" align="start">
                    {gadget ? (
                      // Read-only sharing caps new grants at view.
                      GADGET_LEVELS.filter((level) => !sharingReadOnly || level.value === 1).map(
                        (level) => (
                          <SelectItem key={level.value} value={String(level.value)}>
                            {level.label}
                          </SelectItem>
                        ),
                      )
                    ) : (
                      <>
                        <SelectItem value="1">Read</SelectItem>
                        {/* Read-only sharing caps new grants at view; hide Edit. */}
                        {!sharingReadOnly && <SelectItem value="2">Edit</SelectItem>}
                      </>
                    )}
                  </SelectContent>
                </Select>
              </div>
            )}
            <Button
              type="submit"
              size="sm"
              loading={checkingPermissions || grant.isPending || shareWorkspace.isPending}
              disabled={
                workspaceBlocked ||
                !newUserId.trim() ||
                fileShareUnavailable ||
                (!!filePath && permissions === undefined)
              }
              aria-describedby={workspaceBlocked ? blockedNoticeId : undefined}
              componentId="diagnostics.permissions.grant"
            >
              <UserPlusIcon className="mr-1 size-3.5" />
              Grant
            </Button>
          </form>
        )}

        {visibleError && <p className="text-sm text-destructive">{visibleError}</p>}

        {/* Keep the default stacking below sm: one row of Copy link + QR +
            Done overflows a phone-width dialog and widens the whole grid. */}
        <DialogFooter className="sm:justify-between">
          {gadget && <GadgetCopyLinkButton gadgetId={gadget.id} onError={setError} />}
          {sessionId !== null && (
            <div className="flex items-center gap-2">
              <CopyLinkButton
                sessionId={sessionId}
                filePath={filePath}
                beforeCopy={filePath ? prepareFileShare : undefined}
                onError={setError}
                disabled={!!filePath && (fileShareUnavailable || shareBusy)}
              />
              <Button
                variant="ghost"
                onClick={openMobileLink}
                disabled={!!filePath && (fileShareUnavailable || shareBusy)}
                className="gap-1.5 text-primary"
              >
                <QrCodeIcon className="size-3.5" />
                Open in mobile app
              </Button>
            </div>
          )}
          {/* Right-aligned with or without the link buttons, e.g. for an automation. */}
          <Button variant="outline" className="sm:ml-auto" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>

      {/* Separate modal for the QR code so the share dialog stays compact. */}
      {sessionId !== null && (
        <QrCodeDialog
          sessionId={sessionId}
          filePath={filePath}
          open={showQr}
          onOpenChange={setShowQr}
        />
      )}
    </Dialog>
  );
}

interface AddUserFieldProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

/**
 * The permissions "add user" field. Stays a plain text input unless the host
 * injects a `searchUsers` provider (see `lib/host.ts`), in which case it becomes
 * a free-text suggestion combobox. Free typing is always allowed; suggestions
 * are an aid, not a constraint.
 */
function AddUserField({ value, onChange, disabled }: AddUserFieldProps) {
  // Read once: the host installs config eagerly before first render, so the
  // branch is stable for the lifetime of the modal.
  const searchUsers = getOmnigentUserSearch();
  if (!searchUsers) {
    return (
      <Input
        id="perm-user"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="alice@example.com"
        className="mt-1 h-8"
        disabled={disabled}
      />
    );
  }
  return <AddUserCombobox value={value} onChange={onChange} disabled={disabled} />;
}

// Hand-rolled accessible combobox/listbox rendered INLINE (no Radix Popover).
//
// Two reasons we don't portal this into a Radix Popover:
//   1. cmdk/Popover keyboard + scroll behavior only works when their own input
//      owns focus, but the typing field here is the form's native `Input`.
//   2. This field lives inside a Radix `Dialog`, whose scroll lock
//      (react-remove-scroll) `preventDefault`s wheel events over any portaled
//      content rendered OUTSIDE the dialog content subtree — that's what made
//      the suggestion list visually scrollable but impossible to wheel over.
//
// Rendering the list as an absolutely-positioned descendant of the dialog keeps
// it inside the scroll-lock's allow-list (wheel works) and lets us own the
// combobox a11y roles + keyboard handling directly.
function AddUserCombobox({ value, onChange, disabled }: AddUserFieldProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const { suggestions, isLoading } = useUserSearch(value);
  const hasQuery = value.trim().length > 0;
  // `disabled` can flip on while the list is open (the sharing policy resolves
  // after the dialog mounts), so close it rather than leave it clickable.
  const isOpen = open && hasQuery && !disabled;

  const listId = useId();
  const listRef = useRef<HTMLDivElement>(null);

  // Reset the active option whenever the result set changes.
  useEffect(() => {
    setActiveIndex(-1);
  }, [suggestions]);

  // Keep the active option scrolled into view during keyboard navigation.
  useEffect(() => {
    if (activeIndex < 0) return;
    const el = listRef.current?.children[activeIndex] as HTMLElement | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const optionId = (index: number) => `${listId}-opt-${index}`;
  const activeId = activeIndex >= 0 ? optionId(activeIndex) : undefined;

  function commit(index: number) {
    const suggestion = suggestions[index];
    if (!suggestion) return;
    onChange(suggestion.userId);
    setOpen(false);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!isOpen) {
        setOpen(true);
        return;
      }
      setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1));
    } else if (e.key === "ArrowUp") {
      if (!isOpen) return;
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      // Only intercept Enter to pick a highlighted suggestion; otherwise let it
      // fall through to submit the grant form with the typed value.
      if (isOpen && activeIndex >= 0) {
        e.preventDefault();
        commit(activeIndex);
      }
    } else if (e.key === "Escape") {
      // When the dropdown is open, Escape dismisses only the suggestions.
      // Stop it from bubbling to the enclosing Radix Dialog, which would
      // otherwise close the whole modal in the same keystroke.
      if (isOpen) {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
      }
    }
  }

  return (
    <div className="relative">
      <Input
        id="perm-user"
        role="combobox"
        aria-expanded={isOpen}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        // Closes when focus leaves the field. Option clicks use `mousedown` +
        // preventDefault below, so they don't blur the input before committing.
        onBlur={() => setOpen(false)}
        onKeyDown={handleKeyDown}
        placeholder="alice@example.com"
        className="mt-1 h-8"
        autoComplete="off"
        disabled={disabled}
      />
      {isOpen && (
        // Wider than the (narrow) field so suggested emails aren't truncated.
        <div className="absolute left-0 top-full z-50 mt-1 w-96 rounded-[12px] border border-border bg-popover p-2 text-popover-foreground shadow-menu">
          {isLoading ? (
            <div className="py-6 text-center text-ui text-muted-foreground">Searching…</div>
          ) : suggestions.length === 0 ? (
            <div className="py-6 text-center text-ui text-muted-foreground">No matches</div>
          ) : (
            <div ref={listRef} id={listId} role="listbox" className="max-h-72 overflow-y-auto">
              {suggestions.map((s, index) => (
                <div
                  key={s.userId}
                  id={optionId(index)}
                  role="option"
                  aria-selected={index === activeIndex}
                  onMouseEnter={() => setActiveIndex(index)}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    commit(index);
                  }}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-ui",
                    index === activeIndex && "bg-muted dark:bg-muted/50",
                  )}
                >
                  {/* Primary label fills the row and truncates. When the host
                      provides only an email (no display name), it shows alone;
                      the muted secondary email is only rendered when there's a
                      distinct display name to pair it with. */}
                  <span className="min-w-0 flex-1 truncate">{s.displayName ?? s.userId}</span>
                  {s.displayName && s.displayName !== s.userId && (
                    <span className="ml-2 shrink-0 truncate text-sm text-muted-foreground">
                      {s.userId}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The `omnigent://<host>/c/<session_id>` deep link encoded into the share QR
 * code. The host (with port when non-default) is parsed from the same shareable
 * URL `getShareableSessionLink` resolves — so standalone and embedded (host-transformed)
 * origins agree on the same server the desktop shell's deep-link handler keys
 * off of (see `electron/src/deepLink.js`). The path is always basename-less
 * `/c/<id>` with optional `file` and `fullscreen` query parameters; the workspace
 * mount is server-determined and intentionally absent.
 */
function getDeepLink(
  sessionId: string,
  rebasePath: (path: string) => string,
  filePath?: string,
): string {
  const url = getShareableSessionLink(sessionId, rebasePath, filePath);
  const path = getSessionSharePath(sessionId, filePath);
  try {
    const { host } = new URL(url);
    return `omnigent://${host}${path}`;
  } catch {
    // Unparseable transform output: fall back to the current origin's host.
    return `omnigent://${window.location.host}${path}`;
  }
}

/** Copies the link that opens a gadget; only the people it is shared with can open it. */
function GadgetCopyLinkButton({
  gadgetId,
  onError,
}: {
  gadgetId: string;
  onError: (message: string | null) => void;
}) {
  const [copied, setCopied] = useState(false);
  const rebasePath = useRebasePath();
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);
  return (
    <Button
      variant="ghost"
      className="gap-1.5 text-primary"
      componentId="diagnostics.permissions.copy_gadget_link"
      onClick={() => {
        onError(null);
        copyText(gadgetLink(gadgetId, rebasePath)).then(
          () => setCopied(true),
          () => onError("Couldn’t copy the link."),
        );
      }}
    >
      {copied ? <CheckIcon className="size-3.5" /> : <LinkIcon className="size-3.5" />}
      {copied ? "Copied" : "Copy link"}
    </Button>
  );
}

function CopyLinkButton({
  sessionId,
  filePath,
  beforeCopy,
  onError,
  disabled,
}: {
  sessionId: string;
  filePath?: string;
  beforeCopy?: () => Promise<void>;
  onError: (message: string | null) => void;
  disabled?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const rebasePath = useRebasePath();
  const label = filePath ? "Copy file link" : "Copy link";

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);

  const handleCopy = useCallback(async () => {
    const url = getShareableSessionLink(sessionId, rebasePath, filePath);
    setCopied(false);
    onError(null);
    const [clipboard, prepared] = await Promise.allSettled([copyText(url), beforeCopy?.()]);
    if (clipboard.status === "rejected") {
      console.warn("Failed to copy link to clipboard", clipboard.reason);
    }
    if (prepared.status === "rejected") {
      console.warn("Failed to prepare file share", prepared.reason);
      const message = (prepared.reason as Error).message;
      if (clipboard.status === "fulfilled") {
        onError(
          `Link copied, but file sharing failed: ${message}. Please try again before sharing this link.`,
        );
      } else {
        onError(`Could not copy the link, and file sharing failed: ${message}. Please try again.`);
      }
      return;
    }
    if (clipboard.status === "rejected") {
      if (filePath) {
        onError(
          "Could not copy the link. Workspace files are shared with everyone who has Read access. Please try copying again.",
        );
      } else {
        onError("Could not copy the link. Please try again or use the mobile QR code.");
      }
      return;
    }
    setCopied(true);
  }, [sessionId, rebasePath, filePath, beforeCopy, onError]);

  return (
    <Button
      variant="ghost"
      onClick={handleCopy}
      disabled={disabled}
      className="gap-1.5 text-primary"
      componentId="diagnostics.permissions.copy_link"
    >
      {copied ? <CheckIcon className="size-3.5" /> : <LinkIcon className="size-3.5" />}
      {copied ? "Copied!" : label}
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
  filePath,
  open,
  onOpenChange,
}: {
  sessionId: string;
  filePath?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rebasePath = useRebasePath();
  const deepLink = getDeepLink(sessionId, rebasePath, filePath);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <QrCodeIcon className="size-4" />
            Open in mobile app
          </DialogTitle>
          <DialogDescription>
            {filePath
              ? "Scan with your phone's camera to open this file in the Omnigent app."
              : "Scan with your phone's camera to open this session in the Omnigent app."}
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
              aria-label={`QR code to open this ${filePath ? "file" : "session"} in the Omnigent app`}
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
  levelDisabled,
  readOnly,
  levels,
  youLabel,
}: {
  permission: ShareGrant;
  onRevoke: (userId: string) => void;
  onChangeLevel: (userId: string, level: number) => void;
  busy: boolean;
  levelDisabled: boolean;
  readOnly: boolean;
  /** The levels the row offers, when the target names its own (a gadget). */
  levels?: readonly { value: number; label: string }[];
  /** Marks the viewer's own row. */
  youLabel?: string;
}) {
  const isOwner = permission.level === 4;
  // Manage is not grantable from a session's dialog, so a pre-existing manage
  // grant renders as a fixed label rather than a dropdown choice. Unlike the
  // owner row it can still be revoked.
  const isManage = permission.level === 3 && levels === undefined;
  // Read-only sharing mode: existing grants can't be re-leveled, so the level
  // shows as a fixed label (like owner/manage) — but the row stays revocable.
  const fixedLevel = isOwner || isManage || readOnly || (levels !== undefined && levels.length < 2);
  // A gadget's levels are longer words than a session's Read and Edit.
  const levelWidth = levels ? "w-32" : "w-28";
  // An automation's grants carry a display name; the ID follows when it differs.
  const name = permission.display_name ?? permission.user_id;

  return (
    <div className="flex items-center gap-2 rounded-md px-2 py-0.5 hover:bg-muted/50">
      {/* Tail truncation keeps the local part — the distinguishing half when
          every grantee shares one company domain — and the title tooltip
          carries the full id. */}
      <span className="flex-1 truncate text-ui" title={permission.user_id}>
        {name}
        {youLabel && <span className="ml-1 text-muted-foreground">({youLabel})</span>}
        {name !== permission.user_id && (
          <span className="ml-2 text-sm text-muted-foreground">{permission.user_id}</span>
        )}
      </span>
      {fixedLevel ? (
        <span
          className={cn("flex h-8 items-center px-3 text-ui text-muted-foreground", levelWidth)}
        >
          {levels?.find((level) => level.value === permission.level)?.label ??
            LEVEL_LABELS[permission.level] ??
            "Read"}
        </span>
      ) : (
        <Select
          value={String(permission.level)}
          onValueChange={(v) => onChangeLevel(permission.user_id, parseInt(v, 10))}
          disabled={busy || levelDisabled}
        >
          <SelectTrigger
            className={cn("h-8", levelWidth)}
            aria-label={`Permission level for ${permission.user_id}`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            {levels ? (
              levels.map((level) => (
                <SelectItem key={level.value} value={String(level.value)}>
                  {level.label}
                </SelectItem>
              ))
            ) : (
              <>
                <SelectItem value="1">Read</SelectItem>
                <SelectItem value="2">Edit</SelectItem>
              </>
            )}
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
          aria-label={`Revoke access for ${name}`}
        >
          <Trash2Icon className="size-3.5" />
        </Button>
      )}
    </div>
  );
}
