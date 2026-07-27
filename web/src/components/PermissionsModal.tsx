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
import { CheckIcon, LinkIcon, QrCodeIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
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
  type Permission,
  useGrantPermission,
  usePermissions,
  useRevokePermission,
} from "@/hooks/usePermissions";
import { useUserSearch } from "@/hooks/useUserSearch";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentTransformShareLink, getOmnigentUserSearch } from "@/lib/host";
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
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PermissionsModal({ sessionId, open, onOpenChange }: PermissionsModalProps) {
  // Server sharing policy. While the boot probe is in flight we treat the
  // server as "on" (fail open) so the modal renders its full controls; the
  // server-side gate is the real enforcement point regardless.
  const info = useServerInfo();
  const sharingMode = info === "loading" ? "on" : info.sharing_mode;
  const sharingOff = sharingMode === "off";
  // Both read-capped tiers present the read-only UI. Under
  // "restricted_read_only" the server additionally blocks home/root-cwd
  // sessions entirely; that per-session rule is enforced server-side and
  // surfaces here as an error on the grant attempt.
  const sharingReadOnly = sharingMode === "read_only" || sharingMode === "restricted_read_only";
  // Public (anyone-with-the-link) access is a separate server switch from the
  // sharing tiers; when off, hide the toggle (the server rejects the grant too).
  const publicSharingEnabled = info === "loading" ? true : info.public_sharing_enabled;
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

  const userGrants = (permissions ?? []).filter((p) => p.user_id !== PUBLIC_USER);
  const publicGrant = (permissions ?? []).find((p) => p.user_id === PUBLIC_USER);
  const isPublic = !!publicGrant;

  function handleGrant(e: FormEvent) {
    e.preventDefault();
    const trimmed = newUserId.trim();
    if (!trimmed) return;
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
    setError(null);
    grant.mutate({ userId, level }, { onError: (err) => setError(err.message) });
  }

  function handlePublicToggle(checked: boolean) {
    setError(null);
    if (checked) {
      grant.mutate({ userId: PUBLIC_USER, level: 1 }, { onError: (err) => setError(err.message) });
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
          <DialogDescription>
            {sharingReadOnly
              ? "This server allows read-only sharing — invite others to view this session."
              : "Invite others to view or collaborate on this session."}
          </DialogDescription>
        </DialogHeader>

        {/* Public toggle — hidden when the server disables public access. */}
        {publicSharingEnabled && (
          <div className="flex items-center justify-between rounded-lg border px-3 py-2">
            <div>
              <p className="text-sm font-medium">Public access</p>
              <p className="text-xs text-muted-foreground">Anyone can view this session</p>
            </div>
            <Switch
              checked={isPublic}
              onCheckedChange={handlePublicToggle}
              disabled={grant.isPending || revoke.isPending}
            />
          </div>
        )}

        {/* Current grants. DialogContent is a grid, and grid items default to
            min-width:auto — without min-w-0 a long nowrap email sets the whole
            track's min-content and pushes every row past the dialog edge. */}
        <div className="min-w-0" data-testid="share-grants">
          {isLoading ? (
            <p className="text-sm text-muted-foreground py-2">Loading…</p>
          ) : userGrants.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">No grants yet.</p>
          ) : (
            <>
              {/* Column headers */}
              <div className="flex items-center gap-2 px-2 pb-0.5">
                <span className="flex-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Name
                </span>
                <span className="w-28 text-xs font-medium uppercase tracking-wide text-muted-foreground">
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

        {/* Add grant form */}
        <form onSubmit={handleGrant} className="flex items-end gap-2">
          <div className="flex-1">
            <label htmlFor="perm-user" className="text-xs font-medium text-muted-foreground">
              User ID
            </label>
            <AddUserField value={newUserId} onChange={setNewUserId} />
          </div>
          <div>
            <label htmlFor="perm-level" className="text-xs font-medium text-muted-foreground">
              Level
            </label>
            <Select value={newLevel} onValueChange={setNewLevel}>
              <SelectTrigger className="mt-1 w-24">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">Read</SelectItem>
                {/* Read-only sharing caps new grants at view; hide Edit. */}
                {!sharingReadOnly && <SelectItem value="2">Edit</SelectItem>}
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" size="sm" disabled={!newUserId.trim() || grant.isPending}>
            <UserPlusIcon className="mr-1 size-3.5" />
            Grant
          </Button>
        </form>

        {error && <p className="text-xs text-destructive">{error}</p>}

        <DialogFooter className="flex-row justify-between sm:justify-between">
          <div className="flex items-center gap-2">
            <CopyLinkButton sessionId={sessionId} />
            <Button
              variant="ghost"
              size="sm"
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

interface AddUserFieldProps {
  value: string;
  onChange: (value: string) => void;
}

/**
 * The permissions "add user" field. Stays a plain text input unless the host
 * injects a `searchUsers` provider (see `lib/host.ts`), in which case it becomes
 * a free-text suggestion combobox. Free typing is always allowed; suggestions
 * are an aid, not a constraint.
 */
function AddUserField({ value, onChange }: AddUserFieldProps) {
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
      />
    );
  }
  return <AddUserCombobox value={value} onChange={onChange} />;
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
function AddUserCombobox({ value, onChange }: AddUserFieldProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const { suggestions, isLoading } = useUserSearch(value);
  const hasQuery = value.trim().length > 0;
  const isOpen = open && hasQuery;

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
      />
      {isOpen && (
        // Wider than the (narrow) field so suggested emails aren't truncated.
        <div className="absolute left-0 top-full z-50 mt-1 w-96 rounded-lg border bg-popover p-1 text-popover-foreground shadow-md">
          {isLoading ? (
            <div className="py-6 text-center text-sm text-muted-foreground">Searching…</div>
          ) : suggestions.length === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">No matches</div>
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
                    "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm",
                    index === activeIndex && "bg-muted",
                  )}
                >
                  {/* Primary label fills the row and truncates. When the host
                      provides only an email (no display name), it shows alone;
                      the muted secondary email is only rendered when there's a
                      distinct display name to pair it with. */}
                  <span className="min-w-0 flex-1 truncate">{s.displayName ?? s.userId}</span>
                  {s.displayName && s.displayName !== s.userId && (
                    <span className="ml-2 shrink-0 truncate text-xs text-muted-foreground">
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
 * The basename-rebased session path turned into an absolute URL. In the embed
 * the host transform returns the full URL (origin included); standalone has no
 * transform, so we prepend the origin ourselves.
 */
function getShareableLink(sessionId: string, rebasePath: (path: string) => string): string {
  const path = rebasePath(`/c/${sessionId}`);
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${path}`;
}

/**
 * The `omnigent://<host>/c/<session_id>` deep link encoded into the share QR
 * code. The host (with port when non-default) is parsed from the same shareable
 * URL `getShareableLink` resolves — so standalone and embedded (host-transformed)
 * origins agree on the same server the desktop shell's deep-link handler keys
 * off of (see `electron/src/deepLink.js`). The path is always basename-less
 * `/c/<id>`; the workspace mount is server-determined and intentionally absent.
 */
function getDeepLink(sessionId: string, rebasePath: (path: string) => string): string {
  const url = getShareableLink(sessionId, rebasePath);
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
    const url = getShareableLink(sessionId, rebasePath);
    navigator.clipboard.writeText(url).then(
      () => setCopied(true),
      (err) => {
        console.warn("Failed to copy link to clipboard", err);
      },
    );
  }, [sessionId, rebasePath]);

  return (
    <Button variant="ghost" size="sm" onClick={handleCopy} className="gap-1.5 text-primary">
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
      <span className="flex-1 truncate text-sm" title={permission.user_id}>
        {permission.user_id}
      </span>
      {fixedLevel ? (
        <span className="flex h-8 w-28 items-center px-3 text-sm text-muted-foreground">
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
          <SelectContent>
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
        >
          <Trash2Icon className="size-3.5" />
          <span className="sr-only">Revoke</span>
        </Button>
      )}
    </div>
  );
}
