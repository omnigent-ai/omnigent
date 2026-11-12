/**
 * Share dialog for a session.
 *
 * People added by email and link ("general") access are independent grants:
 * changing one never re-levels the other. Workspace-file access for Read-level
 * viewers is a separate session setting behind the gear. Manage-level (3)
 * access is required to open it.
 */

import {
  type ComponentProps,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  CheckIcon,
  ChevronDownIcon,
  FolderIcon,
  GlobeIcon,
  LinkIcon,
  LockIcon,
  QrCodeIcon,
  SettingsIcon,
  UsersIcon,
} from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Switch } from "@/components/ui/switch";
import { useGrantPermission, usePermissions, useRevokePermission } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { usePublicSharingMaxLevel } from "@/hooks/useSharing";
import { useUserSearch } from "@/hooks/useUserSearch";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { copyText } from "@/lib/clipboard";
import { updateSession } from "@/lib/sessionsApi";
import { getOmnigentUserSearch } from "@/lib/host";
import { getShareableSessionLink } from "@/lib/shareLink";
import { getCurrentUserId } from "@/lib/identity";
import {
  LEVEL_EDIT,
  LEVEL_MANAGE,
  LEVEL_OWNER,
  LEVEL_READ,
  type Permission,
  workspaceSharingBlocked,
} from "@/lib/permissionsApi";
import { useRebasePath } from "@/lib/routing";
import {
  capRole,
  grantableRoles,
  joinRoleNames,
  roleHint,
  roleLabel,
  roleRank,
  roleVerb,
} from "@/lib/shareRoles";
import { cn } from "@/lib/utils";

const PUBLIC_USER = "__public__";

