// Project sharing: the owner adds or revokes users who may read and write
// everything in the project (board, tickets, files and every session), and may
// make a member a Manager, who can also add and remove ordinary members.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, CopyIcon, LinkIcon, UsersIcon, XIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useProjects } from "@/hooks/useConversations";
import { useCopyLink } from "@/hooks/useCopyLink";
import {
  getBoardReadAudit,
  getProject,
  listProjectShares,
  type ProjectShareRole,
  revokeProjectShare,
  setProjectShareRole,
  shareProject,
  updateProjectConfig,
} from "@/lib/projectsApi";
import { useRebasePath } from "@/lib/routing";
import { getShareableProjectLink } from "@/lib/shareLink";

function sharesKey(projectId: string) {
  return ["project-shares", projectId] as const;
}

/** Copies the project's absolute URL; shows "Copied" for 2 seconds, or the URL when copying fails. */
function CopyProjectLinkButton({ projectName }: { projectName: string }) {
  const rebasePath = useRebasePath();
  const { status, copy } = useCopyLink();
  const url = getShareableProjectLink(projectName, rebasePath);

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="ml-auto gap-1.5 text-primary"
        data-testid="project-share-copy-link"
        onClick={() => void copy(url)}
      >
        {status === "copied" ? (
          <CheckIcon className="size-3.5" />
        ) : (
          <LinkIcon className="size-3.5" />
        )}
        {status === "copied" ? "Copied" : "Copy URL"}
      </Button>
      {status === "failed" && (
        <p className="w-full text-muted-foreground text-sm" role="status">
          Couldn't copy automatically. Copy this link:{" "}
          <span className="select-all break-all text-foreground">{url}</span>
        </p>
      )}
    </>
  );
}

const ROLE_LABELS: Record<ProjectShareRole, string> = { member: "Member", manager: "Manager" };

/**
 * The settings dialog's Sharing section. The owner adds, removes and sets roles;
 * a Manager adds and removes ordinary members; members see the list read-only.
 */
