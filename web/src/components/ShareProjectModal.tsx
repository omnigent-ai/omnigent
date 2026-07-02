/**
 * Share modal for a first-class project.
 *
 * Sharing a project grants access to every chat filed under it (chats inherit
 * the project's ACL server-side), including chats added later. Two "everyone"
 * scopes are offered as toggles — share with all signed-in members
 * (`__members__`) and anyone with the link (`__public__`) — plus an invite form
 * and the current member list. All grants target the project by id.
 */

import { type FormEvent, useState } from "react";
import { Trash2Icon, UserPlusIcon } from "lucide-react";
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
import { useProjectMembers, useShareProject, useUnshareProject } from "@/hooks/useProjectShare";
import { MEMBERS_USER, PUBLIC_USER } from "@/lib/projectsApi";

interface ShareProjectModalProps {
  projectId: string;
  projectName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const LEVEL_LABELS: Record<number, string> = { 1: "Read", 2: "Edit", 3: "Manage", 4: "Owner" };

export function ShareProjectModal({
  projectId,
  projectName,
  open,
  onOpenChange,
}: ShareProjectModalProps) {
  const { data: members, isLoading } = useProjectMembers(open ? projectId : null);
  const share = useShareProject(projectId);
  const unshare = useUnshareProject(projectId);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);

  const busy = share.isPending || unshare.isPending;
  const rows = members ?? [];
  const isMembers = rows.some((m) => m.user_id === MEMBERS_USER);
  const isPublic = rows.some((m) => m.user_id === PUBLIC_USER);
  // Real, removable grantees — the sentinels are the toggles above, and the
  // owner can't be revoked, so neither belongs in the member list.
  const memberRows = rows.filter(
    (m) => m.user_id !== MEMBERS_USER && m.user_id !== PUBLIC_USER && m.level < 4,
  );

  function toggleScope(sentinel: string, on: boolean) {
    setError(null);
    if (on) {
      share.mutate({ userId: sentinel, level: 1 }, { onError: (e) => setError(e.message) });
    } else {
      unshare.mutate(sentinel, { onError: (e) => setError(e.message) });
    }
  }

  function handleInvite(e: FormEvent) {
    e.preventDefault();
    const trimmed = newUserId.trim();
    if (!trimmed) return;
    setError(null);
    share.mutate(
      { userId: trimmed, level: parseInt(newLevel, 10) },
      {
        onSuccess: () => {
          setNewUserId("");
          setNewLevel("1");
        },
        onError: (e) => setError(e.message),
      },
    );
  }

  function handleRevoke(userId: string) {
    setError(null);
    unshare.mutate(userId, { onError: (e) => setError(e.message) });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Share project</DialogTitle>
          <DialogDescription>
            Sharing applies to every chat in <span className="font-medium">{projectName}</span> —
            including chats added later.
          </DialogDescription>
        </DialogHeader>

        {/* Members scope */}
        <div className="flex items-center justify-between rounded-lg border px-3 py-2">
          <div>
            <p className="text-sm font-medium">Share with all members</p>
            <p className="text-xs text-muted-foreground">
              Everyone signed in to this Omnigent sees this project
            </p>
          </div>
          <Switch
            checked={isMembers}
            onCheckedChange={(c) => toggleScope(MEMBERS_USER, c)}
            disabled={busy || isLoading}
            data-testid="project-members-toggle"
          />
        </div>

        {/* Public link scope */}
        <div className="flex items-center justify-between rounded-lg border px-3 py-2">
          <div>
            <p className="text-sm font-medium">Anyone with the link</p>
            <p className="text-xs text-muted-foreground">
              Anyone with a chat's link can view it, even without an account
            </p>
          </div>
          <Switch
            checked={isPublic}
            onCheckedChange={(c) => toggleScope(PUBLIC_USER, c)}
            disabled={busy || isLoading}
            data-testid="project-public-toggle"
          />
        </div>

        {/* Current members */}
        {memberRows.length > 0 && (
          <div className="max-h-40 overflow-y-auto">
            <p className="px-1 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              People with access
            </p>
            {memberRows.map((m) => (
              <div
                key={m.user_id}
                className="flex items-center gap-2 px-1 py-1 text-sm"
                data-testid="project-member-row"
              >
                <span className="flex-1 truncate">{m.user_id}</span>
                <span className="text-xs text-muted-foreground">
                  {LEVEL_LABELS[m.level] ?? m.level}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove ${m.user_id}`}
                  disabled={busy}
                  onClick={() => handleRevoke(m.user_id)}
                >
                  <Trash2Icon className="size-3.5" />
                </Button>
              </div>
            ))}
          </div>
        )}

        {/* Invite a specific user */}
        <form onSubmit={handleInvite} className="flex items-end gap-2">
          <div className="flex-1">
            <label
              htmlFor="project-perm-user"
              className="text-xs font-medium text-muted-foreground"
            >
              Invite by user ID
            </label>
            <Input
              id="project-perm-user"
              value={newUserId}
              onChange={(e) => setNewUserId(e.target.value)}
              placeholder="alice@example.com"
              className="mt-1"
            />
          </div>
          <div>
            <label
              htmlFor="project-perm-level"
              className="text-xs font-medium text-muted-foreground"
            >
              Level
            </label>
            <Select value={newLevel} onValueChange={setNewLevel}>
              <SelectTrigger className="mt-1 w-24" id="project-perm-level">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">Read</SelectItem>
                <SelectItem value="2">Edit</SelectItem>
                <SelectItem value="3">Manage</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" size="sm" disabled={!newUserId.trim() || busy}>
            <UserPlusIcon className="mr-1 size-3.5" />
            Grant
          </Button>
        </form>

        {error && (
          <p className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
