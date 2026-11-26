import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UserPlusIcon, UsersIcon } from "lucide-react";
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
import { Link, useNavigate, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
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
  labelled = false,
}: {
  projectId: string | null;
  projectName: string;
  /** Phones: the Team menu's trigger names itself, beside the Lead's avatar. */
  labelled?: boolean;
  /** The Lead leads the avatar stack (on phones, the Team menu); omit when the project has no Lead. */
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
              size={labelled ? "sm" : "icon"}
              aria-label="Team"
              data-testid="project-team-menu"
              loading={open.isPending}
              className={cn(labelled && "-ml-2 h-10 gap-2 px-2")}
            >
              {labelled && leadPath !== undefined ? (
                <PersonaAvatar name={leadName ?? "Lead"} size="xs" />
              ) : (
                <UsersIcon />
              )}
              {labelled && <span className="text-optical">Team</span>}
              {ordered.some((identity) => identity.active_session_state === "busy") && (
                <RunningDot />
              )}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align={labelled ? "start" : "end"}>
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
  const lead = identities.data?.find((identity) => identity.builtin);
  // Overlapping avatars; a ring in the page color separates neighbours.
  const avatarButton =
    "relative size-7 rounded-full p-0 ring-2 ring-background hover:z-10 hover:bg-transparent focus-visible:z-10";
  // Bottom-left: each avatar paints over its left neighbour, so a right-side mark would be hidden.
  const busyMark = (
    <RunningDot className="absolute -bottom-0.5 -left-0.5 size-3 rounded-full bg-background p-px" />
  );
  return (
    <div
      className="flex items-center gap-1.5"
      role="group"
      aria-label="Team members"
      data-testid="project-identity-roster"
    >
      <div className="flex items-center -space-x-1">
        {leadPath !== undefined && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button asChild variant="ghost" className={avatarButton}>
                <Link to={leadPath} aria-label="Lead">
                  <PersonaAvatar name={leadName ?? "Lead"} className="size-7 text-[0.625rem]" />
                  {lead?.active_session_state === "busy" && busyMark}
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {/* One text node: the tooltip is a flex row that would split each part into a column. */}
              <span>
                <span className="font-medium">{leadName ?? "Lead"}</span>: coordinates the board
              </span>
            </TooltipContent>
          </Tooltip>
        )}
        {visible.map((identity) => {
          const busy = identity.active_session_state === "busy";
          const pending = open.isPending && open.variables?.id === identity.id;
          return (
            <Tooltip key={identity.id}>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  className={avatarButton}
                  aria-label={`Talk to ${identity.name}${busy ? " (working)" : ""}`}
                  data-testid="project-identity-button"
                  disabled={pending}
                  onClick={() => open.mutate(identity)}
                >
                  <PersonaAvatar name={identity.name} className="size-7 text-[0.625rem]" />
                  {(busy || pending) && busyMark}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <span>
                  <span className="font-medium">{identity.name}</span>
                  {": "}
                  {busy
                    ? "working; open its session"
                    : identity.active_session_id
                      ? "open its session"
                      : "start a session"}
                  {identity.router && ". It receives ticket events and escalations."}
                </span>
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
                aria-label={`${hidden.length} more team members`}
                data-testid="project-identity-more"
                className={cn(
                  avatarButton,
                  "bg-muted text-[0.625rem] font-semibold text-muted-foreground tabular-nums hover:bg-accent",
                )}
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
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            aria-label="Add a team member"
            data-testid="project-edit-team"
            className="size-7 rounded-full border border-dashed border-muted-foreground/50 p-0 text-muted-foreground hover:border-foreground/60 hover:bg-transparent hover:text-foreground"
            onClick={() => setEditing(true)}
          >
            <UserPlusIcon className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {open.error
            ? `Could not open: ${open.error.message}`
            : "Add a team member or edit the team"}
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