// Radix wraps scroll content in a display:table div, which defeats truncation.
const SCROLL_CONTENT_BLOCK = "[&>[data-slot=scroll-area-viewport]>div]:!block";

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
  // While the boot probe is in flight, fail open; the server gate is the real check.
  const info = useServerInfo();
  const sharingMode = info === "loading" ? "on" : info.sharing_mode;
  const sharingOff = sharingMode === "off";
  const publicSharingEnabled = info === "loading" ? true : info.public_sharing_enabled;
  // Comment grants are opt-in per server; fail closed so the role never flashes in.
  const commentSharing = info !== "loading" && info.comment_sharing_enabled === true;
  const { data: livePublicMaxLevel } = usePublicSharingMaxLevel(open && !sharingOff);
  const publicMax =
    (livePublicMaxLevel ?? (info === "loading" ? "read" : info.public_sharing_max_level)) === "edit"
      ? LEVEL_EDIT
      : LEVEL_READ;
  const {
    data: permissions,
    isLoading,
    error: permissionsError,
    refetch: refetchPermissions,
  } = usePermissions(open && !sharingOff ? sessionId : null);
  // The sidebar opens this without a workspace prop; fall back to the snapshot.
  const { session } = useSession(open && !sharingOff ? sessionId : null);
  const sessionWorkspace = workspace ?? session?.workspace;
  // A home or root cwd on a restricted server shares Read only, never its files.
  const filesWithheld =
    sharingMode === "restricted_read_only" && workspaceSharingBlocked(sessionWorkspace);
  const grantable = grantableRoles(sharingMode, filesWithheld, commentSharing);
  const ceiling = grantable[grantable.length - 1];
  const ceilingHint = filesWithheld
    ? "Sessions in a home or root folder can only be shared with read access"
    : `Only ${joinRoleNames(grantable)} access can be shared on this server`;
  const linkGrantable = grantable.filter((level) => roleRank(level) <= roleRank(publicMax));
  const linkCeiling = linkGrantable[linkGrantable.length - 1];
  // Without the grant list we can't tell who has access, so don't offer changes.
  const permissionsFailed = !!permissionsError && !permissions;
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);
  const queryClient = useQueryClient();
  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState(LEVEL_READ);
  const [error, setError] = useState<string | null>(null);
  const [showQr, setShowQr] = useState(false);
  const [view, setView] = useState<"main" | "settings">("main");
  const controlsId = useId();
  const currentUserId = getCurrentUserId();
  const people = (permissions ?? []).filter((permission) => permission.user_id !== PUBLIC_USER);
  const publicGrant = permissions?.find((permission) => permission.user_id === PUBLIC_USER);
  const linkLevel = publicGrant ? capRole(publicGrant.level, linkCeiling) : null;
  const busy = isLoading || grant.isPending || revoke.isPending;
  const orderedPeople = [...people].sort(
    (first, second) => personRank(first, currentUserId) - personRank(second, currentUserId),
  );
  const hasWorkspace = !!session?.workspace;
  const filesShared = !!session?.shareWorkspaceFiles && !filesWithheld;
  // Comment and Edit always include workspace files; only Read follows the switch.
  const alwaysWithFiles = commentSharing ? "Comment and edit" : "Edit";
  const reportError = (failure: Error) => setError(failure.message);

  const shareWorkspace = useMutation({
    mutationFn: (next: boolean) => updateSession(sessionId, { shareWorkspaceFiles: next }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
    onError: reportError,
  });

  useEffect(() => {
    if (!open) setView("main");
  }, [open]);

  function handleGrant(event: FormEvent) {
    event.preventDefault();
    const userId = newUserId.trim();
    if (!userId || permissionsFailed) return;
    setError(null);
    grant.mutate(
      { userId, level: grantable.includes(newLevel) ? newLevel : LEVEL_READ },
      {
        onSuccess: () => {
          setNewUserId("");
          setNewLevel(LEVEL_READ);
        },
        onError: reportError,
      },
    );
  }

  function handleRevoke(userId: string) {
    setError(null);
    revoke.mutate(userId, { onError: reportError });
  }

  function handleChangeLevel(userId: string, level: number) {
    setError(null);
    grant.mutate({ userId, level }, { onError: reportError });
  }

  // null revokes link access; a level grants it. Named grants are never touched.
  function handleLinkLevel(level: number | null) {
    if (level !== null && !publicSharingEnabled) return;
    setError(null);
    if (level === null) {
      revoke.mutate(PUBLIC_USER, { onError: reportError });
    } else {
      grant.mutate({ userId: PUBLIC_USER, level }, { onError: reportError });
    }
  }

  if (sharingOff) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Sharing unavailable</DialogTitle>
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

  const linkUnavailable = !publicSharingEnabled && !publicGrant;
  const linkDescription = linkUnavailable
    ? "Link sharing is turned off on this Omnigent server"
    : linkLevel !== null
      ? `Everyone with this link can ${roleVerb(linkLevel)}`
      : "Only people added above can open this session";

  const main = (
    <div className="flex flex-col gap-5">
      <form
        onSubmit={handleGrant}
        className={cn(
          "grid min-w-0 items-center gap-2",
          newUserId ? "grid-cols-[minmax(0,1fr)_auto_auto]" : "grid-cols-1",
        )}
      >
        <label htmlFor="perm-user" className="sr-only">
          User ID
        </label>
        <AddUserField value={newUserId} onChange={setNewUserId} disabled={permissionsFailed} />
        {newUserId && (
          <>
            {grantable.length > 1 ? (
              <RoleMenu
                value={newLevel}
                grantable={grantable}
                ceiling={ceiling}
                ceilingHint={ceilingHint}
                onChange={setNewLevel}
              >
                <RoleTrigger aria-label="New user permission">{roleLabel(newLevel)}</RoleTrigger>
              </RoleMenu>
            ) : (
              <span className="px-2.5 text-sm text-muted-foreground">{roleLabel(LEVEL_READ)}</span>
            )}
            <Button
              type="submit"
              loading={grant.isPending}
              disabled={permissionsFailed || !newUserId.trim()}
              componentId="diagnostics.permissions.grant"
            >
              Add
            </Button>
          </>
        )}
      </form>

      <div className="min-w-0 space-y-2" data-testid="share-grants">
        <SectionLabel icon={<UsersIcon className="size-3.5" aria-hidden="true" />}>
          People with access
        </SectionLabel>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : permissionsFailed ? (
          <div role="alert" className="flex items-center justify-between gap-2">
            <p className="min-w-0 text-sm text-destructive">
              Couldn&apos;t load who has access: {permissionsError.message}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={() => void refetchPermissions()}
              componentId="diagnostics.permissions.retry_list"
            >
              Retry
            </Button>
          </div>
        ) : orderedPeople.length === 0 ? (
          <p className="text-sm text-muted-foreground">No one has access yet.</p>
        ) : (
          // Shrinks in short windows so only this list scrolls, not the whole dialog.
          <ScrollArea
            type="auto"
            className={cn(
              "-mr-3 pr-3",
              SCROLL_CONTENT_BLOCK,
              "[&>[data-slot=scroll-area-viewport]]:max-h-[clamp(6rem,calc(85vh-27rem),15rem)]",
            )}
          >
            <ul className="space-y-0.5" aria-label="People with access">
              {orderedPeople.map((permission) => (
                <PersonRow
                  key={permission.user_id}
                  permission={permission}
                  isYou={permission.user_id === currentUserId}
                  grantable={grantable}
                  ceiling={ceiling}
                  ceilingHint={ceilingHint}
                  busy={busy}
                  onChangeLevel={handleChangeLevel}
                  onRevoke={handleRevoke}
                />
              ))}
            </ul>
          </ScrollArea>
        )}
      </div>

      <div className="space-y-2 border-t border-border pt-5" data-testid="share-general-access">
        <SectionLabel icon={<LinkIcon className="size-3.5" aria-hidden="true" />}>
          General access
        </SectionLabel>
        <div className="flex min-w-0 items-start gap-3 py-1">
          <span
            className={cn(
              "flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground",
              linkLevel !== null && "bg-primary/15 text-primary",
            )}
            aria-hidden="true"
          >
            {linkLevel !== null ? (
              <GlobeIcon className="size-4" />
            ) : (
              <LockIcon className="size-4" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="-ml-2.5 gap-1 font-medium"
                    aria-label="General access"
                    disabled={busy || permissionsFailed || linkUnavailable}
                    componentId="diagnostics.permissions.public_level"
                  >
                    {linkLevel !== null ? "Anyone with the link" : "Restricted"}
                    <ChevronDownIcon
                      className="size-3.5 text-muted-foreground"
                      aria-hidden="true"
                    />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-72">
                  <DropdownMenuRadioGroup
                    value={linkLevel !== null ? "link" : "restricted"}
                    onValueChange={(next) =>
                      handleLinkLevel(next === "link" ? (linkLevel ?? LEVEL_READ) : null)
                    }
                  >
                    <DropdownMenuRadioItem value="restricted">
                      <RoleOption
                        label="Restricted"
                        hint="Only people added above can open this session"
                      />
                    </DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="link" disabled={!publicSharingEnabled}>
                      <RoleOption
                        label="Anyone with the link"
                        hint={
                          publicSharingEnabled
                            ? `Everyone with this link can ${roleVerb(linkLevel ?? LEVEL_READ)}`
                            : "Link sharing is turned off on this Omnigent server"
                        }
                      />
                    </DropdownMenuRadioItem>
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>
              {/* Always laid out so turning the link on never shifts the row. */}
              <div className={cn("shrink-0", linkLevel === null && "invisible")}>
                {linkGrantable.length > 1 ? (
                  <RoleMenu
                    value={linkLevel ?? LEVEL_READ}
                    grantable={linkGrantable}
                    ceiling={linkCeiling}
                    ceilingHint={`Link access is limited to ${joinRoleNames([linkCeiling])} on this server`}
                    onChange={(level) => handleLinkLevel(level)}
                  >
                    <RoleTrigger
                      aria-label="Link access permission"
                      tabIndex={linkLevel === null ? -1 : undefined}
                      disabled={busy || linkLevel === null}
                    >
                      {roleLabel(linkLevel ?? LEVEL_READ)}
                    </RoleTrigger>
                  </RoleMenu>
                ) : (
                  <span className="px-2.5 py-1 text-sm text-muted-foreground">
                    {roleLabel(LEVEL_READ)}
                  </span>
                )}
              </div>
            </div>
            <p className="truncate text-xs leading-snug text-muted-foreground">{linkDescription}</p>
          </div>
        </div>
        {hasWorkspace && (
          <div
            className="flex items-start gap-2 text-xs leading-4 text-muted-foreground"
            data-testid="share-workspace-files"
          >
            {filesWithheld ? (
              <FolderOffIcon className="mt-px size-3.5 shrink-0" />
            ) : (
              <FolderIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
            )}
            <span className="min-w-0 flex-1">
              {filesWithheld
                ? "File access is disabled for sessions in a home or root folder"
                : filesShared
                  ? "File access is enabled for users with read access"
                  : "File access is disabled for users with read access"}
            </span>
            {!filesWithheld && (
              <Button
                variant="link"
                size="xs"
                className="h-auto px-0 text-xs leading-4"
                onClick={() => setView("settings")}
                componentId="diagnostics.permissions.files_change"
              >
                Change
              </Button>
            )}
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );

  const settings = (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="icon-sm" aria-label="Back" onClick={() => setView("main")}>
          <ArrowLeftIcon className="size-4" />
        </Button>
        <p className="text-base font-semibold">Share settings</p>
      </div>
      <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-3">
        <div className="min-w-0 flex-1 space-y-1">
          <label htmlFor={`${controlsId}-files`} className="block cursor-pointer text-sm">
            Users with read access can browse workspace files
          </label>
          <p
            id={`${controlsId}-files-description`}
            className="text-[0.85em] leading-snug text-muted-foreground"
          >
            {filesWithheld
              ? "Sessions in a home or root folder never share their workspace files on this server."
              : `Applies to everyone with read access, added above or through the link. ${alwaysWithFiles} access always include workspace files.`}
          </p>
        </div>
        <Switch
          id={`${controlsId}-files`}
          aria-describedby={`${controlsId}-files-description`}
          checked={filesShared}
          onCheckedChange={(checked) => {
            setError(null);
            shareWorkspace.mutate(checked);
          }}
          disabled={shareWorkspace.isPending || filesWithheld}
          componentId="diagnostics.permissions.share_workspace_toggle"
        />
      </div>
      <p className="text-xs text-muted-foreground">
        Workspace files often hold secrets, so this is off by default.
      </p>
    </div>
  );

  const title = session?.title?.trim();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] gap-5 overflow-hidden sm:max-w-[480px]">
        <DialogHeader className="flex-row items-center justify-between gap-2 pr-8">
          <DialogTitle className="min-w-0 truncate">
            {title ? `Share "${title}"` : "Share this session"}
          </DialogTitle>
          {hasWorkspace && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Share settings"
              className="shrink-0 text-muted-foreground"
              onClick={() => setView(view === "main" ? "settings" : "main")}
              componentId="diagnostics.permissions.settings"
            >
              <SettingsIcon className="size-4" />
            </Button>
          )}
          <DialogDescription className="sr-only">
            Manage who can open this session and its workspace files.
          </DialogDescription>
        </DialogHeader>
        {/* Very short windows scroll the body with the app's overlay scrollbar. */}
        <ScrollArea
          type="auto"
          className={cn(
            "-mr-4",
            SCROLL_CONTENT_BLOCK,
            "[&>[data-slot=scroll-area-viewport]]:max-h-[calc(85vh-12rem)]",
          )}
        >
          {/* Both views share one grid cell so switching never resizes the dialog; the
              minmax column keeps a long email from widening it. */}
          <div className="grid grid-cols-[minmax(0,1fr)] pr-4 *:[grid-area:1/1]">
            <div className={cn(view !== "main" && "invisible")} inert={view !== "main"}>
              {main}
            </div>
            <div className={cn(view !== "settings" && "invisible")} inert={view !== "settings"}>
              {settings}
            </div>
          </div>
        </ScrollArea>
        {/* Outside the scroller so Copy link stays visible. */}
        <DialogFooter
          className={cn("sm:justify-between", view !== "main" && "invisible")}
          inert={view !== "main"}
        >
          <Button variant="ghost" onClick={() => setShowQr(true)} className="gap-1.5 text-primary">
            <QrCodeIcon className="size-4" />
            Open in mobile app
          </Button>
          <CopyLinkButton sessionId={sessionId} />
        </DialogFooter>
      </DialogContent>
      <QrCodeDialog sessionId={sessionId} open={showQr} onOpenChange={setShowQr} />
    </Dialog>
  );
}

