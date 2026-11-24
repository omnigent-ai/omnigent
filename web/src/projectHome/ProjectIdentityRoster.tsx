import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UserRoundIcon, UsersIcon } from "lucide-react";
import { RunningDot } from "@/components/RunningDot";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { openProjectIdentitySession, type ProjectIdentity } from "@/lib/projectsApi";
import { useNavigate } from "@/lib/routing";
import { ProjectTeamDialog } from "./ProjectTeamDialog";
import {
  projectIdentitiesKey,
  useProjectIdentities,
  useProjectIdentityProposals,
} from "./useProjectIdentities";

/**
 * The project's owner-defined identities beside the Orchestrator button. Each
 * opens its one active session (single-tracked) or starts it when it has none.
 */
export function ProjectIdentityRoster({
  projectId,
  projectName,
}: {
  projectId: string | null;
  projectName: string;
}) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const identities = useProjectIdentities(projectId);
  const proposed = useProjectIdentityProposals(projectId).data?.length ?? 0;
  const open = useMutation({
    mutationFn: (identity: ProjectIdentity) =>
      openProjectIdentitySession(projectId as string, identity.id, identity.active_session_id),
    onSuccess: ({ session_id }) => {
      void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) });
      navigate(`/c/${encodeURIComponent(session_id)}`);
    },
    onError: () => void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) }),
  });
  if (projectId === null) return null;
  const roles = (identities.data ?? []).filter((identity) => !identity.builtin);
  return (
    <div
      className="flex items-center gap-1"
      role="group"
      aria-label="Project identities"
      data-testid="project-identity-roster"
    >
      {roles.map((identity) => {
        const busy = identity.active_session_state === "busy";
        const pending = open.isPending && open.variables?.id === identity.id;
        return (
          <Tooltip key={identity.id}>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size={isMobile ? "icon" : "sm"}
                aria-label={`Talk to ${identity.name}${busy ? " (working)" : ""}`}
                data-testid="project-identity-button"
                loading={pending}
                onClick={() => open.mutate(identity)}
              >
                <UserRoundIcon />
                {!isMobile && <span className="max-w-28 truncate">{identity.name}</span>}
                {busy && <RunningDot />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {busy
                ? `${identity.name} is working; open its session`
                : identity.active_session_id
                  ? `Open ${identity.name}'s session`
                  : `Start a session with ${identity.name}`}
            </TooltipContent>
          </Tooltip>
        );
      })}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size={proposed > 0 ? "sm" : "icon-sm"}
            aria-label={proposed > 0 ? `Edit team, ${proposed} proposed` : "Edit team"}
            data-testid="project-edit-team"
            className={proposed > 0 ? "text-foreground" : "text-muted-foreground"}
            onClick={() => setEditing(true)}
          >
            <UsersIcon />
            {proposed > 0 && <span data-testid="project-team-proposed-count">{proposed}</span>}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {open.error
            ? `Could not open: ${open.error.message}`
            : proposed > 0
              ? "Review proposed identities"
              : "Team"}
        </TooltipContent>
      </Tooltip>
      {editing && (
        <ProjectTeamDialog
          projectId={projectId}
          projectName={projectName}
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  );
}
