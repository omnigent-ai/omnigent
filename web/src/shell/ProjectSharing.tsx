// Project sharing: the owner adds or revokes users who may read and write
// everything in the project (board, tickets, files and every session).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UsersIcon, XIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getProject,
  listProjectShares,
  revokeProjectShare,
  shareProject,
} from "@/lib/projectsApi";

function sharesKey(projectId: string) {
  return ["project-shares", projectId] as const;
}

/** The settings dialog's Sharing section; members see the list but can't change it. */
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
  const isOwner = project.data !== undefined && project.data.shared !== true;
  const owner = shares.data?.owner_user_id ?? project.data?.owner_user_id ?? null;
  const members = shares.data?.data ?? [];

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const userId = draft.trim();
    if (userId) add.mutate(userId);
  };

  return (
    <section className="flex flex-col gap-2 border-t pt-4" data-testid="project-sharing">
      <div className="flex items-center gap-2">
        <UsersIcon className="size-4 text-muted-foreground" />
        <h3 className="font-medium text-ui">Sharing</h3>
      </div>
      <p className="text-muted-foreground text-sm">
        {isOwner
          ? "People you share with can open the board, tickets, files and every session here, and chat in them. Sessions you started still run on your agents and hosts."
          : `Shared with you by ${owner ?? "the owner"}. Only the owner can change who has access.`}
      </p>
      {isOwner && (
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
      {(add.isError || revoke.isError) && (
        <p className="text-destructive text-sm" role="alert">
          {((add.error ?? revoke.error) as Error).message}
        </p>
      )}
      <ul className="flex flex-col gap-1" data-testid="project-share-list">
        {owner && (
          <li className="flex items-center justify-between text-sm">
            <span className="truncate">{owner}</span>
            <span className="text-muted-foreground">Owner</span>
          </li>
        )}
        {members.map((share) => (
          <li
            key={share.user_id}
            className="flex items-center justify-between gap-2 text-sm"
            data-testid="project-share-row"
          >
            <span className="truncate">{share.user_id}</span>
            {isOwner ? (
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
            ) : (
              <span className="text-muted-foreground">Member</span>
            )}
          </li>
        ))}
        {shares.isSuccess && members.length === 0 && (
          <li className="text-muted-foreground text-sm">Not shared with anyone yet.</li>
        )}
      </ul>
    </section>
  );
}