/** Sort key that lists the current user first, then owners. */
function personRank(permission: Permission, currentUserId: string | null): number {
  if (permission.user_id === currentUserId) return 0;
  return permission.level === LEVEL_OWNER ? 1 : 2;
}

/** Lucide's folder with a slash through it, for workspace files that are never shared. */
function FolderOffIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h16" />
      <path d="M21.5 17.5A2 2 0 0 0 22 16V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9" />
      <path d="m2 2 20 20" />
    </svg>
  );
}

function SectionLabel({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
      {icon}
      {children}
    </p>
  );
}

function RoleOption({ label, hint }: { label: string; hint?: string }) {
  return (
    <div>
      <div>{label}</div>
      {hint && <div className="text-xs text-muted-foreground whitespace-normal">{hint}</div>}
    </div>
  );
}

// Radix asChild triggers need the button's ref to anchor and open the menu.
const RoleTrigger = forwardRef<HTMLButtonElement, ComponentProps<typeof Button>>(
  function RoleTrigger({ children, className, ...props }, ref) {
    return (
      <Button
        ref={ref}
        type="button"
        variant="ghost"
        size="sm"
        className={cn("shrink-0 gap-1 font-normal", className)}
        {...props}
      >
        {children}
        <ChevronDownIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
      </Button>
    );
  },
);

