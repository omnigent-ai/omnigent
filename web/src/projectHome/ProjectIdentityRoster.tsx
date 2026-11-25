import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UsersIcon } from "lucide-react";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { RunningDot } from "@/components/RunningDot";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { openProjectIdentitySession, type ProjectIdentity } from "@/lib/projectsApi";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { personaPanelPath } from "./projectHomeNavigation";
import { ProjectTeamDialog } from "./ProjectTeamDialog";
import { projectIdentitiesKey, useProjectIdentities } from "./useProjectIdentities";

/** Personas shown in the header; the rest open from a "+N" menu so the bar never overflows. */
export const VISIBLE_ROSTER_LIMIT = 4;

/**
 * The project's owner-defined identities beside the Lead button. Each
 * opens its one active session (single-tracked) or starts it when it has none.
 */
export function ProjectIdentityRoster({
  projectId,
  projectName,
  leadPath,
}: {
  projectId: string | null;
  projectName: string;
  /** On phones the Lead joins the Team menu; omit when the project has no Lead. */
  leadPath?: string;
}) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const identities = useProjectIdentities(projectId);
  const open = useMutation({
    mutationFn: (identity: ProjectIdentity) =>
      openProjectIdentitySession(projectId as string, identity.id, identity.active_session_id),
    onSuccess: ({ session_id }) => {
      void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) });
      // Open beside the board, like the Lead, rather than leaving the project.
      navigate(personaPanelPath(projectName, session_id, search));
    },
    onError: () => void client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) }),
  });
  if (projectId === null) return null;
  const roles = (identities.data ?? []).filter((identity) => !identity.builtin);
  const leadName = identities.data?.find((identity) => identity.builtin)?.name;
  // Keep working personas in view; otherwise the team's own order.
  const ordered = [
    ...roles.filter((identity) => identity.active_session_state === "busy"),
    ...roles.filter((identity) => identity.active_session_state !== "busy"),
  ];
  if (isMobile) {
    return (
      <>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Team"
              data-testid="project-team-menu"
              loading={open.isPending}
            >
              <UsersIcon />
              {ordered.some((identity) => identity.active_session_state === "busy") && (
                <RunningDot />
              )}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {leadPath !== undefined && (
              <DropdownMenuItem className="gap-2" onSelect={() => navigate(leadPath)}>
                <PersonaAvatar name={leadName ?? "Lead"} size="xs" />
                {leadName ?? "Lead"}
              </DropdownMenuItem>
            )}
            {ordered.map((identity) => (
              <DropdownMenuItem
                key={identity.id}
                className="gap-2"
                onSelect={() => open.mutate(identity)}
              >
                <PersonaAvatar name={identity.name} size="xs" />
                <span className="max-w-48 truncate">{identity.name}</span>
                {identity.active_session_state === "busy" && <RunningDot />}
              </DropdownMenuItem>
            ))}
            {(leadPath !== undefined || ordered.length > 0) && <DropdownMenuSeparator />}
            <DropdownMenuItem className="gap-2" onSelect={() => setEditing(true)}>
              <UsersIcon className="size-4" />
              Edit team
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {editing && (
          <ProjectTeamDialog
            projectId={projectId}
            projectName={projectName}
            onClose={() => setEditing(false)}
          />
        )}
      </>
    );
  }
  const limit = VISIBLE_ROSTER_LIMIT;
  const visible = ordered.slice(0, limit);
  const hidden = ordered.slice(limit);
  return (
    <div
      className="flex items-center gap-1"
      role="group"
      aria-label="Project identities"
      data-testid="project-identity-roster"
    >
      {visible.map((identity) => {
        const busy = identity.active_session_state === "busy";
        const pending = open.isPending && open.variables?.id === identity.id;
        return (
          <Tooltip key={identity.id}>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size={isMobile ? "icon" : "sm"}
                // Names show only in a wide header; the avatar and tooltip identify each persona.
                className="@max-[104rem]/topbar:size-8 @max-[104rem]/topbar:px-0"
                aria-label={`Talk to ${identity.name}${busy ? " (working)" : ""}`}
                data-testid="project-identity-button"
                loading={pending}
                onClick={() => open.mutate(identity)}
              >
                <PersonaAvatar name={identity.name} size="xs" />
                {!isMobile && (
                  <span className="max-w-28 truncate @max-[104rem]/topbar:hidden">
                    {identity.name}
                  </span>
                )}
                {busy && <RunningDot />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <span className="font-medium">{identity.name}</span>
              {": "}
              {busy
                ? `${identity.name} is working; open its session`
                : identity.active_session_id
                  ? `Open ${identity.name}'s session`
                  : `Start a session with ${identity.name}`}
              {identity.router && ". It receives ticket events and escalations."}
            </TooltipContent>
          </Tooltip>
        );
      })}
      {hidden.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`${hidden.length} more personas`}
              data-testid="project-identity-more"
              className="text-xs text-muted-foreground tabular-nums"
            >
              +{hidden.length}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {hidden.map((identity) => (
              <DropdownMenuItem
                key={identity.id}
                className="gap-2"
                onSelect={() => open.mutate(identity)}
              >
                <PersonaAvatar name={identity.name} size="xs" />
                <span className="max-w-48 truncate">{identity.name}</span>
                {identity.active_session_state === "busy" && <RunningDot />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Edit team"
            data-testid="project-edit-team"
            className="text-muted-foreground"
            onClick={() => setEditing(true)}
          >
            <UsersIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {open.error ? `Could not open: ${open.error.message}` : "Team"}
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
