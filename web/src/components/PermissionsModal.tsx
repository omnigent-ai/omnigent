/**
 * Permissions management modal for a session.
 *
 * Displays current grants, allows granting/revoking access, and
 * toggling public visibility. Only accessible to users with
 * manage-level (3) permission on the session.
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
import { CheckIcon, LinkIcon, LockIcon, QrCodeIcon, Trash2Icon, UsersIcon } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { type Permission, workspaceSharingBlocked } from "@/lib/permissionsApi";
import { useRebasePath } from "@/lib/routing";
import { cn } from "@/lib/utils";

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
  // notice below, so the request would be wasted.
  const { data: permissions, isLoading } = usePermissions(open && !sharingOff ? sessionId : null);
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);
  const [showQr, setShowQr] = useState(false);
  const controlsId = useId();
  const blockedNoticeId = useId();
  const currentUserId = getCurrentUserId();

  // Whether the owner shared the workspace files with view-level collaborators.
  const { session } = useSession(open && !sharingOff ? sessionId : null);
  const queryClient = useQueryClient();
  const shareWorkspace = useMutation({
    mutationFn: (next: boolean) => updateSession(sessionId, { shareWorkspaceFiles: next }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
    onError: (err: Error) => setError(err.message),
  });

  const people = (permissions ?? []).filter((permission) => permission.user_id !== PUBLIC_USER);
  const publicGrant = permissions?.find((permission) => permission.user_id === PUBLIC_USER);
  const publicLevel = publicGrant ? Math.min(publicGrant.level, publicMaxLevel) : 0;
  const busy = isLoading || grant.isPending || revoke.isPending;
  // The current user and owners lead the list, then everyone, then the rest.
  const leadingPeople = people
    .filter((permission) => permission.user_id === currentUserId || permission.level === 4)
    .sort(
      (first, second) =>
        Number(second.user_id === currentUserId) - Number(first.user_id === currentUserId),
    );
  const accessEntries = [
    ...leadingPeople,
    ...(publicGrant ? [publicGrant] : []),
    ...people.filter((permission) => !leadingPeople.includes(permission)),
  ];

  function handleGrant(event: FormEvent) {
    event.preventDefault();
    const userId = newUserId.trim();
    if (!userId || workspaceBlocked) return;
    setError(null);
    grant.mutate(
      { userId, level: Number(newLevel) },
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
    revoke.mutate(userId, { onError: (err) => setError(err.message) });
  }

  function handleChangeLevel(userId: string, level: number) {
    if (workspaceBlocked) return;
    setError(null);
    grant.mutate({ userId, level }, { onError: (err) => setError(err.message) });
  }

  // Level 0 revokes public access; 1 or 2 grants it at that level.
  function handlePublicLevel(level: number) {
    if (level > 0 && (workspaceBlocked || !publicSharingEnabled)) return;
    setError(null);
    if (level > 0) {
      grant.mutate({ userId: PUBLIC_USER, level }, { onError: (err) => setError(err.message) });
    } else {
      revoke.mutate(PUBLIC_USER, { onError: (err) => setError(err.message) });
    }
  }

  // "off" short-circuit: sharing is disabled server-wide, so skip the whole
  // grant UI and show a plain notice instead. Mirrors the server's 403.
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] gap-5 overflow-y-auto sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>Share this session</DialogTitle>
          <DialogDescription className="sr-only">
            Manage who can open this session and its workspace files.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 border-b border-border pb-5">
          <p className="text-xs leading-relaxed text-muted-foreground">
            {sharingReadOnly
              ? "This Omnigent server allows read-only sharing. Invite others to read this session."
              : "This Omnigent server allows Read and Edit access for added people."}
            {publicSharingEnabled
              ? ` Anyone signed in is limited to ${sharingReadOnly || publicMaxLevel === 1 ? "Read" : "Edit"} access.`
              : " Access is limited to added people on this server."}
          </p>
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
            <label className="text-sm font-medium" htmlFor={`${controlsId}-access`}>
              Who can access
            </label>
            <Select
              value={publicGrant ? "signed-in" : "restricted"}
              onValueChange={(value) => handlePublicLevel(value === "signed-in" ? 1 : 0)}
              disabled={busy || (workspaceBlocked && !publicGrant)}
              componentId="diagnostics.permissions.public_level"
              valueHasNoPii
            >
              <SelectTrigger
                id={`${controlsId}-access`}
                className="w-full min-w-0 sm:w-48 sm:justify-self-end"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="end">
                <SelectItem value="restricted">Only added people</SelectItem>
                {(publicSharingEnabled || publicGrant) && (
                  <SelectItem
                    value="signed-in"
                    disabled={workspaceBlocked || !publicSharingEnabled}
                  >
                    Anyone signed in
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </div>
          {publicGrant && (
            <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
              <label className="text-sm font-medium" htmlFor={`${controlsId}-role`}>
                What can they do?
              </label>
              <Select
                value={String(publicLevel)}
                onValueChange={(value) => handlePublicLevel(Number(value))}
                disabled={busy}
                componentId="diagnostics.permissions.public_role"
                valueHasNoPii
              >
                <SelectTrigger
                  id={`${controlsId}-role`}
                  className="w-full min-w-0 sm:w-32 sm:justify-self-end"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="end">
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
          {workspaceBlocked && (
            <p id={blockedNoticeId} className="text-sm text-destructive">
              This session&apos;s working directory (a home or root directory) cannot be shared on
              this Omnigent server.
            </p>
          )}
        </div>

        <div className="min-w-0 space-y-3" data-testid="share-grants">
          <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <UsersIcon className="size-3.5" aria-hidden="true" />
            People with access
          </p>
          <form
            onSubmit={handleGrant}
            className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-2"
          >
            <label htmlFor="perm-user" className="sr-only">
              User ID
            </label>
            <AddUserField value={newUserId} onChange={setNewUserId} disabled={workspaceBlocked} />
            <Select
              value={newLevel}
              onValueChange={setNewLevel}
              disabled={workspaceBlocked}
              componentId="diagnostics.permissions.grant_level"
              valueHasNoPii
            >
              <SelectTrigger className="h-8 w-24" aria-label="New user permission">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="end">
                <SelectItem value="1">Read</SelectItem>
                {/* Read-only sharing caps new grants at view; hide Edit. */}
                {!sharingReadOnly && <SelectItem value="2">Edit</SelectItem>}
              </SelectContent>
            </Select>
            <Button
              type="submit"
              loading={grant.isPending}
              disabled={workspaceBlocked || !newUserId.trim()}
              aria-describedby={workspaceBlocked ? blockedNoticeId : undefined}
              componentId="diagnostics.permissions.grant"
            >
              Add
            </Button>
          </form>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : accessEntries.length === 0 ? (
            <p className="text-sm text-muted-foreground">No grants yet.</p>
          ) : (
            <ul className="max-h-52 space-y-1 overflow-y-auto" aria-label="People with access">
              {accessEntries.map((permission) => (
                <AccessRow
                  key={permission.user_id}
                  permission={permission}
                  publicLevel={publicLevel}
                  readOnly={sharingReadOnly}
                  busy={busy}
                  onChangeLevel={handleChangeLevel}
                  onRevoke={handleRevoke}
                />
              ))}
            </ul>
          )}
        </div>

        {!!session?.workspace && (
          <div className="space-y-3 border-t border-border pt-5">
            <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              <LockIcon className="size-3.5" aria-hidden="true" />
              What they can access
            </p>
            <div
              className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-3"
              data-testid="share-workspace-files"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <label htmlFor={`${controlsId}-files`} className="block cursor-pointer text-sm">
                  Viewers can browse workspace files
                </label>
                {session.shareWorkspaceFiles && (
                  <p
                    id={`${controlsId}-files-description`}
                    className="text-[0.85em] leading-snug text-muted-foreground"
                  >
                    People with Read access can browse files. Edit access and higher also allow file
                    changes. Existing workspace access still applies.
                  </p>
                )}
              </div>
              <Switch
                id={`${controlsId}-files`}
                aria-describedby={
                  session.shareWorkspaceFiles ? `${controlsId}-files-description` : undefined
                }
                checked={session.shareWorkspaceFiles ?? false}
                onCheckedChange={(checked) => {
                  setError(null);
                  shareWorkspace.mutate(checked);
                }}
                disabled={shareWorkspace.isPending}
                componentId="diagnostics.permissions.share_workspace_toggle"
              />
            </div>
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter className="sm:justify-between">
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

function AccessRow({
  permission,
  publicLevel,
  readOnly,
  busy,
  onChangeLevel,
  onRevoke,
}: {
  permission: Permission;
  publicLevel: number;
  readOnly: boolean;
  busy: boolean;
  onChangeLevel: (userId: string, level: number) => void;
  onRevoke: (userId: string) => void;
}) {
  const everyone = permission.user_id === PUBLIC_USER;
  const label = everyone ? "Everyone" : permission.user_id;
  const isOwner = permission.level === 4;
  // Owner and Manage are never grantable here, and read-only sharing can't
  // re-level grants, so those rows show a fixed label. Everyone's level is
  // set by the "What can they do?" control above.
  const fixedLevel = everyone || isOwner || permission.level === 3 || readOnly;

  return (
    <li className="flex min-w-0 items-center gap-3 py-2">
      <Avatar>
        <AvatarFallback>
          {everyone ? (
            <UsersIcon className="size-4" aria-hidden="true" />
          ) : (
            label.slice(0, 1).toUpperCase()
          )}
        </AvatarFallback>
      </Avatar>
      <p className="min-w-0 flex-1 truncate font-medium" title={label}>
        {label}
      </p>
      {fixedLevel ? (
        <span className="shrink-0 text-sm text-muted-foreground">
          {LEVEL_LABELS[everyone ? publicLevel : permission.level] ?? "Read"}
        </span>
      ) : (
        <Select
          value={String(permission.level)}
          onValueChange={(value) => onChangeLevel(permission.user_id, Number(value))}
          disabled={busy}
        >
          <SelectTrigger
            className="h-8 w-24 shrink-0"
            aria-label={`Permission level for ${permission.user_id}`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="end">
            <SelectItem value="1">Read</SelectItem>
            <SelectItem value="2">Edit</SelectItem>
          </SelectContent>
        </Select>
      )}
      {!everyone && !isOwner && (
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={busy}
          className="shrink-0 text-muted-foreground hover:text-destructive"
          aria-label={`Remove access for ${permission.user_id}`}
          title="Remove access"
          onClick={() => onRevoke(permission.user_id)}
          componentId="diagnostics.permissions.revoke"
        >
          <Trash2Icon className="size-3.5" aria-hidden="true" />
        </Button>
      )}
    </li>
  );
}