/**
 * Role picker for one grant. Offers ``grantable``; a current level outside it
 * (e.g. Edit after the server turned read-only) stays shown and checked but
 * can't be chosen again.
 */
function RoleMenu({
  value,
  grantable,
  ceiling,
  ceilingHint,
  onChange,
  onRemove,
  children,
}: {
  value: number;
  grantable: readonly number[];
  ceiling: number;
  ceilingHint: string;
  onChange?: (level: number) => void;
  onRemove?: () => void;
  children: ReactNode;
}) {
  const levels = onChange
    ? [...new Set([...grantable, value])].sort(
        (first, second) => roleRank(first) - roleRank(second),
      )
    : [];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {levels.length > 0 && (
          <DropdownMenuRadioGroup
            value={String(value)}
            onValueChange={(next) => onChange?.(Number(next))}
          >
            {levels.map((level) => (
              <DropdownMenuRadioItem
                key={level}
                value={String(level)}
                disabled={!grantable.includes(level)}
              >
                <RoleOption
                  label={roleLabel(level)}
                  hint={roleRank(level) > roleRank(ceiling) ? ceilingHint : roleHint(level)}
                />
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        )}
        {onRemove && (
          <>
            {levels.length > 0 && <DropdownMenuSeparator />}
            <DropdownMenuItem variant="destructive" onSelect={onRemove}>
              Remove access
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function PersonRow({
  permission,
  isYou,
  grantable,
  ceiling,
  ceilingHint,
  busy,
  onChangeLevel,
  onRevoke,
}: {
  permission: Permission;
  isYou: boolean;
  grantable: readonly number[];
  ceiling: number;
  ceilingHint: string;
  busy: boolean;
  onChangeLevel: (userId: string, level: number) => void;
  onRevoke: (userId: string) => void;
}) {
  const { level, user_id: userId } = permission;
  // Manage isn't grantable here, but a manager can still be removed.
  const canRelevel = roleRank(level) < roleRank(LEVEL_MANAGE);
  return (
    <li className="flex min-w-0 items-center gap-3 py-1.5">
      <Avatar>
        <AvatarFallback>{userId.slice(0, 1).toUpperCase()}</AvatarFallback>
      </Avatar>
      <p className="min-w-0 flex-1 truncate font-medium" title={userId}>
        {userId}
        {isYou && <span className="font-normal text-muted-foreground"> (you)</span>}
      </p>
      {level === LEVEL_OWNER || isYou ? (
        <span className="shrink-0 px-2.5 text-sm text-muted-foreground">{roleLabel(level)}</span>
      ) : (
        <RoleMenu
          value={level}
          grantable={grantable}
          ceiling={ceiling}
          ceilingHint={ceilingHint}
          onChange={canRelevel ? (next) => onChangeLevel(userId, next) : undefined}
          onRemove={() => onRevoke(userId)}
        >
          <RoleTrigger aria-label={`Permission level for ${userId}`} disabled={busy}>
            {roleLabel(level)}
          </RoleTrigger>
        </RoleMenu>
      )}
    </li>
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
        className="h-8 min-w-0"
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
        className="h-8 min-w-0"
        autoComplete="off"
        disabled={disabled}
      />
      {isOpen && (
        // Wider than the (narrow) field so suggested emails aren't truncated.
        <div className="absolute left-0 top-full z-50 mt-1 w-full sm:w-96 max-w-[calc(100vw-4rem)] rounded-[12px] border border-border bg-popover p-2 text-popover-foreground shadow-menu">
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
      onClick={handleCopy}
      className="gap-1.5"
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
