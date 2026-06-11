/**
 * Permissions management modal for a session.
 *
 * Displays current grants, allows granting/revoking access, and
 * toggling public visibility. Only accessible to users with
 * manage-level (3) permission on the session.
 */

import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { CheckIcon, LinkIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
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
import { getOmnigentUserSearch } from "@/lib/host";
import { cn } from "@/lib/utils";

const PUBLIC_USER = "__public__";

interface PermissionsModalProps {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PermissionsModal({ sessionId, open, onOpenChange }: PermissionsModalProps) {
  const { data: permissions, isLoading } = usePermissions(open ? sessionId : null);
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);

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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">Share this session</DialogTitle>
          <DialogDescription>
            Invite others to view or collaborate on this session.
          </DialogDescription>
        </DialogHeader>

        {/* Public toggle */}
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

        {/* Current grants */}
        <div>
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
                <SelectItem value="2">Edit</SelectItem>
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
          <CopyLinkButton sessionId={sessionId} />
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
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

function CopyLinkButton({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);

  const handleCopy = useCallback(() => {
    const url = `${window.location.origin}/c/${sessionId}`;
    navigator.clipboard.writeText(url).then(
      () => setCopied(true),
      (err) => {
        console.warn("Failed to copy link to clipboard", err);
      },
    );
  }, [sessionId]);

  return (
    <Button variant="ghost" size="sm" onClick={handleCopy} className="gap-1.5 text-primary">
      {copied ? <CheckIcon className="size-3.5" /> : <LinkIcon className="size-3.5" />}
      {copied ? "Copied!" : "Copy link"}
    </Button>
  );
}

function GrantRow({
  permission,
  onRevoke,
  onChangeLevel,
  busy,
}: {
  permission: Permission;
  onRevoke: (userId: string) => void;
  onChangeLevel: (userId: string, level: number) => void;
  busy: boolean;
}) {
  const isOwner = permission.level === 4;
  // Manage is not grantable from the UI, so a pre-existing manage grant
  // renders as a fixed label rather than a dropdown choice. Unlike the
  // owner row it can still be revoked.
  const isManage = permission.level === 3;

  return (
    <div className="flex items-center gap-2 rounded-md px-2 py-0.5 hover:bg-muted/50">
      <span className="flex-1 truncate text-sm" title={permission.user_id}>
        {permission.user_id}
      </span>
      {isOwner || isManage ? (
        <span className="flex h-8 w-28 items-center px-3 text-sm text-muted-foreground">
          {isOwner ? "Owner" : "Manage"}
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