export function ProjectSharingSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
    staleTime: 30_000,
  });
  const shares = useQuery({
    queryKey: sharesKey(projectId),
    queryFn: () => listProjectShares(projectId),
  });
  const [draft, setDraft] = useState("");
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: sharesKey(projectId) });
    void queryClient.invalidateQueries({ queryKey: ["projects"] });
  };
  const add = useMutation({
    mutationFn: (userId: string) => shareProject(projectId, userId),
    onSuccess: () => {
      setDraft("");
      refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (userId: string) => revokeProjectShare(projectId, userId),
    onSuccess: refresh,
  });
  const setRole = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: ProjectShareRole }) =>
      setProjectShareRole(projectId, userId, role),
    onSuccess: refresh,
  });
  const isOwner = project.data !== undefined && project.data.shared !== true;
  const isManager = !isOwner && shares.data?.viewer_role === "manager";
  const canInvite = isOwner || isManager;
  const owner = shares.data?.owner_user_id ?? project.data?.owner_user_id ?? null;
  const members = shares.data?.data ?? [];

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const userId = draft.trim();
    if (userId) add.mutate(userId);
  };

  return (
    <section className="flex flex-col gap-2 border-t pt-4" data-testid="project-sharing">
      <div className="flex flex-wrap items-center gap-2">
        <UsersIcon className="size-4 text-muted-foreground" />
        <h3 className="font-medium text-ui">Sharing</h3>
        {project.data && <CopyProjectLinkButton projectName={project.data.name} />}
      </div>
      <p className="text-muted-foreground text-sm">
        {isOwner
          ? "People you share with can open the board, tickets, files and every session here, and chat in them. Sessions you started still run on your agents and hosts. Managers can also add and remove members."
          : isManager
            ? `Shared with you by ${owner ?? "the owner"}. As a Manager you can add and remove members; only the owner can change roles or remove a Manager.`
            : `Shared with you by ${owner ?? "the owner"}. Only the owner or a Manager can change who has access.`}
      </p>
      {canInvite && (
        <form onSubmit={onSubmit} className="flex gap-2">
          <Input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="User ID, e.g. name@example.com"
            aria-label="User to share with"
            data-testid="project-share-input"
            className="flex-1"
          />
          <Button
            type="submit"
            size="sm"
            data-testid="project-share-add"
            loading={add.isPending}
            disabled={draft.trim() === ""}
          >
            Share
          </Button>
        </form>
      )}
      {(add.isError || revoke.isError || setRole.isError) && (
        <p className="text-destructive text-sm" role="alert">
          {((add.error ?? revoke.error ?? setRole.error) as Error).message}
        </p>
      )}
      <ul className="flex flex-col gap-1" data-testid="project-share-list">
        {owner && (
          <li className="flex items-center justify-between text-sm">
            <span className="truncate">{owner}</span>
            <span className="text-muted-foreground">Owner</span>
          </li>
        )}
        {members.map((share) => {
          const role: ProjectShareRole = share.role ?? "member";
          const canRemove = isOwner || (isManager && role === "member");
          return (
            <li
              key={share.user_id}
              className="flex items-center justify-between gap-2 text-sm"
              data-testid="project-share-row"
            >
              <span className="truncate">{share.user_id}</span>
              <span className="flex shrink-0 items-center gap-1">
                {isOwner ? (
                  <Select
                    value={role}
                    onValueChange={(next) =>
                      setRole.mutate({ userId: share.user_id, role: next as ProjectShareRole })
                    }
                    disabled={setRole.isPending}
                  >
                    <SelectTrigger
                      size="sm"
                      className="w-28"
                      aria-label={`Role for ${share.user_id}`}
                      data-testid="project-share-role"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="member">{ROLE_LABELS.member}</SelectItem>
                      <SelectItem value="manager">{ROLE_LABELS.manager}</SelectItem>
                    </SelectContent>
                  </Select>
                ) : (
                  <span className="text-muted-foreground" data-testid="project-share-role-label">
                    {ROLE_LABELS[role]}
                  </span>
                )}
                {canRemove && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Stop sharing with ${share.user_id}`}
                    data-testid="project-share-revoke"
                    onClick={() => revoke.mutate(share.user_id)}
                    disabled={revoke.isPending}
                  >
                    <XIcon className="size-3.5" />
                  </Button>
                )}
              </span>
            </li>
          );
        })}
        {shares.isSuccess && members.length === 0 && (
          <li className="text-muted-foreground text-sm">Not shared with anyone yet.</li>
        )}
      </ul>
    </section>
  );
}

/**
 * The owner's product approvers: people besides the owner whose own `product-ok`
 * lets a feature someone else requested land. Members see the list read-only.
 */
export function ProjectProductApproversSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
    staleTime: 30_000,
  });
  const [draft, setDraft] = useState("");
  const approvers = project.data?.config?.product_approvers ?? [];
  const save = useMutation({
    // Re-read so the whole-config write keeps every other stored setting.
    mutationFn: async (next: string[]) => {
      const current = await getProject(projectId);
      return updateProjectConfig(projectId, { ...current.config, product_approvers: next });
    },
    onSuccess: () => {
      setDraft("");
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project-config", projectId] });
    },
  });
  const isOwner = project.data !== undefined && project.data.shared !== true;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const userId = draft.trim();
    if (userId && !approvers.includes(userId)) save.mutate([...approvers, userId]);
  };
  return (
    <section className="flex flex-col gap-2 border-t pt-4" data-testid="project-product-approvers">
      <h3 className="font-medium text-ui">Product approvers</h3>
      <p className="text-muted-foreground text-sm">
        A feature someone other than the owner asked for lands only after a product approver adds{" "}
        <code className="text-xs">product-ok</code> on its ticket. The owner always counts.
      </p>
      {isOwner && (
        <form onSubmit={onSubmit} className="flex gap-2">
          <Input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="User ID, e.g. name@example.com"
            aria-label="Product approver to add"
            data-testid="product-approver-input"
            className="flex-1"
          />
          <Button
            type="submit"
            size="sm"
            data-testid="product-approver-add"
            loading={save.isPending}
            disabled={draft.trim() === ""}
          >
            Add
          </Button>
        </form>
      )}
      {save.isError && (
        <p className="text-destructive text-sm" role="alert">
          {(save.error as Error).message}
        </p>
      )}
      <ul className="flex flex-col gap-1" data-testid="product-approver-list">
        {approvers.map((userId) => (
          <li key={userId} className="flex items-center justify-between gap-2 text-sm">
            <span className="truncate">{userId}</span>
            {isOwner && (
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove ${userId} as a product approver`}
                onClick={() => save.mutate(approvers.filter((entry) => entry !== userId))}
                disabled={save.isPending}
              >
                <XIcon className="size-3.5" />
              </Button>
            )}
          </li>
        ))}
        {project.isSuccess && approvers.length === 0 && (
          <li className="text-muted-foreground text-sm">Only the owner so far.</li>
        )}
      </ul>
    </section>
  );
}

const PROJECT_ID = /^[0-9a-f]{32}$/;

function readSummary(reads: number): string {
  if (reads === 0) return "Its Lead hasn't read this board in the last day";
  return `Its Lead read this board ${reads} ${reads === 1 ? "time" : "times"} in the last day`;
}

/**
 * The owner's board readers: other projects whose Lead may read this board's tickets,
 * comments and stages. Off until the owner adds one; members see the list read-only.
 */
