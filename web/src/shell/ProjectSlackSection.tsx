/**
 * The project's Slack channel bridge, opened from Sources → Slack channel: an
 * `@omni` mention in the channel reaches the project's Lead. Members see the
 * link; the owner and product approvers change it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLinkIcon } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getProjectSlack,
  linkProjectSlack,
  type ProjectSlackLink,
  type ProjectSlackState,
  setProjectSlackEnabled,
  unlinkProjectSlack,
} from "@/lib/projectSlackApi";

export const SLACK_STATE_TEXT: Record<ProjectSlackState, string> = {
  claimed: "Setting up the channel watcher",
  active: "On: @omni in this channel reaches the Lead",
  disabled: "Paused: the channel is kept for this project but not watched",
  error: "Setup failed",
  needs_reconnect: "Needs the owner to sign in",
};

// States where the watcher isn't running and retrying sets it up.
const RETRY_STATES: ReadonlySet<ProjectSlackState> = new Set(["claimed", "error", "needs_reconnect"]);

export function projectSlackQueryKey(projectId: string) {
  return ["project-slack", projectId];
}

export function useProjectSlack(projectId: string | null) {
  return useQuery({
    queryKey: projectSlackQueryKey(projectId ?? ""),
    queryFn: () => getProjectSlack(projectId ?? ""),
    enabled: projectId !== null,
  });
}

/** The link's saved settings to send again, minus what the server fills in. */
function savedDestination(link: ProjectSlackLink | undefined): Record<string, string> {
  const { relay_url: _relayUrl, ...rest } = link?.destination ?? {};
  return rest;
}

export function slackChannelLabel(link: ProjectSlackLink): string {
  return link.channel_name ? `#${link.channel_name}` : (link.channel_id ?? "");
}

export function ProjectSlackSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const link = useProjectSlack(projectId);
  const [channelLink, setChannelLink] = useState("");
  const [channelName, setChannelName] = useState("");
  const onDone = (next: ProjectSlackLink) =>
    queryClient.setQueryData(projectSlackQueryKey(projectId), next);
  const save = useMutation({
    mutationFn: () =>
      linkProjectSlack(projectId, {
        channel_link: channelLink.trim(),
        channel_name: channelName.trim() || link.data?.channel_name || undefined,
        destination: savedDestination(link.data),
      }),
    onSuccess: (next) => {
      setChannelLink("");
      setChannelName("");
      onDone(next);
    },
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setProjectSlackEnabled(projectId, enabled),
    onSuccess: onDone,
  });
  const unlink = useMutation({ mutationFn: () => unlinkProjectSlack(projectId), onSuccess: onDone });
  const data = link.data;
  const canManage = data?.can_manage === true;
  const busy = save.isPending || toggle.isPending || unlink.isPending;
  const failure = [save, toggle, unlink].find((mutation) => mutation.isError)?.error as
    | Error
    | undefined;
  const retry = data?.state !== undefined && RETRY_STATES.has(data.state);
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (channelLink.trim()) save.mutate();
  };
  return (
    <section className="flex flex-col gap-3" data-testid="project-slack">
      <p className="text-muted-foreground text-sm">
        Anyone in the channel can <code className="text-xs">@omni</code> the project&apos;s Lead,
        which answers, delegates or files a ticket. Follow-ups in the thread need no mention, and
        replies stay in the thread marked <em>via this project · Lead</em>.
      </p>
      {link.isError && (
        <p className="text-destructive text-sm" role="alert">
          {(link.error as Error).message}
        </p>
      )}
      {data?.linked === true && (
        <div className="flex flex-col gap-1 text-sm" data-testid="project-slack-link">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-medium">{slackChannelLabel(data)}</span>
            {data.state !== undefined && (
              <span className="text-muted-foreground" data-testid="project-slack-state">
                {SLACK_STATE_TEXT[data.state]}
              </span>
            )}
          </div>
          {data.last_error && (
            <p className="text-destructive" role="alert">
              {data.last_error}
            </p>
          )}
          {(data.overlaps ?? []).length > 0 && (
            <p className="text-amber-600 dark:text-amber-400" data-testid="project-slack-overlap">
              Another automation also watches this channel ({data.overlaps?.join(", ")}). Turn
              it off so messages aren&apos;t answered twice.
            </p>
          )}
          {data.automation_link && (
            <a
              href={data.automation_link}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 self-start text-xs underline underline-offset-2"
              data-testid="project-slack-automation-link"
            >
              Open the channel watcher
              <ExternalLinkIcon aria-hidden className="size-3" />
            </a>
          )}
          {canManage && (
            <div className="flex gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                variant="outline"
                loading={toggle.isPending}
                disabled={busy}
                data-testid="project-slack-toggle"
                onClick={() => toggle.mutate(data.state === "disabled" || retry)}
              >
                {data.state === "disabled" ? "Resume" : retry ? "Retry" : "Pause"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                loading={unlink.isPending}
                disabled={busy}
                data-testid="project-slack-unlink"
                onClick={() => unlink.mutate()}
              >
                Unlink
              </Button>
            </div>
          )}
        </div>
      )}
      {link.isSuccess && !data?.linked && !canManage && (
        <p className="text-muted-foreground text-sm">
          Not linked to a Slack channel. The project owner or a product approver can link one.
        </p>
      )}
      {canManage && (
        <form onSubmit={onSubmit} className="flex flex-col gap-2" data-testid="project-slack-form">
          <Input
            value={channelLink}
            onChange={(event) => setChannelLink(event.target.value)}
            placeholder="Channel link from Slack (Copy link), e.g. https://example.slack.com/archives/C0456"
            aria-label="Slack channel link"
            data-testid="project-slack-channel-link"
          />
          <Input
            value={channelName}
            onChange={(event) => setChannelName(event.target.value)}
            placeholder="Channel name (optional)"
            aria-label="Slack channel name"
            data-testid="project-slack-channel-name"
          />
          <Button
            type="submit"
            size="sm"
            className="self-start"
            loading={save.isPending}
            disabled={busy || channelLink.trim() === ""}
            data-testid="project-slack-save"
          >
            {data?.linked ? "Change channel" : "Link channel"}
          </Button>
        </form>
      )}
      {failure !== undefined && (
        <p className="text-destructive text-sm" role="alert">
          {failure.message}
        </p>
      )}
    </section>
  );
}