export function ProjectBoardReadersSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
    staleTime: 30_000,
  });
  const projects = useProjects();
  const { status: copyStatus, copy } = useCopyLink();
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const audit = useQuery({
    queryKey: ["board-read-audit", projectId],
    queryFn: () => getBoardReadAudit(projectId),
    staleTime: 60_000,
  });
  const readers = project.data?.config?.board_readers ?? [];
  const nameOf = (id: string) =>
    projects.data?.find((entry) => entry.id === id)?.name ??
    audit.data?.data.find((entry) => entry.project_id === id)?.project_name ??
    null;
  const readsBy = (id: string) =>
    audit.data?.data.find((entry) => entry.project_id === id)?.reads ?? 0;
  // Projects removed from the list whose Lead still read the board in the last day.
  const formerReaders = (audit.data?.data ?? []).filter(
    (entry) => !readers.includes(entry.project_id),
  );
  const save = useMutation({
    // Re-read so the whole-config write keeps every other stored setting.
    mutationFn: async (next: string[]) => {
      const current = await getProject(projectId);
      return updateProjectConfig(projectId, { ...current.config, board_readers: next });
    },
    onSuccess: () => {
      setDraft("");
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project-config", projectId] });
    },
  });
  const isOwner = project.data !== undefined && project.data.shared !== true;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const entry = draft.trim();
    if (!entry) return;
    const id = PROJECT_ID.test(entry)
      ? entry
      : (projects.data?.find((item) => item.name === entry)?.id ?? null);
    if (id === null) {
      setDraftError("No project you can see has that name. Paste its project ID instead.");
      return;
    }
    if (id === projectId) {
      setDraftError("This project can already read its own board.");
      return;
    }
    setDraftError(null);
    if (!readers.includes(id)) save.mutate([...readers, id]);
    else setDraft("");
  };
  return (
    <section className="flex flex-col gap-2 border-t pt-4" data-testid="project-board-readers">
      <h3 className="font-medium text-ui">Other projects' Leads that may read this board</h3>
      <p className="text-muted-foreground text-sm">
        A listed project's Lead may read this board's tickets, comments and stages, never its chats,
        files or mandate. Anyone with access to a listed project may see what its Lead reads here.
        Off until you add one.
      </p>
      <p className="flex items-center gap-1 text-muted-foreground text-sm">
        This project's ID: <code className="select-all font-mono text-xs">{projectId}</code>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Copy this project's ID"
          onClick={() => void copy(projectId)}
        >
          {copyStatus === "copied" ? (
            <CheckIcon className="size-3.5" />
          ) : (
            <CopyIcon className="size-3.5" />
          )}
        </Button>
      </p>
      {isOwner && (
        <form onSubmit={onSubmit} className="flex gap-2">
          <Input
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setDraftError(null);
            }}
            placeholder="Project name or ID"
            aria-label="Project whose Lead may read this board"
            data-testid="board-reader-input"
            list="board-reader-projects"
            className="flex-1"
          />
          <datalist id="board-reader-projects">
            {(projects.data ?? [])
              .filter((entry) => entry.id !== null && entry.id !== projectId)
              .map((entry) => (
                <option key={entry.id} value={entry.name} />
              ))}
          </datalist>
          <Button
            type="submit"
            size="sm"
            data-testid="board-reader-add"
            loading={save.isPending}
            disabled={draft.trim() === ""}
          >
            Add
          </Button>
        </form>
      )}
      {(draftError ?? (save.isError ? (save.error as Error).message : null)) && (
        <p className="text-destructive text-sm" role="alert">
          {draftError ?? (save.error as Error).message}
        </p>
      )}
      <ul className="flex flex-col gap-1" data-testid="board-reader-list">
        {readers.map((id) => (
          <li key={id} className="flex items-center justify-between gap-2 text-sm">
            <span className="flex min-w-0 flex-col">
              <span className="truncate">
                {nameOf(id) ?? (
                  <>
                    <code className="font-mono text-xs">{id}</code>
                    <span className="text-muted-foreground"> · a project you can't open</span>
                  </>
                )}
              </span>
              <span className="text-muted-foreground text-xs" data-testid="board-reader-reads">
                {readSummary(readsBy(id))}
              </span>
            </span>
            {isOwner && (
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Stop ${nameOf(id) ?? id} reading this board`}
                onClick={() => save.mutate(readers.filter((entry) => entry !== id))}
                disabled={save.isPending}
              >
                <XIcon className="size-3.5" />
              </Button>
            )}
          </li>
        ))}
        {project.isSuccess && readers.length === 0 && (
          <li className="text-muted-foreground text-sm">No other project's Lead can read it.</li>
        )}
      </ul>
      {formerReaders.length > 0 && (
        <p className="text-muted-foreground text-sm" data-testid="board-former-readers">
          No longer listed, read in the last day:{" "}
          {formerReaders
            .map(
              (entry) =>
                `${entry.project_name ?? entry.project_id} (${entry.reads} ${entry.reads === 1 ? "read" : "reads"})`,
            )
            .join(", ")}
        </p>
      )}
    </section>
  );
}
